# Fresh Machine Prerequisites

These are the exact prerequisite commands for Phase 5 fresh-machine testing on
Ubuntu 24.04.3 LTS. They install the base tools, a pinned Node runtime, and a
pinned OpenClaw release.

Pinned versions:

- Node.js: `22.22.3-1nodesource1`
- OpenClaw default: `2026.7.1`

Run as root, or as a sudo-capable user:

```bash
set -euo pipefail

OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.7.1}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git jq python3 python3-pip python3-venv tmux

curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs=22.22.3-1nodesource1

npm install -g "openclaw@${OPENCLAW_VERSION}"
hash -r

python3 --version
python3 -m pip --version
git --version
jq --version
node --version
npm --version
tmux -V
openclaw --version
```
