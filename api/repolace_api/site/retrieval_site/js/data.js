// Load retrieval.json and give both states (ready, awaiting) the same shape where the figures can share code.
export async function loadData() {
  const res = await fetch("data/retrieval.json", { cache: "no-cache" });
  if (!res.ok) throw new Error("retrieval.json: HTTP " + res.status);
  return res.json();
}

const KS = [5, 10, 20];

export function derive(d) {
  const ready = d.status === "ready";
  let cells, deltas, n;
  if (ready) {
    cells = d.cells.map((c) => ({ key: c.key, metrics: c.metrics, scored: { chunk: c.metrics.chunk_mrr.scored, file: c.metrics.file_mrr.scored } }));
    deltas = (m) => d.comparisons[m]?.rows || [];
    n = d.meta.instances;
  } else {
    const r = d.aggregate;
    const names = ["chunk_mrr", "file_mrr", ...["chunk", "file"].flatMap((k) => KS.map((x) => `${k}_r${x}`))];
    cells = r.cells.map((c) => ({
      key: c.key, scored: { chunk: c.chunk_scored, file: c.file_scored },
      metrics: Object.fromEntries(names.map((m) => [m, { mean: c[m], ci: null, scored: m.startsWith("file") ? c.file_scored : c.chunk_scored }])),
    }));
    deltas = (m) => r.deltas.filter((x) => (m === "chunk_mrr" ? x.chunk_mrr_pp : m === "file_mrr" ? x.file_mrr_pp : null)).map((x) => {
      const t = m === "chunk_mrr" ? x.chunk_mrr_pp : x.file_mrr_pp;
      return { cell: x.cell, paired: x.paired, mean_pp: t[0], ci_pp: [t[1], t[2]], meets_switch_rule: m === "chunk_mrr" && x.meets_switch_rule, p: null, p_holm: null, dz: null };
    });
    n = r.instances;
  }
  const keys = cells.map((c) => c.key);
  const byKey = Object.fromEntries(cells.map((c) => [c.key, c]));
  const prim = deltas("chunk_mrr");
  const anyMet = ready ? d.switch_rule.any_met : prim.some((x) => x.meets_switch_rule);
  const best = prim.reduce((a, b) => (a == null || (b.mean_pp ?? -1e9) > (a.mean_pp ?? -1e9) ? b : a), null);
  const mde = ready ? d.power.mde_pp_median_sd : d.aggregate.mde_pp_median;
  return { d, ready, cells, byKey, keys, deltas, n, anyMet, best, mde, minGain: d.design.min_gain_pp, comps: d.design.comparisons };
}
