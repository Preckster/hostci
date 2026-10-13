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
  `out/release/`.
- Jobs run as user `ci` in a transient systemd unit: no GitHub token, ProtectSystem=strict,
  ProtectHome, no access to /root or Proxmox config, MemoryMax/CPUQuota/RuntimeMaxSec limits.
  Only the root poller talks to GitHub (via `gh`).
- Each result is posted as a commit status. Its link points to a secret gist with the log;
  full logs stay in `/var/lib/hostci-state/logs/`.
- Branch rulesets require the `hostci/ci` status, so PRs can't merge until the host has built them.

## Operate
    sudo ./install.sh                         # install/update, enables the timer
    python3 /opt/hostci/hostci.py --dry-run   # what would build now
    python3 /opt/hostci/hostci.py --rerun OWNER/REPO@SHA
    journalctl -u hostci -f                   # live runner log
    ls /var/lib/hostci-state/logs/

Toolchain available to jobs (PATH): /opt/ci-tools/bin (repak, retoc, gitleaks, pwsh),
/opt/dotnet (.NET 10 SDK), shellcheck, luac5.4, python3 + pyflakes, PSScriptAnalyzer in
/opt/ci-tools/psmodules.
