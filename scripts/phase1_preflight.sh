#!/usr/bin/env bash
# Read-only checks before the Phase 1 smoke test, dry run or sweep. Run from the repository root:
#
#     scripts/phase1_preflight.sh [model-key]        # default: vertex-gemini-3-1-pro-preview
#
# Prints one line per check ("ok", "warn" or "FAIL"), never a secret (key *names* only), makes no model call
# and writes nothing. Exits 1 if any check FAILed. See docs/PHASE1_TESTING.md.
set -u

MODEL="${1:-vertex-gemini-3-1-pro-preview}"
cd "$(dirname "$0")/.." || exit 2
failures=0

ok()   { printf 'ok    %s\n' "$1"; }
warn() { printf 'warn  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; failures=$((failures + 1)); }

# --- tools ---------------------------------------------------------------------------------------
if command -v git >/dev/null; then
  v=$(git --version | sed -E 's/[^0-9]*([0-9]+)\.([0-9]+).*/\1 \2/')
  set -- $v
  if [ "$1" -gt 2 ] || { [ "$1" -eq 2 ] && [ "$2" -ge 38 ]; }; then ok "git $1.$2 (>= 2.38)"; else fail "git $1.$2 is older than 2.38 (merge-tree --write-tree)"; fi
else fail "git not found"; fi
command -v prlimit >/dev/null && ok "prlimit present (util-linux)" || fail "prlimit missing: the agent's grep tool refuses to build without it"
command -v uv >/dev/null && ok "uv present" || fail "uv not found"

# --- Docker: rootless, with the cgroup limits actually delegated ---------------------------------------
sock="/run/user/$(id -u)/docker.sock"
if [ -z "${DOCKER_HOST:-}" ]; then
  fail "DOCKER_HOST is unset: plain docker talks to the rootful daemon. Use DOCKER_HOST=unix://$sock"
elif [ "$DOCKER_HOST" != "unix://$sock" ]; then
  warn "DOCKER_HOST=$DOCKER_HOST (expected unix://$sock)"
else ok "DOCKER_HOST points at the rootless socket"; fi
if docker info >/dev/null 2>&1; then
  docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless && ok "docker daemon is rootless" || fail "docker daemon is NOT rootless: the sandbox must not run on the rootful daemon"
  controllers="/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers"
  if [ -r "$controllers" ]; then
    missing=""; for c in cpu memory pids; do grep -qw "$c" "$controllers" || missing="$missing $c"; done
    [ -z "$missing" ] && ok "cgroup v2 delegates cpu memory pids" || fail "cgroup controllers not delegated:$missing (resource caps would be silently ignored)"
  else warn "cannot read $controllers: check cgroup delegation by hand (docker info warnings)"; fi
else fail "docker daemon not reachable with the current DOCKER_HOST"; fi

# --- .env: names only, never values ------------------------------------------------------------------
if [ -f .env ]; then
  grep -Eq '^GOOGLE_API_KEY=.+' .env && ok ".env has GOOGLE_API_KEY (value not shown)" || fail ".env has no non-empty GOOGLE_API_KEY (needed for the Vertex headline run)"
  grep -q '^REPOLACE_BENCH_GITHUB_TOKEN' .env && fail ".env contains REPOLACE_BENCH_GITHUB_TOKEN: it must never be in .env" || ok "bench token is not in .env"
else fail ".env not found in the repository root"; fi

# --- database and instances --------------------------------------------------------------------------
if command -v uv >/dev/null; then
  out=$(uv run --all-packages python - "$MODEL" <<'PY' 2>&1
import asyncio, sys, pathlib
from types import SimpleNamespace
model = sys.argv[1]
problems, notes = [], []

from repolace_shared.config import SharedSettings
from sqlalchemy import text
from repolace_shared.db.session import create_engine

async def db():
    engine = create_engine(SharedSettings().database_url)
    try:
        async with engine.connect() as c:
            version = (await c.execute(text("select version_num from alembic_version"))).scalar()
            repos = (await c.execute(text(
                "select count(*) from registered_repos where full_name like 'repolace/bench-%' and is_active"))).scalar()
        return version, repos
    finally:
        await engine.dispose()

try:
    version, repos = asyncio.run(db())
    heads = sorted(p.stem.split("_")[0] for p in pathlib.Path("api/alembic/versions").glob("0*.py"))
    if version == heads[-1]:
        notes.append(f"database migrated to {version}")
    else:
        problems.append(f"database is at migration {version}, head is {heads[-1]}: run `cd api && uv run alembic upgrade head` (back it up first)")
    instances = len(list(pathlib.Path("eval/instances").glob("*.json")))
    if repos == instances and instances > 0:
        notes.append(f"{repos} bench repos registered, matching {instances} instance files")
    else:
        problems.append(f"{repos} active bench repos registered but {instances} instance files")
except Exception as exc:
    problems.append(f"database check failed: {type(exc).__name__}")

try:
    from repolace_gateway.client import LLMClient
    from repolace_gateway.config import GatewaySettings, load_config
    settings = GatewaySettings()
    config = load_config(settings.models_path, {"agent": model})
    client = LLMClient(config, settings, SimpleNamespace(redactor=None))
    for stage in ("agent", "resolve"):
        client.preflight(stage)
    notes.append(f"{model}: priced and keyed for the agent and resolve stages (no call made)")
except Exception as exc:
    problems.append(f"model preflight failed: {type(exc).__name__}: {str(exc)[:160]}")

for n in notes: print("ok    " + n)
for p in problems: print("FAIL  " + p)
PY
)
  printf '%s\n' "$out" | grep -E '^(ok|FAIL|warn) ' || { fail "python checks did not run: ${out:0:200}"; }
  failures=$((failures + $(printf '%s\n' "$out" | grep -c '^FAIL ')))
fi

# --- the tree -----------------------------------------------------------------------------------------
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then warn "tracked files are modified: repolace-eval enqueue refuses a dirty tree (--allow-dirty-tree overrides)"; else ok "tracked tree is clean"; fi
[ "$(git branch --show-current)" = "main" ] && ok "on main" || warn "on branch $(git branch --show-current), not main: the manifest records this commit"

echo
if [ "$failures" -eq 0 ]; then echo "preflight passed"; else echo "preflight FAILED: $failures problem(s)"; fi
[ "$failures" -eq 0 ]
