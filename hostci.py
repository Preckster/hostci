#!/usr/bin/env python3
"""hostci: a small local CI/CD runner for GitHub repos, without GitHub Actions.

Each run (systemd timer, every minute) polls the configured repos and builds:
  * the default branch head            -> job "ci"
  * open pull requests (trusted only)  -> job "ci"
  * tags matching release_tag_glob     -> job "release" (CD), then publishes a GitHub Release
                                          with out/release/* + SHA256SUMS, only when every
                                          asset in release_assets is there (see publish_release)

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
    # globs that out/release must match (each at least once) before a release is published;
    # {tag} = the tag, {version} = the tag without a leading "v" (test-v1.2 counts as v1.2). An empty out/release always
    # fails unless release_allow_empty is set.
    release_assets: list = field(default_factory=list)
    release_allow_empty: bool = False
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


def complete_auto_merges(repo: Repo) -> None:
    """Merge PRs whose author enabled auto-merge once they are CLEAN.

    Safety net: GitHub's own auto-merge did fire for hostci statuses, but only minutes after
    the PR turned CLEAN. Merging here makes it prompt. The merge still goes through GitHub's
    ruleset checks, and --match-head-commit prevents merging a head that changed meanwhile.
    """
    out = sh(["gh", "pr", "list", "--repo", repo.name, "--state", "open", "--json",
              "number,autoMergeRequest,mergeStateStatus,headRefOid"]).stdout
    for pr in json.loads(out or "[]"):
        amr = pr.get("autoMergeRequest")
        if not amr or pr["mergeStateStatus"] != "CLEAN":
            continue
        method = {"SQUASH": "--squash", "REBASE": "--rebase", "MERGE": "--merge"}[amr["mergeMethod"]]
        try:
            sh(["gh", "pr", "merge", str(pr["number"]), "--repo", repo.name, method,
                "--match-head-commit", pr["headRefOid"]])
            log(f"{repo.name}: auto-merged PR #{pr['number']}")
        except subprocess.CalledProcessError as e:
            log(f"{repo.name}: auto-merge of PR #{pr['number']} failed: {e.stderr.strip()}")


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


class ReleaseError(Exception):
    """The release job passed but its assets are incomplete: nothing (or only a draft) is published."""


def expected_assets(repo: Repo, tag: str) -> list[str]:
    # throwaway dry-run tags test-v<ver> (never released by discovery) expect v<ver>'s assets
    tag = tag.removeprefix("test-")
    version = tag[1:] if tag[:1] == "v" else tag
    return [g.replace("{tag}", tag).replace("{version}", version) for g in repo.release_assets]


def prepare_release(b: Build, wd: Path) -> list[Path]:
    """Check out/release against the repo's expected assets and write a verified SHA256SUMS."""
    out = wd / "out" / "release"
    files = sorted(p for p in out.glob("*") if p.is_file() and p.name != "SHA256SUMS") if out.is_dir() else []
    if not files:
        if b.repo.release_allow_empty:
            return []
        raise ReleaseError("out/release is empty: the release job built no assets")
    names = [p.name for p in files]
    missing = [g for g in expected_assets(b.repo, b.tag or "") if not any(fnmatch.fnmatch(n, g) for n in names)]
    if missing:
        raise ReleaseError(f"missing release assets {', '.join(missing)} (out/release has {', '.join(names)})")
    empty = [p.name for p in files if p.stat().st_size == 0]
    if empty:
        raise ReleaseError(f"empty release assets: {', '.join(empty)}")
    sums = out / "SHA256SUMS"
    sums.write_text(sh(["sha256sum", "--", *names], cwd=out).stdout)
    listed = sorted(line.split(None, 1)[1].lstrip("*") for line in sums.read_text().splitlines())
    if listed != sorted(names):
        raise ReleaseError("SHA256SUMS does not list exactly the release assets")
    try:
        sh(["sha256sum", "--strict", "--quiet", "-c", "SHA256SUMS"], cwd=out)
    except subprocess.CalledProcessError as e:
        raise ReleaseError(f"SHA256SUMS does not verify: {(e.stdout + e.stderr).strip()[:200]}") from e
    return files + [sums]


def find_release(repo: Repo, tag: str) -> dict | None:
    """The release for a tag, drafts included (the tags endpoint does not return drafts)."""
    return next((r for r in gh_api(f"repos/{repo.name}/releases?per_page=100") or []
                 if r["tag_name"] == tag), None)


def publish_release(b: Build, wd: Path, publish: bool = True) -> dict:
    """Create the release as a draft, check that GitHub has every asset (name, size, uploaded),
    then publish it. A failed check leaves the draft (delete it to retry) and raises."""
    assets = prepare_release(b, wd)
    cmd = ["gh", "release", "create", b.tag, "--repo", b.repo.name, "--verify-tag", "--draft",
           "--generate-notes", "--title", b.tag]
    if "-" in b.tag:
        cmd.append("--prerelease")
    sh(cmd + [str(p) for p in assets])
    rel = find_release(b.repo, b.tag)
    if rel is None:
        raise ReleaseError(f"draft release {b.tag} not found after upload")
    remote = {a["name"]: (a["size"], a["state"]) for a in rel.get("assets", [])}
    local = {p.name: (p.stat().st_size, "uploaded") for p in assets}
    if remote != local:
        raise ReleaseError(f"uploaded assets differ (draft {b.tag} kept): "
                           f"missing/different {sorted(set(local.items()) - set(remote.items()))}, "
                           f"extra {sorted(set(remote) - set(local))}")
    if publish:
        sh(["gh", "api", "-X", "PATCH", f"repos/{b.repo.name}/releases/{rel['id']}", "-F", "draft=false"])
    return rel


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
        blocked = ""
        if rc == 0 and b.job == "release":
            try:
                publish_release(b, wd)
            except ReleaseError as e:
                blocked = str(e)
                with logf.open("a") as fh:
                    fh.write(f"\nhostci: release NOT published: {blocked}\n")
        url = publish_log(b, logf)
        state = "success" if rc == 0 and not blocked else "failure"
        desc = "passed" if rc == 0 else f"failed (exit {rc})"
        if b.job == "release" and rc == 0:
            desc = f"release blocked: {blocked}" if blocked else f"released {b.tag}"
        set_status(b.repo, b.sha, state, ctx, desc, url)
        log(f"{b.repo.name} {b.label} {b.sha[:12]} {b.job}: {state} {url}")
        shutil.rmtree(wd, ignore_errors=True)
    except Exception as e:  # infrastructure error, not a test failure
        log(f"{b.repo.name} {b.sha[:12]}: ERROR {e}")
        with logf.open("a") as fh:
            fh.write(f"\nhostci infrastructure error: {e}\n{getattr(e, 'stderr', '')}\n")
        set_status(b.repo, b.sha, "error", ctx, f"runner error: {str(e)[:100]}")


def release_check(repos: list[Repo], target: str, tag: str, draft: bool = False) -> int:
    name, sha = target.split("@")
    repo = next(r for r in repos if r.name == name)
    b = Build(repo, sha, sha, "release", f"release-check {tag}", tag=tag)
    logdir = STATE / "logs" / repo.name.replace("/", "__")
    logdir.mkdir(parents=True, exist_ok=True)
    logf = logdir / f"{time.strftime('%Y%m%d-%H%M%S')}-{sha[:12]}-release-check.log"
    wd = checkout(b)
    try:
        rc = run_job(b, wd, logf)
        log(f"release job exit {rc}, log {logf}")
        if rc != 0:
            return rc
        try:
            if draft:
                rel = publish_release(b, wd, publish=False)
                log(f"draft {tag} verified: {rel['html_url']}")
            else:
                for p in prepare_release(b, wd):
                    log(f"asset {p.name} {p.stat().st_size} bytes")
                log("SHA256SUMS:\n" + (wd / "out" / "release" / "SHA256SUMS").read_text().rstrip())
        except ReleaseError as e:
            log(f"release would be BLOCKED: {e}")
            return 1
        log("release check passed")
        return 0
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="only list what would be built")
    ap.add_argument("--rerun", metavar="OWNER/REPO@SHA", help="force a ci job for one commit")
    ap.add_argument("--release-check", nargs=2, metavar=("OWNER/REPO@SHA", "TAG"),
                    help="run the release job for a commit as if TAG pointed at it and check its "
                         "assets; no status, no release, the tag need not exist")
    ap.add_argument("--draft", action="store_true",
                    help="with --release-check: also upload a draft release to the existing TAG "
                         "and verify it (left as a draft; delete it and the tag afterwards)")
    args = ap.parse_args()
    lock = open("/run/hostci.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run is active; exiting")
        return 0
    repos = load_config()
    if args.release_check:
        return release_check(repos, *args.release_check, draft=args.draft)
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
        if not args.dry_run:
            try:
                complete_auto_merges(repo)
            except subprocess.CalledProcessError as e:
                log(f"{repo.name}: auto-merge scan failed: {e.stderr.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
