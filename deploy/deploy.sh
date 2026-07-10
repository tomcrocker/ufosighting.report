#!/usr/bin/env bash
# Deploy from the Mac to the Oracle VM. Run from the repo root:
#   bash deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="ubuntu@170.9.36.91"
KEY="oracle2.key"
DEST="/home/ubuntu/ufosighting"

rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude 'data/' --exclude '.env' \
  --exclude 'oracle2.key' --exclude '__pycache__' --exclude '.claude' \
  --exclude '.pytest_cache' \
  -e "ssh -i $KEY" ./ "$HOST:$DEST/"

ssh -i "$KEY" "$HOST" "
  set -e
  cd $DEST
  .venv/bin/pip install -q -r requirements.txt
  sudo systemctl restart ufosighting-web
  sleep 2
  systemctl is-active ufosighting-web
  curl -sf -o /dev/null http://127.0.0.1:8010/ && echo 'deploy OK'
"
