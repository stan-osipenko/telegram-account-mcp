#!/usr/bin/env bash
# Update the running service from GitHub: fetch → fast-forward → tests → restart → status.
# Run on the server inside the service directory: ./deploy.sh
# If tests fail, the code goes back to the previous commit and nothing is restarted.
set -euo pipefail
cd "$(dirname "$0")"
SERVICE=${SERVICE:-stan-vps-tg}
PY=.venv/bin/python

git fetch --quiet origin main
if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
    echo "already up to date: $(git log -1 --format='%h %s')"
    exit 0
fi
prev=$(git rev-parse HEAD)
git merge --ff-only --quiet origin/main
echo "updated $(git rev-parse --short "$prev") → $(git log -1 --format='%h %s')"

if ! git diff --quiet "$prev" HEAD -- requirements.txt; then
    $PY -m pip install --quiet -r requirements.txt
fi

if ! $PY -m unittest discover -p 'test_*.py' -q 2>/tmp/"$SERVICE"-tests.log; then
    tail -20 /tmp/"$SERVICE"-tests.log
    git reset --hard --quiet "$prev"
    echo "TESTS FAILED: rolled back to $(git rev-parse --short "$prev"), service not restarted"
    exit 1
fi
echo "tests passed: $(grep -o 'Ran [0-9]* tests' /tmp/"$SERVICE"-tests.log)"

# Do not restart in the middle of a watcher pass that calls this service.
for unit in ${WAIT_FOR_UNITS:-alter-davalch-watch.service}; do
    while systemctl is-active --quiet "$unit"; do sleep 2; done
done

sudo systemctl restart "$SERVICE"
sleep 3
systemctl is-active "$SERVICE"
printf '{}' | $PY rpc.py get_status | $PY -c 'import json,sys; r=json.load(sys.stdin)["result"]; print("status:", r["service_version"], r["tool_count"], "tools, authorized:", r["authorized"])'
