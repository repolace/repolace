// Every run on one axis: pass rate and its interval over instances, the baseline band behind, and the zone this
// benchmark cannot resolve. Partial runs are shown, hatched and labelled, and never read as a rate.
import { $, el, figure, pct, attachTip, trow, ALL_ORDER, RUN_META } from "./core.js";

export function renderForest(D) {
  const d = D.d;
  const base = D.runs.baseline;
  const mde = (base.power?.approx_minimum_detectable_gap_pp_two_independent_runs ?? d.power.approx_minimum_detectable_gap_pp_two_independent_runs) / 100;
  const [blo, bhi] = base.headline.ci_bootstrap_instances;
  const rows = ALL_ORDER.map((id) => D.runs[id]).filter(Boolean);

  const label = (r) => RUN_META[r.id]?.name || r.label;
  figure($("#fig-forest"), {
    id: "forest",
    title: "Pass rate with its 95% interval over the bugs",
    sub: "Passed / planned; every row that did not pass counts against the run",
    caption: `The darker band is the baseline's own interval. The paler zone marks gaps of up to about ${Math.round(mde * 100)} points either side of the baseline: with 18 bugs, a run inside it has not been shown to differ. ` +
      `Open markers are not the same thing as full runs: sweep-4 is the baseline before its lost rows were re-run, and the two hatched runs are unfinished, so their rates are floors and are never compared.`,
    table: () => ({
      caption: "Pass rate per run, bootstrap over instances (percentile, 20,000 resamples).",
      cols: [{ key: "run", label: "Run" }, { key: "p", label: "Passed" }, { key: "rate", label: "Rate" }, { key: "ci", label: "95% interval over bugs" }, { key: "wil", label: "Row-level interval (context only, too narrow)" }, { key: "fin", label: "Finished" }, { key: "mark", label: "Status" }],
      rows: rows.map((r) => ({ run: label(r), p: `${r.headline.passed} / ${r.headline.planned}`, rate: pct(r.headline.rate, 1), ci: `${pct(r.headline.ci_bootstrap_instances[0], 1)} to ${pct(r.headline.ci_bootstrap_instances[1], 1)}`, wil: r.headline.wilson_rows_context_only ? `${pct(r.headline.wilson_rows_context_only[0], 1)} to ${pct(r.headline.wilson_rows_context_only[1], 1)}` : "n/a", fin: `${r.finished} of ${r.planned}`, mark: r.partial_mark || (r.kind === "baseline-substitution" ? "hand-stated substitution" : r.kind === "baseline-official" ? "12 rows lost to 429" : "complete") })),
    }),
    draw: (plot) => {
      const d3 = window.d3;
      const w = Math.floor(plot.getBoundingClientRect().width);
      const narrow = w < 620;
      const labW = narrow ? 136 : 250;
      const rowH = narrow ? 58 : 58;
      const gapBefore = (r, i) => (r.partial && !rows[i - 1]?.partial ? 26 : 0);
      let yy = 34;
      const ys = rows.map((r, i) => { yy += gapBefore(r, i); const y = yy + rowH / 2; yy += rowH; return y; });
      const H = yy + 36;
      const m = { l: labW, r: 14, t: 0, b: 0 };
      const iw = w - m.l - m.r;
      const x = d3.scaleLinear([0, 1], [0, iw]);
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", "Pass rate and interval for every run");
      const g = svg.append("g").attr("transform", `translate(${m.l},0)`);

      g.append("rect").attr("x", x(Math.max(0, base.headline.rate - mde))).attr("width", x(Math.min(1, base.headline.rate + mde)) - x(Math.max(0, base.headline.rate - mde))).attr("y", 22).attr("height", H - 22 - 28).attr("fill", "var(--ink)").attr("opacity", 0.045);
      g.append("rect").attr("x", x(blo)).attr("width", x(bhi) - x(blo)).attr("y", 22).attr("height", H - 22 - 28).attr("fill", "var(--s-baseline)").attr("opacity", 0.13);
      for (const t of [0, 0.25, 0.5, 0.75, 1]) {
        g.append("line").attr("class", "grid").attr("x1", x(t)).attr("x2", x(t)).attr("y1", 22).attr("y2", H - 28);
        g.append("text").attr("x", x(t)).attr("y", H - 10).attr("text-anchor", t === 0 ? "start" : t === 1 ? "end" : "middle").text(pct(t));
      }
      g.append("line").attr("x1", x(base.headline.rate)).attr("x2", x(base.headline.rate)).attr("y1", 22).attr("y2", H - 28).attr("stroke", "var(--s-baseline)").attr("stroke-width", 1.5).attr("opacity", 0.9);
      g.append("text").attr("x", x(base.headline.rate) + 6).attr("y", 14).attr("class", "t-strong").text(narrow ? "baseline" : "baseline 65%");
      const zoneL = Math.max(0, base.headline.rate - mde);
      if (!narrow && zoneL > 0.02) g.append("text").attr("x", x(zoneL) + 6).attr("y", 14).text("within the noise zone →");

      rows.forEach((r, i) => {
        const cy = ys[i];
        const [lo, hi] = r.headline.ci_bootstrap_instances;
        const part = r.partial, open = RUN_META[r.id]?.open;
        const meta = RUN_META[r.id];
        const grp = svg.append("g").attr("class", meta?.cls || "").attr("tabindex", 0).attr("role", "img")
          .attr("aria-label", `${label(r)}: ${r.headline.passed} of ${r.headline.planned}, ${pct(r.headline.rate, 1)}, interval ${pct(lo, 1)} to ${pct(hi, 1)}${r.partial_mark ? ", " + r.partial_mark : ""}`);
        // left label: the run name, then the figures
        const lab = grp.append("g").attr("transform", `translate(0,${cy})`);
        lab.append("text").attr("x", 0).attr("y", -3).attr("class", "t-strong").text(trunc(meta?.short || label(r), narrow ? 19 : 34));
        lab.append("text").attr("x", 0).attr("y", 14).text(`${r.headline.passed} of ${r.headline.planned} · ${pct(r.headline.rate)}` + (part ? "" : narrow ? "" : `  [${pct(lo)} to ${pct(hi)}]`));
        if (part) lab.append("text").attr("x", 0).attr("y", 29).attr("class", "t-strong").style("font-size", "11px").text(r.partial_mark);
        if (!part && r.kind === "baseline-official" && !narrow) lab.append("text").attr("x", 0).attr("y", 29).style("font-size", "11px").text("before the 12 lost rows were re-run");
        const c = "var(--c)";
        const gm = grp.append("g").attr("transform", `translate(${m.l},${cy})`).style("color", "inherit");
        const stroke = part ? "var(--s-partial)" : c;
        const line = gm.append("line").attr("x1", x(lo)).attr("x2", x(hi)).attr("y1", 0).attr("y2", 0).attr("stroke", stroke).attr("stroke-width", 3).attr("stroke-linecap", "round");
        if (part) line.attr("stroke-dasharray", "2 6");
        for (const v of [lo, hi]) gm.append("line").attr("x1", x(v)).attr("x2", x(v)).attr("y1", -7).attr("y2", 7).attr("stroke", stroke).attr("stroke-width", 2.5).attr("stroke-linecap", "round");
        gm.append("circle").attr("cx", x(r.headline.rate)).attr("cy", 0).attr("r", 7)
          .attr("fill", part || open ? "var(--page)" : c).attr("stroke", stroke).attr("stroke-width", part || open ? 2.5 : 2).attr("paint-order", "stroke");
        // 24px hit target spanning the whole row of the plot
        gm.append("rect").attr("class", "hit").attr("x", -m.l).attr("y", -rowH / 2).attr("width", iw + m.l).attr("height", rowH);
        const node = grp.node();
        attachTip(node, () => `<b>${esc2(label(r))}</b>` + trow("Passed", `${r.headline.passed} of ${r.headline.planned} (${pct(r.headline.rate, 1)})`) + trow("95% interval", `${pct(lo, 1)} to ${pct(hi, 1)}`) + trow("Finished", `${r.finished} of ${r.planned}`) + (r.partial_mark ? `<div>${r.partial_mark}: a floor, not compared</div>` : ""));
      });
      // marker for the unfinished group
      const firstPartial = rows.findIndex((r) => r.partial);
      if (firstPartial > 0) {
        const yTop = ys[firstPartial] - rowH / 2 - 14;
        svg.append("line").attr("class", "grid").attr("x1", 0).attr("x2", w).attr("y1", yTop - 6).attr("y2", yTop - 6);
        svg.append("text").attr("x", 0).attr("y", yTop + 8).attr("class", "t-strong").text("Unfinished runs: shown, not compared");
      }
      plot.replaceChildren(svg.node());
    },
  });
}

const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
const esc2 = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
