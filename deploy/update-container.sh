#!/usr/bin/env bash
# Update one instance that runs inside a Docker container under supervisord.
#   update-container.sh HOST_DIR CONTAINER CONTAINER_DIR PROGRAM [PROGRAM...]
# HOST_DIR is the git checkout on the host (bind-mounted as CONTAINER_DIR in the container).
# Fetch → fast-forward → install changed requirements → tests in the container → restart programs
# → get_status. If tests fail, the checkout goes back to the previous commit and nothing restarts.
set -euo pipefail
HOST_DIR=$1; CONTAINER=$2; CDIR=$3; shift 3; PROGRAMS=("$@")
G="sudo git -C $HOST_DIR"

$G fetch --quiet origin main
if [ "$($G rev-parse HEAD)" = "$($G rev-parse origin/main)" ]; then
    echo "$CONTAINER: already up to date ($($G log -1 --format=%h))"
    exit 0
fi
prev=$($G rev-parse HEAD)
$G merge --ff-only --quiet origin/main
echo "$CONTAINER: updated ${prev:0:7} → $($G log -1 --format='%h %s')"

run() { sudo docker exec -w "$CDIR" "$CONTAINER" sh -c "$1"; }
if ! $G diff --quiet "$prev" HEAD -- requirements.txt; then
    run ".venv/bin/pip install --quiet -r requirements.txt"
fi
if ! run ".venv/bin/python -m unittest discover -p 'test_*.py' -q" 2>/tmp/"$CONTAINER"-tests.log; then
    tail -20 /tmp/"$CONTAINER"-tests.log
    $G reset --hard --quiet "$prev"
    echo "$CONTAINER: TESTS FAILED, rolled back to ${prev:0:7}, not restarted"
    exit 1
fi
echo "$CONTAINER: tests passed ($(grep -o 'Ran [0-9]* tests' /tmp/"$CONTAINER"-tests.log))"
run "supervisorctl restart ${PROGRAMS[*]} >/dev/null"
sleep 8
run "printf '{}' | .venv/bin/python rpc.py get_status" | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print("  status:", r["service"], r["service_version"], r["tool_count"], "tools, authorized:", r["authorized"])'
