#!/usr/bin/env python3
"""hostci: a small local CI/CD runner for GitHub repos, without GitHub Actions.

Each run (systemd timer, every minute) polls the configured repos and builds:
  * the default branch head            -> job "ci"
  * open pull requests (trusted only)  -> job "ci"
  * tags matching release_tag_glob     -> job "release" (CD), then publishes a GitHub Release

Results are reported as commit statuses (context "hostci/<job>"), so branch rulesets can
require them. Jobs run the repo's own `ci/run <job>` as the unprivileged `ci` user in a
transient, sandboxed systemd unit with no GitHub credentials. Only this privileged poller
talks to GitHub.
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG = Path(os.environ.get("HOSTCI_CONFIG", "/etc/hostci/config.toml"))
STATE = Path("/var/lib/hostci-state")           # root-only: mirrors, logs, seen-tags
WORK = Path("/var/lib/hostci/work")             # ci-owned job checkouts
TRUSTED_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass
class Repo:
    name: str                                  # owner/repo
    jobs_timeout: int = 1800
    memory_max: str = "6G"
    cpu_quota: str = "600%"
    release_tag_glob: str = "v*"
    trusted_label: str = "ci:run"              # owner adds this to run CI on untrusted PRs
    extra_env: dict = field(default_factory=dict)


@dataclass
class Build:
    repo: Repo
    sha: str
    ref: str                                   # refspec to fetch (branch, refs/pull/N/head, tag)
    job: str                                   # "ci" | "release"
    label: str                                 # human description, e.g. "PR #3" / "main" / "v1.0.0"
    tag: str | None = None


def log(msg: str) -> None:
    print(f"[hostci] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def gh_api(path: str, method: str = "GET", fields: dict | None = None):
    cmd = ["gh", "api", "-X", method, path]
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    out = sh(cmd).stdout
    return json.loads(out) if out.strip() else None


def load_config() -> list[Repo]:
    cfg = tomllib.loads(CONFIG.read_text())
    return [Repo(name=r.pop("name"), **r) for r in cfg.get("repo", [])]


# ---------------------------------------------------------------- discovery

WAITING = "waiting: untrusted PR"


def latest_status(repo: Repo, sha: str, context: str) -> dict | None:
    """Newest status for a context (the API lists newest first)."""
    st = gh_api(f"repos/{repo.name}/commits/{sha}/statuses?per_page=100")
    return next((s for s in st or [] if s["context"] == context), None)


def has_status(repo: Repo, sha: str, context: str) -> bool:
    return latest_status(repo, sha, context) is not None


def ci_succeeded(repo: Repo, sha: str) -> bool:
    st = gh_api(f"repos/{repo.name}/commits/{sha}/status")
    return any(s["context"] == "hostci/ci" and s["state"] == "success" for s in st["statuses"])


def discover(repo: Repo) -> list[Build]:
    builds: list[Build] = []
    meta = gh_api(f"repos/{repo.name}")
    default = meta["default_branch"]
    head = gh_api(f"repos/{repo.name}/commits/{default}")["sha"]
    if not has_status(repo, head, "hostci/ci"):
        builds.append(Build(repo, head, f"refs/heads/{default}", "ci", default))

    for pr in gh_api(f"repos/{repo.name}/pulls?state=open&per_page=100") or []:
        sha = pr["head"]["sha"]
        trusted = pr["author_association"] in TRUSTED_ASSOC or any(
            lb["name"] == repo.trusted_label for lb in pr["labels"])
        last = latest_status(repo, sha, "hostci/ci")
        waiting = last is not None and last["description"].startswith(WAITING)
        if last is not None and not (waiting and trusted):
            continue
        if not trusted:
            set_status(repo, sha, "pending", "hostci/ci",
                       f"{WAITING}, owner must add label '{repo.trusted_label}'")
            continue
        builds.append(Build(repo, sha, f"refs/pull/{pr['number']}/head", "ci", f"PR #{pr['number']}"))

    releases = {r["tag_name"] for r in gh_api(f"repos/{repo.name}/releases?per_page=100") or []}
    for t in gh_api(f"repos/{repo.name}/tags?per_page=100") or []:
        tag = t["name"]
        if not fnmatch.fnmatch(tag, repo.release_tag_glob) or tag in releases:
            continue
        sha = t["commit"]["sha"]
        if has_status(repo, sha, "hostci/release"):
            continue
        if not ci_succeeded(repo, sha):
            # release only commits that already passed CI on the default branch / a PR
            if not has_status(repo, sha, "hostci/ci"):
                builds.append(Build(repo, sha, f"refs/tags/{tag}", "ci", tag))
            continue
        builds.append(Build(repo, sha, f"refs/tags/{tag}", "release", tag, tag=tag))
    return builds


# ---------------------------------------------------------------- execution

def set_status(repo: Repo, sha: str, state: str, context: str, desc: str, url: str = "") -> None:
    fields = {"state": state, "context": context, "description": desc[:139]}
    if url:
        fields["target_url"] = url
    gh_api(f"repos/{repo.name}/statuses/{sha}", "POST", fields)


def mirror(repo: Repo) -> Path:
    m = STATE / "mirrors" / (repo.name.replace("/", "__") + ".git")
    url = f"https://github.com/{repo.name}.git"
    if not m.exists():
        m.parent.mkdir(parents=True, exist_ok=True)
        sh(["git", "-c", "credential.helper=!gh auth git-credential", "clone", "--bare", "-q", url, str(m)])
    return m


def checkout(b: Build) -> Path:
    m = mirror(b.repo)
    sh(["git", "-C", str(m), "-c", "credential.helper=!gh auth git-credential", "fetch", "-q",
        "origin", f"+{b.ref}:refs/hostci/{b.sha}"])
    wd = WORK / b.repo.name.replace("/", "__") / f"{b.sha[:12]}-{b.job}"
    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    archive = subprocess.Popen(["git", "-C", str(m), "archive", b.sha], stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(wd)], stdin=archive.stdout, check=True)
    archive.wait()
    sh(["chown", "-R", "ci:ci", str(wd)])
    return wd


def run_job(b: Build, wd: Path, logf: Path) -> int:
    entry = wd / "ci" / "run"
    if not entry.exists():
        logf.write_text("hostci: repository has no ci/run entrypoint\n")
        return 2
    env = {
        "HOME": "/var/lib/hostci/cache", "CI": "true", "HOSTCI": "1",
        "NUGET_PACKAGES": "/var/lib/hostci/cache/nuget",
        "HOSTCI_SHA": b.sha, "HOSTCI_REF": b.ref, "HOSTCI_JOB": b.job, "HOSTCI_TAG": b.tag or "",
        "PATH": "/opt/ci-tools/bin:/opt/dotnet:/usr/local/bin:/usr/bin:/bin",
        "DOTNET_ROOT": "/opt/dotnet", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
        "PSModulePath": "/opt/ci-tools/psmodules", **b.repo.extra_env,
    }
    cmd = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect",
           f"--unit=hostci-{b.sha[:12]}-{b.job}", "--uid=ci", "--gid=ci",
           f"--working-directory={wd}",
           "-p", f"RuntimeMaxSec={b.repo.jobs_timeout}", "-p", f"MemoryMax={b.repo.memory_max}",
           "-p", f"CPUQuota={b.repo.cpu_quota}", "-p", "Nice=10", "-p", "IOSchedulingClass=idle",
           "-p", "NoNewPrivileges=yes", "-p", "PrivateTmp=yes", "-p", "PrivateDevices=yes",
           "-p", "ProtectSystem=strict", "-p", "ProtectHome=yes", "-p", "ProtectKernelTunables=yes",
           "-p", "ProtectKernelModules=yes", "-p", "ProtectControlGroups=yes",
           "-p", "RestrictSUIDSGID=yes", "-p", "LockPersonality=yes",
           "-p", "InaccessiblePaths=-/var/lib/hostci-state -/etc/pve -/etc/hostci -/root -/run/lock",
           "-p", f"ReadWritePaths={wd} /var/lib/hostci/cache"]
    cmd += [f"--setenv={k}={v}" for k, v in env.items()]
    cmd += ["--", "/bin/bash", "-c", f"exec ./ci/run {b.job} 2>&1"]
    t0 = time.time()
    with logf.open("w") as fh:
        fh.write(f"hostci: {b.repo.name} {b.label} {b.sha} job={b.job}\n$ ./ci/run {b.job}\n\n")
        fh.flush()
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
        fh.write(f"\nhostci: exit {rc} after {time.time() - t0:.0f}s\n")
    return rc


def publish_log(b: Build, logf: Path) -> str:
    """Secret gist with the (tail of the) log, linked from the commit status."""
    text = logf.read_text(errors="replace")
    if len(text) > 900_000:
        text = "[... truncated, full log on host: " + str(logf) + " ...]\n" + text[-900_000:]
    tmp = logf.with_suffix(".gist.log")
    tmp.write_text(text)
    try:
        out = sh(["gh", "gist", "create", "--desc",
                  f"hostci {b.repo.name} {b.label} {b.sha[:12]} {b.job}", str(tmp)]).stdout.strip()
        return out.splitlines()[-1]
    except subprocess.CalledProcessError as e:
        log(f"gist upload failed: {e.stderr.strip()}")
        return ""
    finally:
        tmp.unlink(missing_ok=True)


def publish_release(b: Build, wd: Path) -> None:
    out = wd / "out" / "release"
    assets = sorted(p for p in out.glob("*") if p.is_file()) if out.exists() else []
    if assets:
        sums = "".join(f"{sh(['sha256sum', p.name], cwd=out).stdout}" for p in assets)
        (out / "SHA256SUMS").write_text(sums)
        assets.append(out / "SHA256SUMS")
    cmd = ["gh", "release", "create", b.tag, "--repo", b.repo.name, "--verify-tag",
           "--generate-notes", "--title", b.tag]
    if "-" in b.tag:
        cmd.append("--prerelease")
    sh(cmd + [str(p) for p in assets])


def build(b: Build) -> None:
    ctx = f"hostci/{b.job}"
    logdir = STATE / "logs" / b.repo.name.replace("/", "__")
    logdir.mkdir(parents=True, exist_ok=True)
    logf = logdir / f"{time.strftime('%Y%m%d-%H%M%S')}-{b.sha[:12]}-{b.job}.log"
    log(f"{b.repo.name} {b.label} {b.sha[:12]} {b.job}: start")
    set_status(b.repo, b.sha, "pending", ctx, f"running on host ({b.label})")
    try:
        wd = checkout(b)
        rc = run_job(b, wd, logf)
        if rc == 0 and b.job == "release":
            publish_release(b, wd)
        url = publish_log(b, logf)
        state = "success" if rc == 0 else "failure"
        desc = "passed" if rc == 0 else f"failed (exit {rc})"
        if b.job == "release" and rc == 0:
            desc = f"released {b.tag}"
        set_status(b.repo, b.sha, state, ctx, desc, url)
        log(f"{b.repo.name} {b.label} {b.sha[:12]} {b.job}: {state} {url}")
        shutil.rmtree(wd, ignore_errors=True)
    except Exception as e:  # infrastructure error, not a test failure
        log(f"{b.repo.name} {b.sha[:12]}: ERROR {e}")
        with logf.open("a") as fh:
            fh.write(f"\nhostci infrastructure error: {e}\n{getattr(e, 'stderr', '')}\n")
        set_status(b.repo, b.sha, "error", ctx, f"runner error: {str(e)[:100]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="only list what would be built")
    ap.add_argument("--rerun", metavar="OWNER/REPO@SHA", help="force a ci job for one commit")
    args = ap.parse_args()
    lock = open("/run/hostci.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run is active; exiting")
        return 0
    repos = load_config()
    if args.rerun:
        name, sha = args.rerun.split("@")
        repo = next(r for r in repos if r.name == name)
        build(Build(repo, sha, sha, "ci", f"rerun {sha[:12]}"))
        return 0
    for repo in repos:
        try:
            todo = discover(repo)
        except subprocess.CalledProcessError as e:
            log(f"{repo.name}: discovery failed: {e.stderr.strip()}")
            continue
        for b in todo:
            if args.dry_run:
                log(f"would build {repo.name} {b.label} {b.sha[:12]} {b.job}")
            else:
                build(b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
