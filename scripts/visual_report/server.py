"""Local visual report for the Phase 1 benchmark. Usage: server.py [port]. Binds 127.0.0.1 only.

One server, several routes: / (index), /e1 /r2 /m1 (live per-sweep views, refreshed every 15 s), /report (the visual report).
Serves BENCHMARK/analysis/results.json, the SVG figures, the retrieval grid tables parsed from
eval/runs/retrieval-1/report.md, and a live count of queued/running/finished tasks per sweep read through the
compose Postgres container (needs DOCKER_HOST on the rootless socket; the live panel is omitted if that fails).
Share it with `tailscale serve --bg <port>`.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
SWEEPS = {"e1": "sweep-e1", "r2": "sweep-r2", "m1": "sweep-m1"}
INTERVAL = 15
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
BENCH = os.path.join(ROOT, "BENCHMARK")
RETRIEVAL = os.path.join(ROOT, "eval", "runs", "retrieval-1", "report.md")


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def table_after(text, heading):
    """Rows of the first markdown table under `## heading`, as lists of cells."""
    m = re.search(r"^## " + re.escape(heading) + r".*?$(.*?)(?=^## |\Z)", text, re.DOTALL | re.MULTILINE)
    rows = []
    for line in (m.group(1) if m else "").splitlines():
        if line.startswith("|") and not re.match(r"^\|[-| ]+\|$", line):
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def retrieval():
    try:
        with open(RETRIEVAL) as f:
            text = f.read()
    except OSError:
        return None
    return {"by_strategy": table_after(text, "By embedding strategy"), "switch_rule": table_after(text, "Reading the grid")}


def live():
    sql = ("select eval_run_id||'|'||status::text||'|'||count(*) from tasks "
           "where eval_run_id in ('sweep-4','sweep-4b','sweep-5','sweep-r1','sweep-r2','sweep-e1','sweep-m1') group by eval_run_id, status order by 1")
    env = dict(os.environ, DOCKER_HOST=os.environ.get("DOCKER_HOST", "unix:///run/user/1000/docker.sock"))
    try:
        out = subprocess.run(["docker", "exec", "repolace-postgres-1", "psql", "-U", "repolace", "-d", "repolace", "-At", "-c", sql],
                             capture_output=True, text=True, timeout=15, env=env, check=False)
        if out.returncode:
            return None
        sweeps = {}
        for line in out.stdout.splitlines():
            run, status, n = line.split("|")
            sweeps.setdefault(run, {})[status] = int(n)
        return sweeps
    except (OSError, subprocess.TimeoutExpired):
        return None


SQL = """select coalesce(json_agg(r),'[]') from (
 select split_part(instance_id,'__',2) as instance, run_index as run, status::text as status,
  coalesce(outcome::text,'') as outcome, coalesce(agent_stop_reason::text,'') as stop,
  coalesce(error_message,'') as error,
  round(coalesce((select sum(cost_usd) from llm_calls l where l.task_id=t.id),0)::numeric,3)::float as cost,
  (select count(*) from llm_calls l where l.task_id=t.id) as calls,
  (select s.stage from task_stages s where s.task_id=t.id order by s.started_at desc limit 1) as stage,
  (select extract(epoch from s.started_at)::bigint from task_stages s where s.task_id=t.id order by s.started_at desc limit 1) as stage_started,
  (select s.status from task_stages s where s.task_id=t.id order by s.started_at desc limit 1) as stage_status,
  extract(epoch from started_at)::bigint as started, extract(epoch from completed_at)::bigint as completed
 from tasks t where eval_run_id='%s' order by instance_id, run_index) r"""




def sweep_query(run):
    env = dict(os.environ, DOCKER_HOST=os.environ.get("DOCKER_HOST", "unix:///run/user/1000/docker.sock"))
    out = subprocess.run(["docker", "exec", "repolace-postgres-1", "psql", "-U", "repolace", "-d", "repolace", "-At", "-c", SQL % run],
                         capture_output=True, text=True, timeout=20, env=env, check=False)
    if out.returncode:
        raise RuntimeError(out.stderr.strip()[:300])
    return json.loads(out.stdout)


def mem():
    with open("/proc/meminfo") as f:
        kb = {line.split(":")[0]: int(line.split()[1]) for line in f}
    return {"avail_gb": round(kb["MemAvailable"] / 1048576, 1), "total_gb": round(kb["MemTotal"] / 1048576, 1),
            "load": os.getloadavg()[0]}



CACHE = {k: json.dumps({"error": "first fetch pending"}) for k in SWEEPS}


def refresh_forever():
    while True:
        for key, run in SWEEPS.items():
            try:
                CACHE[key] = json.dumps({"run": run, "tasks": sweep_query(run), "mem": mem(), "fetched": int(time.time())})
            except Exception as e:  # noqa: BLE001 -- keep the last good data and say the refresh failed, whatever broke
                prev = json.loads(CACHE[key])
                CACHE[key] = json.dumps({**prev, "stale_error": str(e)} if "tasks" in prev else {"error": str(e)})
        time.sleep(INTERVAL)


INDEX = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>repolace sweeps</title>
<style>:root{--bg:#f6f7f9;--fg:#1c2430;--card:#fff;--line:#e4e8ee;--acc:#2f6fdb}@media(prefers-color-scheme:dark){:root{--bg:#0d1218;--fg:#e6eaf0;--card:#151c25;--line:#2a3340;--acc:#6aa0ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}main{max-width:640px;margin:0 auto;padding:24px 16px}
a{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:10px 0;color:var(--acc);text-decoration:none;font-weight:600}
a small{display:block;color:inherit;opacity:.65;font-weight:400}</style><main><h1>repolace sweeps</h1>
<a href="/e1/">sweep-e1<small>early-edit nudge, Pro, single agent</small></a>
<a href="/r2/">sweep-r2<small>role split with the planner hand-over fix, Pro</small></a>
<a href="/m1/">sweep-m1<small>mixed models: Flash editor, Pro plan/review/debug</small></a>
<a href="/report/">Visual report<small>sweep-4 + 4b results, statistics, retrieval grid</small></a></main>"""


class H(BaseHTTPRequestHandler):
    def send(self, body, ctype, code=200):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to):
        self.send_response(301)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            return self.send(INDEX, "text/html; charset=utf-8")
        m = re.fullmatch(r"/(e1|r2|m1|report)(/.*)?", p)
        if not m:
            return self.send("not found", "text/plain", 404)
        name, rest = m.group(1), m.group(2)
        if rest is None:
            return self.redirect(f"/{name}/")
        if name == "report":
            if rest == "/":
                return self.send(read_bytes(os.path.join(HERE, "index.html")), "text/html; charset=utf-8")
            if rest == "/api":
                res = json.loads(read_bytes(os.path.join(BENCH, "analysis", "results.json")))
                return self.send(json.dumps({"results": res, "retrieval": retrieval(), "live": live()}), "application/json")
            f = re.fullmatch(r"/figures/([a-z_]+\.svg)", rest)
            if f and os.path.exists(os.path.join(BENCH, "figures", f.group(1))):
                return self.send(read_bytes(os.path.join(BENCH, "figures", f.group(1))), "image/svg+xml")
            return self.send("not found", "text/plain", 404)
        if rest == "/":
            return self.send(read_bytes(os.path.join(HERE, "sweep.html")), "text/html; charset=utf-8")
        if rest == "/api":
            return self.send(CACHE[name], "application/json")
        return self.send("not found", "text/plain", 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"http://127.0.0.1:{PORT}")
    threading.Thread(target=refresh_forever, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
