#!/usr/bin/env python3
"""Export the one real run shown at /demo/ to api/repolace_api/site/demo_site/data/demo.json.

    python docs/tools/export_demo.py            # write the JSON
    python docs/tools/export_demo.py --check    # exit 1 if the committed JSON differs from a fresh export

Needs the compose Postgres (container `repolace-postgres-1`, on the rootless Docker daemon) and read-only
`select`s only. The user and database name are parsed in memory from DATABASE_URL (the environment, else the
repository's `.env`); nothing from that file is printed or stored. Because it needs the database, the freshness
check SKIPS (exit 0, with a message) when the database is unreachable, and it is NOT part of CI.

What is left out on purpose: the full task id (only its first 8 hex digits), who claimed the task, error
messages and stage errors, workspace paths, container names, heartbeats, user ids, any email or login,
timestamps finer than a date, and the node ids of tests (only counts are exported).
Standard library only.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "api" / "repolace_api" / "site" / "demo_site" / "data" / "demo.json"
TASK_ID = "4f381d8e-ef4f-4ba4-be31-1c513f9d2e3b"
# The second run of the same issue, made so the walkthrough video could be recorded as a real screen capture.
VIDEO_TASK_ID = "9f248ba3-2be4-4ebb-b68c-552871882a5d"
VIDEO_PR_NOTE = "closed after the recording, so the issue has one open pull request"
CONTAINER = os.environ.get("DEMO_PG_CONTAINER", "repolace-postgres-1")
SCHEMA_VERSION = 1


class Unreachable(Exception):
    pass


def _env_files() -> list[Path]:
    files = [ROOT / ".env"]
    try:
        common = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"], capture_output=True, text=True, check=True
        ).stdout.strip()
        files.append((ROOT / common).resolve().parent / ".env")
    except (OSError, subprocess.CalledProcessError):
        pass
    return files


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    for path in _env_files():
        if path.is_file():
            m = re.search(r"^DATABASE_URL=(.*)$", path.read_text(), re.MULTILINE)
            if m:
                return m.group(1).strip().strip("\"'")
    raise Unreachable("DATABASE_URL not found")


def _docker_env() -> dict[str, str]:
    env = dict(os.environ)
    sock = Path(f"/run/user/{os.getuid()}/docker.sock")
    if "DOCKER_HOST" not in env and sock.exists():
        env["DOCKER_HOST"] = f"unix://{sock}"
    return env


def query(sql: str) -> str:
    parts = urlsplit(_database_url())
    user, db = parts.username, parts.path.lstrip("/")
    if not user or not db:
        raise Unreachable("DATABASE_URL has no user or database")
    try:
        done = subprocess.run(
            ["docker", "exec", CONTAINER, "psql", "-U", user, "-d", db, "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
            check=False, env=_docker_env(), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unreachable(type(exc).__name__) from exc
    if done.returncode != 0:
        raise Unreachable("psql failed (is the compose Postgres up?)")
    return done.stdout.strip()


SQL = """
select json_build_object(
  'status', t.status, 'outcome', t.outcome, 'stop_reason', t.agent_stop_reason, 'cost', t.cost_usd,
  'started', to_char(t.started_at at time zone 'UTC', 'YYYY-MM-DD'),
  'duration', extract(epoch from (t.completed_at - t.started_at)),
  'score_reason', t.score_reason, 'pr_url', t.pr_url, 'issue_number', t.issue_number,
  'issue_title', t.issue_title, 'issue_body', t.issue_body, 'issue_url', t.issue_url, 'diff', t.patch_diff,
  'repo', (select r.full_name from registered_repos r where r.id = t.repo_id),
  'stages', (select coalesce(json_agg(json_build_object('stage', s.stage, 'status', s.status,
      'seconds', extract(epoch from (s.ended_at - s.started_at))) order by s.started_at), '[]'::json)
      from task_stages s where s.task_id = t.id),
  'tests', (select coalesce(json_agg(json_build_object('attempt', x.attempt, 'passed', cardinality(x.passed),
      'failed', cardinality(x.failed), 'errors', cardinality(x.collect_failures), 'seconds', x.duration_seconds)
      order by x.attempt), '[]'::json) from task_test_runs x where x.task_id = t.id),
  'calls', (select coalesce(json_agg(json_build_object('stage', c.stage, 'model', c.model,
      'input', c.input_tokens, 'cached', c.cached_input_tokens, 'output', c.output_tokens, 'cost', c.cost_usd)
      order by c.created_at, c.id), '[]'::json) from llm_calls c where c.task_id = t.id)
) from tasks t where t.id = '{task_id}'
"""


VIDEO_SQL = """
select json_build_object(
  'status', t.status, 'outcome', t.outcome, 'stop_reason', t.agent_stop_reason, 'cost', t.cost_usd,
  'duration', extract(epoch from (t.completed_at - t.started_at)), 'pr_url', t.pr_url,
  'calls', (select count(*) from llm_calls c where c.task_id = t.id)
) from tasks t where t.id = '{task_id}'
"""


def build_video_run(task_id: str = VIDEO_TASK_ID) -> dict:
    """Facts of the run the video shows (a second run of the same issue): counts and totals only, no diff."""
    raw = query(VIDEO_SQL.format(task_id=task_id))
    if not raw:
        raise Unreachable("video task not found")
    r = json.loads(raw)
    return {
        "task_short_id": task_id[:8],
        "status": r["status"],
        "outcome": r["outcome"],
        "stop_reason": r["stop_reason"],
        "cost_usd": round(float(r["cost"]), 4),
        "llm_call_count": int(r["calls"]),
        "duration_seconds": round(float(r["duration"]), 1),
        "pr_url": r["pr_url"],
        "pr_note": VIDEO_PR_NOTE,
    }


def build(task_id: str = TASK_ID) -> dict:
    raw = query(SQL.format(task_id=task_id))
    if not raw:
        raise Unreachable("task not found")
    r = json.loads(raw)
    calls = [
        {
            "n": i,
            "stage": c["stage"],
            "model": c["model"],
            "input_tokens": c["input"] or 0,
            "cached_input_tokens": c["cached"] or 0,
            "output_tokens": c["output"] or 0,
            "cost_usd": round(float(c["cost"] or 0), 8),
        }
        for i, c in enumerate(r["calls"], 1)
    ]
    models = {c["model"] for c in calls}
    reason = (r["score_reason"] or "").strip()
    head = reason.split(" (")[0] if reason else ""
    unc = " (uncurated)" if "uncurated" in reason else ""
    summary = f"{r['outcome']}{unc}: {head}" if head else str(r["outcome"])
    return {
        "schema_version": SCHEMA_VERSION,
        "task_short_id": task_id[:8],
        "repo": r["repo"],
        "issue": {"number": r["issue_number"], "title": r["issue_title"], "body": r["issue_body"] or "", "url": r["issue_url"]},
        "pr_url": r["pr_url"],
        "status": r["status"],
        "outcome": r["outcome"],
        "stop_reason": r["stop_reason"],
        "model": models.pop() if len(models) == 1 else "mixed",
        "cost_usd": round(float(r["cost"]), 4),
        "duration_seconds": round(float(r["duration"]), 1),
        "started_date": r["started"],
        "stages": [{"stage": s["stage"], "status": s["status"], "seconds": round(float(s["seconds"] or 0), 1)} for s in r["stages"]],
        "llm_calls": calls,
        "test_runs": [
            {
                "attempt": t["attempt"],
                "passed_count": t["passed"],
                "failed_count": t["failed"],
                "errors_count": t["errors"],
                "duration_seconds": round(float(t["seconds"] or 0), 1),
            }
            for t in r["tests"]
        ],
        "diff": r["diff"] or "",
        "score_reason_summary": summary,
        "video_run": build_video_run(),
    }


def render(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed JSON differs from a fresh export")
    args = ap.parse_args()
    try:
        fresh = render(build())
    except Unreachable as exc:
        if args.check:
            print(f"export_demo --check: SKIPPED, database not reachable ({exc}). Not part of CI.")
            return 0
        print(f"export_demo: database not reachable ({exc}).", file=sys.stderr)
        return 2
    if args.check:
        if OUT.is_file() and OUT.read_text() == fresh:
            print("export_demo --check: ok, demo.json matches a fresh export.")
            return 0
        print("export_demo --check: demo.json differs from a fresh export; run python docs/tools/export_demo.py.")
        return 1
    OUT.write_text(fresh)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
