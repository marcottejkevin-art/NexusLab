#!/usr/bin/env bash
# Install the NexusLab agent as a systemd service.
# Usage:  sudo DOCKER_MODE=control ./install-agent.sh     (off | monitor | control)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

DIR=/opt/nexuslab-agent
ENV=/etc/nexuslab-agent.env
HERE="$(cd "$(dirname "$0")" && pwd)"

# Make sure Python can create virtual environments
if ! python3 -c "import venv, ensurepip" >/dev/null 2>&1; then
  if command -v apt-get >/dev/null; then
    apt-get update && apt-get install -y python3-venv
  elif command -v pacman >/dev/null; then
    pacman -S --noconfirm --needed python
  else
    echo "Please install Python's venv module, then run this again."; exit 1
  fi
fi

mkdir -p "$DIR"
cp "$HERE/agent.py" "$HERE/requirements.txt" "$DIR/"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

if [ ! -f "$ENV" ]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  cat > "$ENV" <<CONF
NEXUSLAB_TOKEN=$TOKEN
NEXUSLAB_PORT=9101
NEXUSLAB_DOCKER=${DOCKER_MODE:-off}
# Comma-separated container names to show; leave empty to show all
NEXUSLAB_CONTAINERS=
NEXUSLAB_DISK_PATH=/
CONF
  chmod 600 "$ENV"
fi

cp "$HERE/nexuslab-agent.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nexuslab-agent
systemctl restart nexuslab-agent

echo
echo "NexusLab agent is running on port $(grep NEXUSLAB_PORT "$ENV" | cut -d= -f2)."
echo "Token for the hub config:  $(grep NEXUSLAB_TOKEN "$ENV" | cut -d= -f2)"
