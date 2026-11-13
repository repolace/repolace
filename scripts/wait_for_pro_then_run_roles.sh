#!/bin/bash
# Probe Vertex Pro until it answers 3 times in a row 60 s apart, then start N runners of one benchmark
# run from another checkout (the roles worktree), so each runner dispatches one task at a time.
# Usage: wait_for_pro_then_run_roles.sh <checkout_dir> <eval_run_id> <max_total_usd> [runners=3] [extra run args...]
set -u
DIR=${1:?checkout dir}; RUN=${2:?eval run id}; CAP=${3:?max total usd}; N=${4:-3}; shift 4 2>/dev/null || shift $#
cd "$DIR" || exit 2
export DOCKER_HOST=${DOCKER_HOST:-unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock}
# The key goes in a header, not the URL: a query string shows in `ps` and in any curl trace.
K=$(grep '^GOOGLE_API_KEY=' .env | cut -d= -f2-)
probe() {
  curl -s -m 60 -o /dev/null -w "%{http_code}" -X POST \
    "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-pro-preview:generateContent" \
    -H "x-goog-api-key: $K" \
    -H 'Content-Type: application/json' \
    -d '{"contents":[{"role":"user","parts":[{"text":"say ok"}]}],"generationConfig":{"maxOutputTokens":8}}'
}
ok=0
while [ "$ok" -lt 3 ]; do
  c=$(probe); echo "$(date -u +%T) probe $c"
  if [ "$c" = 200 ]; then ok=$((ok + 1)); sleep 60; else ok=0; sleep 600; fi
done
echo "$(date -u +%T) pro healthy, starting $N runner(s) for $RUN"
mkdir -p eval/runs
for i in $(seq 1 "$N"); do
  uv run --all-packages repolace-eval run --eval-run-id "$RUN" --agent llm --model vertex-gemini-3-1-pro-preview \
    -k 1 --max-total-usd "$CAP" --no-pr "$@" > "eval/runs/${RUN}-launcher-runner-$i.log" 2>&1 &
  sleep 20
done
wait
echo "$(date -u +%T) all runners for $RUN ended"
