#!/usr/bin/env python3
"""Export every benchmark run the report covers from Postgres into BENCHMARK/data/<run>/ (read-only).

Reads the compose Postgres through `docker exec ... psql` with the session forced read-only; it writes files and
never a row. Needs `DOCKER_HOST=unix:///run/user/<uid>/docker.sock` (the rootless daemon) in the environment.

Per run it writes tasks.csv, llm_stage.csv, calls.csv (one row per model call), tool_use.csv (derived from calls.csv),
test_runs.csv, manifest.json and counts.json (rows per status/outcome and the planned grid). Across runs it writes
`archived_spend.json` (attempts the re-queue watchers archived and deleted), `retrieval.json` (the retrieval-1
tables) and `site/data/code_versions.json` (which gateway retry window each row ran under).

    DOCKER_HOST=unix:///run/user/1000/docker.sock python3 -I BENCHMARK/analysis/export_runs.py
"""

import csv
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
REPO = BENCH.parent
# The sibling worktrees (`../repolace-early` ...) hold the manifests and git history of the variant sweeps.
ROOT = Path(os.environ.get("REPOLACE_ROOT", REPO.parent))
MAIN = ROOT / "repolace"

#: run id -> where its manifest lives, and the label the report uses. `kind` separates the baseline pair from the
#: variants so nothing downstream has to guess.
RUNS = {
    "sweep-4": {"label": "Single agent, Gemini 3.1 Pro", "kind": "baseline", "worktree": "repolace"},
    "sweep-4b": {"label": "Re-run of the 12 rows sweep-4 lost to 429", "kind": "baseline-rerun", "worktree": "repolace"},
    "sweep-5": {"label": "Single agent, Gemini 3.8 Flash", "kind": "model", "worktree": "repolace"},
    "sweep-r1": {"label": "Role split, Gemini 3.1 Pro (first attempt)", "kind": "variant", "worktree": "repolace-roles"},
    "sweep-r2": {"label": "Role split, Gemini 3.1 Pro", "kind": "variant", "worktree": "repolace-roles"},
    "sweep-e1": {"label": "Early-edit nudge, Gemini 3.1 Pro", "kind": "variant", "worktree": "repolace-early"},
    "sweep-m1": {"label": "Role split, Flash editor with Pro plan/review/debug", "kind": "variant", "worktree": "repolace-mixed"},
}
ARCHIVE = MAIN / "eval" / "runs" / "archive-2026-10-28"
EDIT_TOOLS = {"edit_file", "create_file"}
RUN_TOOLS = {"run_python", "run_tests"}
READ_TOOLS = {"read_file", "grep", "search_code", "list_dir"}

csv.field_size_limit(10**9)


def psql(sql):
    """Run one SELECT through the container's psql in a read-only session; return the CSV text."""
    cmd = [
        "docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", "-i", "repolace-postgres-1",
        "psql", "-U", "repolace", "-d", "repolace", "-v", "ON_ERROR_STOP=1", "-c",
        f"COPY ({sql}) TO STDOUT WITH CSV HEADER",
    ]
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        sys.exit(f"psql failed: {done.stderr.strip()[:400]}")
    return done.stdout


def rows_of(text):
    return list(csv.DictReader(io.StringIO(text)))


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def sql_in(runs):
    return "(" + ",".join(f"'{r}'" for r in runs) + ")"


# ------------------------------------------------------------------------------------------------- per-run tables
TASKS_SQL = """
select t.id as task_id, t.eval_run_id as run, t.instance_id, t.run_index, t.status, t.outcome,
       t.agent_stop_reason as stop_reason, t.score_reason, left(coalesce(t.error_message, ''), 160) as error_head,
       t.cost_usd, t.started_at, t.completed_at,
       round(extract(epoch from (t.completed_at - t.started_at))::numeric, 1) as latency_s,
       t.retry_count, coalesce(array_length(t.changed_files, 1), 0) as changed_files_n,
       (select count(*) from task_test_runs r where r.task_id = t.id and r.attempt >= 1) as scored_attempts
from tasks t where t.eval_run_id in {runs} order by t.eval_run_id, t.instance_id, t.run_index
"""

STAGE_SQL = """
select l.task_id, l.stage, count(*) as calls, coalesce(sum(l.input_tokens), 0) as input_tokens,
       coalesce(sum(l.cached_input_tokens), 0) as cached_input_tokens, coalesce(sum(l.output_tokens), 0) as output_tokens,
       coalesce(sum(l.cost_usd), 0) as cost_usd, coalesce(sum(l.latency_ms), 0) as latency_ms,
       count(l.error) as errors
from llm_calls l join tasks t on t.id = l.task_id where t.eval_run_id in {runs} group by l.task_id, l.stage
"""

CALLS_SQL = """
select l.task_id, row_number() over (partition by l.task_id order by l.created_at) as seq, l.stage, l.attempt,
       round(extract(epoch from (l.created_at - min(l.created_at) over (partition by l.task_id)))::numeric, 2) as t_offset_s,
       l.cost_usd, l.input_tokens, l.cached_input_tokens, l.output_tokens, l.latency_ms,
       (select string_agg(tc->'function'->>'name', ',') from jsonb_array_elements(
            coalesce(l.response->'choices'->0->'message'->'tool_calls', '[]'::jsonb)) tc) as tools
from llm_calls l join tasks t on t.id = l.task_id where t.eval_run_id in {runs} order by l.task_id, l.created_at
"""

TEST_RUNS_SQL = """
select r.task_id, r.attempt, coalesce(cardinality(r.passed), 0) as passed_n, coalesce(cardinality(r.failed), 0) as failed_n,
       r.exit_code, r.duration_seconds, (r.error is not null) as has_error
from task_test_runs r join tasks t on t.id = r.task_id where t.eval_run_id in {runs} order by r.task_id, r.attempt
"""


def tool_use(calls):
    """Per task: call index of the first edit, and tool-invocation counts, over the agent stage only."""
    by_task = {}
    for c in calls:
        if c["stage"] == "agent":
            by_task.setdefault(c["task_id"], []).append(c)
    out = []
    for task_id, cs in by_task.items():
        cs.sort(key=lambda c: int(c["seq"]))
        first_edit, counts = "", {"edit": 0, "run": 0, "read": 0, "submit": 0, "all": 0}
        for i, c in enumerate(cs, 1):
            for name in filter(None, (c["tools"] or "").split(",")):
                counts["all"] += 1
                if name in EDIT_TOOLS:
                    counts["edit"] += 1
                    first_edit = first_edit or i
                elif name in RUN_TOOLS:
                    counts["run"] += 1
                elif name in READ_TOOLS:
                    counts["read"] += 1
                elif name == "submit":
                    counts["submit"] += 1
        out.append({"task_id": task_id, "first_edit_call": first_edit, "edit_calls": counts["edit"],
                    "run_calls": counts["run"], "read_calls": counts["read"], "submit_calls": counts["submit"],
                    "tool_calls": counts["all"]})
    return out


def manifest_path(run):
    spec = RUNS[run]
    for base in (ROOT / spec["worktree"], MAIN):
        p = base / "eval" / "runs" / run / "manifest.json"
        if p.exists():
            return p
    frozen = BENCH / "data" / f"{run}-manifest.json"
    return frozen if frozen.exists() else None


def export_runs():
    runs = list(RUNS)
    where = sql_in(runs)
    tasks = rows_of(psql(TASKS_SQL.format(runs=where)))
    stage = rows_of(psql(STAGE_SQL.format(runs=where)))
    calls = rows_of(psql(CALLS_SQL.format(runs=where)))
    tests = rows_of(psql(TEST_RUNS_SQL.format(runs=where)))
    run_of = {t["task_id"]: t["run"] for t in tasks}
    counts_all = {}
    for run in runs:
        out = BENCH / "data" / run
        mine = [t for t in tasks if t["run"] == run]
        ids = {t["task_id"] for t in mine}
        write_csv(out / "tasks.csv", mine, list(tasks[0]))
        write_csv(out / "llm_stage.csv", [r for r in stage if r["task_id"] in ids], list(stage[0]))
        mine_calls = [c for c in calls if c["task_id"] in ids]
        write_csv(out / "calls.csv", mine_calls, list(calls[0]))
        write_csv(out / "tool_use.csv", tool_use(mine_calls),
                  ["task_id", "first_edit_call", "edit_calls", "run_calls", "read_calls", "submit_calls", "tool_calls"])
        write_csv(out / "test_runs.csv", [r for r in tests if r["task_id"] in ids], list(tests[0]))
        mp = manifest_path(run)
        manifest = json.loads(mp.read_text()) if mp else None
        if manifest:
            (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        by = {}
        for t in mine:
            key = f"{t['status']}/{t['outcome'] or '-'}"
            by[key] = by.get(key, 0) + 1
        grid = None
        if manifest:
            n = len(manifest.get("instance_ids", []))
            per = manifest.get("runs_per_instance", 0)
            grid = {"instances": n, "runs_per_instance": per, "planned": n * per,
                    "rows_subset": bool(manifest.get("rows"))}
        counts_all[run] = {"rows_in_db": len(mine), "by_status_outcome": by, "grid": grid,
                           "manifest_source": str(mp.relative_to(ROOT)) if mp else None}
        print(f"{run}: {len(mine)} rows, {len(mine_calls)} calls", file=sys.stderr)
    for run, c in counts_all.items():
        (BENCH / "data" / run / "counts.json").write_text(json.dumps(c, indent=2) + "\n")
    (BENCH / "data" / "run_counts.json").write_text(json.dumps(counts_all, indent=2) + "\n")
    del run_of


# ---------------------------------------------------------------------------------- archived (deleted) spend
def archive_rows(path):
    """Rows of an archive file that was appended to over several dumps: drop the repeated header lines."""
    if not path.exists():
        return []
    return [r for r in csv.DictReader(path.open()) if r.get("id") != "id" and r.get("task_id") != "task_id"]


def archived_spend():
    """What the re-queue watchers archived (and, for the second generation, deleted from the database).

    Generation 1 (`requeued-sweep-X.csv`, 2026-10-29): the task rows only. Whether their llm_calls were deleted is
    checked against the database below. Generation 2 (`requeued-sweep-X-llm_calls.csv`, 2026-10-31 and 2026-11-02):
    the llm_calls themselves, deleted from the database after the dump.
    """
    out = {}
    for sweep in ("e1", "r2", "m1"):
        run = f"sweep-{sweep}"
        gen1 = archive_rows(ARCHIVE / f"requeued-{run}.csv")
        gen2_tasks = archive_rows(ARCHIVE / f"requeued-{run}-tasks.csv")
        gen2_calls = archive_rows(ARCHIVE / f"requeued-{run}-llm_calls.csv")
        gen2_cost = sum(float(r["cost_usd"] or 0) for r in gen2_calls)
        gen1_ids = [r["id"] for r in gen1]
        # Are the first generation's llm_calls still in the database? Count calls older than the archived attempt's end.
        still_in_db = 0.0
        if gen1:
            cutoff = {r["id"]: r["completed_at"] or r["updated_at"] for r in gen1}
            for tid, ts in cutoff.items():
                text = psql(f"select coalesce(sum(cost_usd), 0) as c from llm_calls where task_id = '{tid}' "
                            f"and created_at <= '{ts}'")
                still_in_db += float(rows_of(text)[0]["c"])
        gen1_cost = sum(float(r["cost_usd"] or 0) for r in gen1)
        out[run] = {
            "generation_1": {"tasks": len(gen1), "task_cost_usd": round(gen1_cost, 4),
                            "of_which_calls_still_in_db_usd": round(still_in_db, 4),
                            "note": "task rows only were archived; 'still in db' is spend already inside the DB totals"},
            "generation_2": {"tasks": len({r['id'] for r in gen2_tasks}), "calls": len(gen2_calls),
                            "llm_cost_usd": round(gen2_cost, 4),
                            "note": "llm_calls archived then deleted: not in the DB totals"},
            "added_back_usd": round(gen2_cost + max(0.0, gen1_cost - still_in_db), 4),
            "task_ids": gen1_ids + [r["id"] for r in gen2_tasks],
        }
    (BENCH / "data" / "archived_spend.json").write_text(json.dumps(out, indent=2) + "\n")
    print("archived spend:", {k: v["added_back_usd"] for k, v in out.items()}, file=sys.stderr)


# --------------------------------------------------------------------------------------------- retrieval tables
def parse_table(lines):
    rows = []
    for ln in lines:
        if not ln.startswith("|") or set(ln.replace("|", "").strip()) <= set("-: "):
            continue
        rows.append([c.strip() for c in ln.strip().strip("|").split("|")])
    return rows


def retrieval():
    path = MAIN / "eval" / "runs" / "retrieval-1" / "report.md"
    text = path.read_text()
    sections = re.split(r"^## ", text, flags=re.M)
    out = {"source": str(path.relative_to(ROOT)), "tables": {}, "intro": sections[0].strip()}
    for sec in sections[1:]:
        title, _, body = sec.partition("\n")
        t = parse_table(body.splitlines())
        if t:
            out["tables"][title.strip()] = {"header": t[0], "rows": t[1:]}
        prose = [ln for ln in body.splitlines() if ln and not ln.startswith("|")]
        if prose:
            out["tables"].setdefault(title.strip(), {})["prose"] = " ".join(prose)
    (BENCH / "data" / "retrieval.json").write_text(json.dumps(out, indent=2) + "\n")
    print("retrieval tables:", list(out["tables"]), file=sys.stderr)


# --------------------------------------------------------------------------------------------- code versions
def git(*args, cwd):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return done.stdout.strip()


def retry_window(sha, cwd):
    text = git("show", f"{sha}:gateway/models.toml", cwd=cwd)
    m = re.search(r"^max_attempts\s*=\s*(\d+)", text, flags=re.M)
    return int(m.group(1)) if m else None


def code_versions():
    """Derive which gateway retry window each row ran under from its start time and the commit history.

    A task runs as its own process from the worktree's files at that moment, so the window is the worktree's
    `max_attempts` at `started_at`. The manifest sha is only the sha at enqueue time; a worktree can move on.
    """
    early = ROOT / "repolace-early"
    commits = {}
    for name, cwd in (("early", early), ("roles", ROOT / "repolace-roles"), ("mixed", ROOT / "repolace-mixed"), ("main", MAIN)):
        log = git("log", "--format=%H|%cI|%s", "-40", cwd=cwd).splitlines()
        commits[name] = [dict(zip(("sha", "committed_at", "subject"), ln.split("|", 2))) for ln in log]
    # the e1 retry-window commit and the file's own mtime (which equals the commit time: the edit was committed at once)
    e1_window_commit = git("log", "--format=%H|%cI", "-1", "--", "gateway/models.toml", cwd=early).split("|")
    e1_sha, e1_time = e1_window_commit[0], e1_window_commit[1]
    manifests = {}
    for run in RUNS:
        mp = manifest_path(run)
        if mp:
            m = json.loads(mp.read_text())
            sha = m.get("git_sha", "")
            cwd = ROOT / RUNS[run]["worktree"]
            manifests[run] = {"manifest_sha": sha[:10], "created_at": m.get("created_at"),
                              "retry_window_at_manifest_sha": retry_window(sha, cwd) or retry_window(sha, MAIN),
                              "subject": git("log", "-1", "--format=%s", sha, cwd=cwd) or git("log", "-1", "--format=%s", sha, cwd=MAIN)}
    started = rows_of(psql(
        "select eval_run_id as run, id as task_id, started_at from tasks "
        f"where eval_run_id in {sql_in(RUNS)} and started_at is not null"))
    boundary = datetime.fromisoformat(e1_time).astimezone(timezone.utc)
    per_row = {}
    for r in started:
        run = r["run"]
        if run == "sweep-e1":
            ts = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            window = 21 if ts >= boundary else 9
        else:
            window = manifests.get(run, {}).get("retry_window_at_manifest_sha")
        per_row.setdefault(run, {}).setdefault(window, 0)
        per_row[run][window] += 1
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "retry window = gateway max_attempts in the worktree files at the task's started_at; sweep-e1's worktree "
                  "moved on after enqueue (commit below), every other run's worktree stayed at its manifest sha",
        "e1_retry_window_commit": {"sha": e1_sha[:10], "committed_at": e1_time, "from": 9, "to": 21},
        "baseline_claim_check": {
            "plan_said": "the baseline ran on the shortest retry window",
            "found": "sweep-4's manifest sha f02ccb3c57 is the merge of PR #50 (b661a30), which already set max_attempts = 9; "
                     "sweep-4, sweep-4b, r2 and m1 all ran on 9 attempts. The plan's claim is WRONG: the baseline did not run "
                     "on a shorter window than r2 and m1. Only e1 rows started after the window commit ran on 21.",
        },
        "manifests": manifests,
        "rows_by_retry_window": per_row,
        "commit_logs": commits,
    }
    dest = BENCH / "site" / "data"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "code_versions.json").write_text(json.dumps(out, indent=2) + "\n")
    print("rows by retry window:", per_row, file=sys.stderr)


def main():
    if "DOCKER_HOST" not in os.environ:
        sys.exit("set DOCKER_HOST to the rootless daemon socket, e.g. unix:///run/user/1000/docker.sock")
    export_runs()
    archived_spend()
    retrieval()
    code_versions()


if __name__ == "__main__":
    main()
