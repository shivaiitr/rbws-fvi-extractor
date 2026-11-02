#!/usr/bin/env bash
# Copy this project to an Oracle Always Free Ubuntu VM and run it in Docker.
# Usage: scripts/oracle_ship.sh ubuntu@PUBLIC_IP /path/to/ssh-key
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: scripts/oracle_ship.sh ubuntu@PUBLIC_IP /path/to/ssh-key" >&2
  exit 2
fi

TARGET="$1"
KEY="$2"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new)
RSYNC_SSH="ssh -i ${KEY} -o StrictHostKeyChecking=accept-new"

PASSWORD="$(
  python3 - "$ROOT/.streamlit/secrets.toml" <<'PY'
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
for line in text.splitlines():
    if line.strip().startswith("APP_PASSWORD"):
        print(line.split("=", 1)[1].strip().strip('"').strip("'"))
        break
PY
)"
if [[ -z "$PASSWORD" ]]; then
  echo "APP_PASSWORD is missing from .streamlit/secrets.toml" >&2
  exit 1
fi

echo "Copying the app to ${TARGET}"
rsync -az --delete \
  -e "$RSYNC_SSH" \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude 'outputs/' \
  --exclude 'artifacts/' \
  --exclude '.cursor/' \
  --exclude '*.xlsx' \
  --exclude '*.pdf' \
  --exclude '*.zip' \
  --exclude '.env' \
  --exclude '.streamlit/secrets.toml' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  "$ROOT/" "${TARGET}:~/rbws-fvi-extractor/"

echo "Building and starting the app"
"${SSH[@]}" "$TARGET" "PASSWORD=$(printf '%q' "$PASSWORD") bash -s" <<'REMOTE'
set -euo pipefail
if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y docker.io
  sudo systemctl enable --now docker
fi
umask 077
printf 'APP_PASSWORD=%s\n' "$PASSWORD" > "$HOME/rbws-fvi-extractor/.env"
cd "$HOME/rbws-fvi-extractor"
sudo docker build -t rbws-fvi .
sudo docker rm -f rbws-fvi >/dev/null 2>&1 || true
sudo docker run -d --name rbws-fvi --restart unless-stopped \
  -p 8501:8501 --env-file .env rbws-fvi
sudo docker ps --filter name=rbws-fvi
REMOTE

echo "App is on port 8501. Open TCP 8501 in the Oracle security list if the page does not load."
