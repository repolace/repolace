// The grid as measured: recall@k curves, one small multiple per index strategy, then the whole table.
import { $, el, figure, attachTip, trow, buildTable } from "/benchmark/js/core.js";
import { INDEX, IX_COLOR, IX_PLAIN, colorOf, hollow, parse, ppct, ci, mark, isBase, fnum, legend } from "./shared.js";

const KS = [5, 10, 20];

export function renderGrid(D) {
  $("#legend").replaceChildren(legend());
  let kind = "chunk";
  const sel = el("select", { class: "sel", "aria-label": "Recall kind" },
    el("option", { value: "chunk" }, "Chunk recall (gold hunks)"), el("option", { value: "file" }, "File recall (gold files)"));
  sel.addEventListener("change", () => { kind = sel.value; api.redraw(); });
  const val = (key, k) => D.byKey[key].metrics[`${kind}_r${k}`];

  const api = figure($("#fig-curves"), {
    id: "curves",
    title: "Recall at 5, 10 and 20 chunks",
    sub: D.ready ? "Mean over the bugs, with the 95% interval over bugs. The grey line is the baseline, truncate / truncate." : "Mean over the bugs from the first run (retrieval-1). Intervals arrive with the per-bug run.",
    tools: [sel],
    caption: "Each panel holds one index strategy; the solid line is queried with truncate, the dashed line with head_tail. The search returns 20 chunks, so file recall at k is over the files of the first k chunks, not the first k files. All panels share one vertical scale.",
    table: () => ({
      caption: `${kind === "chunk" ? "Chunk" : "File"} recall by cut-off.`,
      cols: [{ key: "c", label: "Index / query" }, ...KS.map((k) => ({ key: "k" + k, label: `R@${k} [95% interval]` })), { key: "n", label: "Scored" }],
      rows: D.keys.map((key) => ({
        c: key, n: `${D.byKey[key].scored[kind]} of ${D.n}`,
        ...Object.fromEntries(KS.map((k) => [`k${k}`, `${ppct(val(key, k).mean)}${val(key, k).ci ? " [" + ci(val(key, k).ci) + "]" : ""}`])),
      })),
    }),
    draw: (plot) => {
      const d3 = window.d3;
      const grid = el("div", { class: "small-multiples" });
      plot.replaceChildren(grid);
      const ymax = Math.min(1, Math.ceil(Math.max(...D.keys.flatMap((key) => KS.map((k) => (val(key, k).ci?.[1] ?? val(key, k).mean) ?? 0))) * 10 + 0.01) / 10);
      for (const s of INDEX) {
        const panel = el("div", {}, el("h4", {}, el("span", { class: "sw", style: `background:${IX_COLOR[s]}` }), s));
        grid.append(panel);
        const w = Math.max(160, Math.floor(panel.getBoundingClientRect().width));
        const H = 190, m = { l: 34, r: 8, t: 8, b: 24 };
        const x = d3.scalePoint(KS, [m.l + 14, w - m.r - 14]);
        const y = d3.scaleLinear([0, ymax], [H - m.b, m.t]);
        const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", `${s}: recall at 5, 10 and 20`);
        for (const t of y.ticks(4)) {
          svg.append("line").attr("class", "grid").attr("x1", m.l).attr("x2", w - m.r).attr("y1", y(t)).attr("y2", y(t));
          svg.append("text").attr("x", m.l - 6).attr("y", y(t) + 4).attr("text-anchor", "end").text(Math.round(t * 100) + "%");
        }
        for (const k of KS) svg.append("text").attr("x", x(k)).attr("y", H - 6).attr("text-anchor", "middle").text("@" + k);
        const base = "truncate / truncate";
        if (s !== "truncate") svg.append("path").attr("class", "ghost").attr("d", d3.line().x((p) => x(p.k)).y((p) => y(p.v))(KS.map((k) => ({ k, v: val(base, k).mean }))));
        for (const q of ["truncate", "head_tail"]) {
          const key = `${s} / ${q}`;
          if (!D.byKey[key]) continue;
          const off = q === "truncate" ? -4 : 4;
          const pts = KS.map((k) => ({ k, ...val(key, k) }));
          svg.append("path").attr("class", `lineseries ${q === "head_tail" ? "dash" : ""}`).attr("stroke", colorOf(key))
            .attr("d", d3.line().x((p) => x(p.k) + off).y((p) => y(p.mean))(pts));
          for (const p of pts) {
            if (p.ci) svg.append("line").attr("stroke", colorOf(key)).attr("stroke-width", 1.5).attr("opacity", 0.55).attr("x1", x(p.k) + off).attr("x2", x(p.k) + off).attr("y1", y(p.ci[0])).attr("y2", y(p.ci[1]));
            const dot = mark(svg, key, x(p.k) + off, y(p.mean), 4.5);
            dot.attr("tabindex", 0).attr("role", "img").attr("aria-label", `${key}, recall at ${p.k}: ${ppct(p.mean)}`);
            attachTip(dot.node(), () => `<b>${key}</b>` + trow(`R@${p.k}`, ppct(p.mean)) + (p.ci ? trow("95% interval", ci(p.ci)) : "") + trow("scored", `${p.scored} of ${D.n}`));
          }
        }
        panel.append(svg.node());
      }
    },
  });
  void api;

  // the full table, always visible: every cell, every metric, with its denominator
  const cols = [{ key: "c", label: "Index / query" }, { key: "n", label: "Scored: file / chunk" },
    ...["chunk_mrr", "file_mrr"].map((m) => ({ key: m, label: m === "chunk_mrr" ? "Chunk MRR" : "File MRR" })),
    ...["chunk", "file"].flatMap((kd) => KS.map((k) => ({ key: `${kd}_r${k}`, label: `${kd} R@${k}` })))];
  const rows = D.keys.map((key) => {
    const c = D.byKey[key];
    const cellTxt = (m) => { const v = c.metrics[m]; return v.mean == null ? "n/a" : ppct(v.mean) + (v.ci ? ` [${ppct(v.ci[0], 0)}, ${ppct(v.ci[1], 0)}]` : ""); };
    return { c: key + (isBase(key) ? " (baseline)" : ""), n: `${c.scored.file} / ${c.scored.chunk} of ${D.n}`, ...Object.fromEntries(cols.slice(2).map((col) => [col.key, cellTxt(col.key)])) };
  });
  $("#fig-table").replaceChildren(el("h3", {}, "Every cell, every metric"), buildTable({
    caption: D.ready ? "Mean with the 95% interval over bugs in brackets. MRR counts only the top 20 chunks: a miss scores 0." : "Means from the first run (retrieval-1), without intervals. MRR counts only the top 20 chunks: a miss scores 0.",
    cols, rows,
  }));
}
