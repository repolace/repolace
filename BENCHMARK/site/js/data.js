// Load report.json and derive what the figures share. Every number on the page comes from this file.

export async function loadData() {
  const res = await fetch("data/report.json", { cache: "no-cache" });
  if (!res.ok) throw new Error("report.json: HTTP " + res.status);
  return res.json();
}

export function derive(d) {
  const runs = Object.fromEntries(d.runs.map((r) => [r.id, r]));
  const cmp = Object.fromEntries(d.comparisons.map((c) => [c.variant, c]));
  const taskIndex = {};
  for (const r of d.runs) {
    const m = new Map();
    for (const t of r.tasks) m.set(t.instance_id + "#" + t.run_index, t);
    taskIndex[r.id] = m;
  }
  const task = (runId, inst, idx) => taskIndex[runId]?.get(inst + "#" + idx);
  const instances = runs.baseline.instances.map((i) => i.instance_id);
  const diff = Object.fromEntries((d.difficulty?.instances || []).map((i) => [i.instance_id, i]));
  const sortedInstances = [...runs.baseline.instances].sort(
    (a, b) => b.passes - a.passes || a.repo.localeCompare(b.repo) || a.instance_id.localeCompare(b.instance_id),
  );
  return { d, runs, cmp, task, instances, diff, sortedInstances };
}

// The pre-registered pairing, recomputed from the task rows so the page can check the numbers it was given.
export function pairCounts(D, variantId) {
  let both = 0, baseOnly = 0, varOnly = 0, none = 0;
  const tiles = [];
  for (const inst of D.instances) {
    const b = D.task("baseline", inst, 0)?.bucket === "passed";
    const v = D.task(variantId, inst, 0)?.bucket === "passed";
    const kind = b && v ? "both" : b ? "base" : v ? "var" : "none";
    tiles.push({ inst, kind });
    if (kind === "both") both++; else if (kind === "base") baseOnly++; else if (kind === "var") varOnly++; else none++;
  }
  const order = { both: 0, base: 1, var: 2, none: 3 };
  tiles.sort((x, y) => order[x.kind] - order[y.kind] || x.inst.localeCompare(y.inst));
  return { both, baseOnly, varOnly, none, tiles };
}

export const STOP_LABEL = {
  submitted: "Submitted",
  step_cap: "Hit the step cap",
  no_change: "Ended with no change",
  budget_wall: "Ran out of time",
  llm_error: "Model error",
  lost: "Lost or unfinished",
};
export const STOP_ORDER = ["submitted", "step_cap", "no_change", "budget_wall", "llm_error", "lost"];
