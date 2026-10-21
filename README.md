# hostci

Local CI/CD for GitHub repositories, run on our own host. GitHub Actions is banned for these
repos (it's disabled in each repo's settings and `ci/run` fails if `.github/workflows` exists).

## How it works
- `hostci.timer` runs `hostci.py` every minute. For each repo in `/etc/hostci/config.toml` it
  builds commits that don't have a `hostci/<job>` commit status yet:
  - the default branch head, job `ci`
  - open PR heads, job `ci`. PRs from non-collaborators only run after the owner adds the label
    `ci:run`, because public repos can receive hostile PRs.
  - tags matching `v*`, job `release`, only once `hostci/ci` passed on that commit. It then
    creates the GitHub Release with `out/release/*` plus `SHA256SUMS`.
- The repo defines its pipeline in `ci/run <job>` (exit 0 = pass). Release artifacts go to
  `out/release/` (only there: files elsewhere, e.g. `out/package/`, are never published).
- Release guard: nothing is published unless `out/release` is non-empty, every glob in the
  repo's `release_assets` matches (`{tag}`, `{version}` = tag without `v`), no asset is empty and
  the generated `SHA256SUMS` lists exactly the assets and verifies. The release is created as a
  draft, the uploaded assets (name, size, state) are compared with the local ones, and only then
  is it published. A blocked release sets `hostci/release` to failure ("release blocked: ...");
  a failed upload check leaves the draft: delete the draft to let hostci retry the tag.
- Jobs run as user `ci` in a transient systemd unit: no GitHub token, ProtectSystem=strict,
  ProtectHome, no access to /root or Proxmox config, MemoryMax/CPUQuota/RuntimeMaxSec limits.
  Only the root poller talks to GitHub (via `gh`).
- Each result is posted as a commit status. Its link points to a secret gist with the log;
  full logs stay in `/var/lib/hostci-state/logs/`.
- Branch rulesets require the `hostci/ci` status, so PRs can't merge until the host has built them.

## Repo policy (applies to every hostci-managed repo)
- `main` ruleset: PR required, `hostci/ci` must pass on an up-to-date branch, squash-only,
  linear history, signed commits, no force-push or deletion, no bypass for anyone.
- `v*` tags can't be updated or deleted. Commits and tags are SSH-signed on the host
  (`/root/.ssh/id_ed25519_git_signing`, registered on GitHub as a signing key).

## Credentials
`/etc/hostci/token.env` (0600, root) holds `GH_TOKEN`. install.sh seeds it from `gh auth token`.
For least privilege, replace it with a token that can only write commit statuses, contents
and releases on the configured repos, plus gists for the log links.

## Operate
    sudo ./install.sh                         # install/update, enables the timer
    python3 /opt/hostci/hostci.py --dry-run   # what would build now
    python3 /opt/hostci/hostci.py --rerun OWNER/REPO@SHA
    # release dry run: release job + asset check for a commit, no tag/status/release needed
    python3 /opt/hostci/hostci.py --release-check OWNER/REPO@SHA v1.2.3
    # ... plus a draft upload to an existing throwaway tag test-v1.2.3 (expects v1.2.3's assets;
    # delete the draft and the tag afterwards)
    python3 /opt/hostci/hostci.py --release-check OWNER/REPO@SHA test-v1.2.3 --draft
    journalctl -u hostci -f                   # live runner log
    ls /var/lib/hostci-state/logs/

Toolchain available to jobs (PATH): /opt/ci-tools/bin (repak, retoc, gitleaks, pwsh),
/opt/dotnet (.NET 10 SDK), shellcheck, luac5.4, python3 + pyflakes, PSScriptAnalyzer in
/opt/ci-tools/psmodules.
