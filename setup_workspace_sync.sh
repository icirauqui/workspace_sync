#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./setup_workspace_sync.sh <source|target|both>

Installs the required packages, installs workspace_sync.py into ~/.local/bin,
and creates ~/.config/workspace_sync/config.json from the example file if it
does not already exist.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

ROLE="${1:-}"

case "$ROLE" in
  source|target|both)
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC="$SCRIPT_DIR/workspace_sync.py"
CONFIG_SRC="$SCRIPT_DIR/workspace_sync_config.example.json"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/workspace_sync"
APP_DST="$BIN_DIR/workspace_sync.py"
CONFIG_DST="$CONFIG_DIR/config.json"

if [[ ! -f "$APP_SRC" ]]; then
  echo "Missing script: $APP_SRC" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_SRC" ]]; then
  echo "Missing example config: $CONFIG_SRC" >&2
  exit 1
fi

packages=(git rsync openssh-client)
if [[ "$ROLE" == "source" || "$ROLE" == "both" ]]; then
  packages+=(openssh-server)
fi

echo "[1/4] Installing packages: ${packages[*]}"
sudo apt update
sudo apt install -y "${packages[@]}"

if [[ "$ROLE" == "source" || "$ROLE" == "both" ]]; then
  echo "[2/4] Enabling SSH server"
  sudo systemctl enable --now ssh
else
  echo "[2/4] Skipping SSH server setup for target role"
fi

echo "[3/4] Installing workspace_sync.py into $APP_DST"
mkdir -p "$BIN_DIR" "$CONFIG_DIR"
install -m 755 "$APP_SRC" "$APP_DST"

echo "[4/4] Ensuring config exists at $CONFIG_DST"
if [[ -f "$CONFIG_DST" ]]; then
  echo "Config already exists; leaving it unchanged."
else
  cp "$CONFIG_SRC" "$CONFIG_DST"
  echo "Created config from example."
fi

cat <<EOF

Setup complete.

Next steps:
- Edit $CONFIG_DST on this laptop.
EOF

if [[ "$ROLE" == "source" || "$ROLE" == "both" ]]; then
  cat <<'EOF'
- On the source laptop, create a snapshot with:
  workspace_sync.py source
EOF
fi

if [[ "$ROLE" == "target" || "$ROLE" == "both" ]]; then
  cat <<'EOF'
- On the target laptop, set up SSH access to the source laptop:
  ssh-keygen -t ed25519
  ssh-copy-id youruser@source-host
- Pull from the source with:
  workspace_sync.py target --from youruser@source-host
EOF
fi
