// What 18 bugs can and cannot detect, and the retrieval grid with its switch rule.
import { $, el, figure, buildTable, pct, pp, attachTip, trow, esc, RUN_META } from "./core.js";

// Normal approximation used for the minimum detectable gap in the methods list: se of a difference = sqrt(2) * se.
const phi = (z) => 0.5 * (1 + erf(z / Math.SQRT2));
function erf(x) { const s = Math.sign(x); x = Math.abs(x); const t = 1 / (1 + 0.3275911 * x); const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x); return s * y; }
const power = (gap, se) => { const z = gap / (Math.SQRT2 * se); return phi(z - 1.96) + phi(-z - 1.96); };

export function renderPower(D) {
  const base = D.runs.baseline;
  const se = (base.power.bootstrap_se_pp ?? 9.9) / 100;
  const mde = D.d.power.approx_minimum_detectable_gap_pp_two_independent_runs;
  const gaps = ["sweep-e1", "sweep-r2", "sweep-m1"].map((id) => ({ id, gap: Math.abs(D.cmp[id].variant_rate - D.cmp[id].baseline_rate) }));

  const api = figure($("#fig-power"), {
    id: "power",
    title: "Chance of detecting a real gap, by the size of the gap",
    sub: `Two independent runs of 18 bugs; standard error ${(se * 100).toFixed(1)} points; 5% two-sided test`,
    caption: `Power reaches 80% at about ${mde} points. The three variants' observed gaps are marked on the curve: each sits where a real difference of that size would be missed most of the time. A non-significant result here is therefore not evidence that a variant is equal.`,
    table: () => ({
      caption: "Power at selected gaps (normal approximation).",
      cols: [{ key: "g", label: "True gap" }, { key: "p", label: "Power" }],
      rows: [0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 60].map((g) => ({ g: g + " points", p: pct(power(g / 100, se), 0) })),
    }),
    draw: (plot) => {
      const d3 = window.d3;
      const w = Math.floor(plot.getBoundingClientRect().width), narrow = w < 560;
      const m = { l: 46, r: 16, t: 18, b: 44 }, H = narrow ? 290 : 340;
      const iw = w - m.l - m.r, ih = H - m.t - m.b;
      const x = d3.scaleLinear([0, 0.6], [0, iw]), y = d3.scaleLinear([0, 1], [ih, 0]);
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "img").attr("aria-label", `Power curve: 80 percent at a gap of about ${mde} points`);
      const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
      for (const t of [0, 0.25, 0.5, 0.75, 1]) { g.append("line").attr("class", "grid").attr("x1", 0).attr("x2", iw).attr("y1", y(t)).attr("y2", y(t)); g.append("text").attr("x", -8).attr("y", y(t) + 4).attr("text-anchor", "end").text(pct(t)); }
      for (let t = 0; t <= 60; t += 10) g.append("text").attr("x", x(t / 100)).attr("y", ih + 20).attr("text-anchor", "middle").text(t);
      g.append("line").attr("class", "axis").attr("x1", 0).attr("x2", iw).attr("y1", ih).attr("y2", ih);
      g.append("text").attr("x", iw / 2).attr("y", ih + 38).attr("text-anchor", "middle").text("true gap in pass rate, percentage points");
      const pts = d3.range(0, 0.6001, 0.005).map((v) => [v, power(v, se)]);
      g.append("path").attr("d", d3.area().x((p) => x(p[0])).y0(ih).y1((p) => y(p[1]))(pts)).attr("fill", "var(--accent)").attr("opacity", 0.1);
      g.append("path").attr("d", d3.line().x((p) => x(p[0])).y((p) => y(p[1]))(pts)).attr("fill", "none").attr("stroke", "var(--accent)").attr("stroke-width", 2.5);
      g.append("line").attr("x1", 0).attr("x2", x(mde / 100)).attr("y1", y(0.8)).attr("y2", y(0.8)).attr("stroke", "var(--ink)").attr("stroke-width", 1).attr("opacity", 0.55);
      g.append("line").attr("x1", x(mde / 100)).attr("x2", x(mde / 100)).attr("y1", y(0.8)).attr("y2", ih).attr("stroke", "var(--ink)").attr("stroke-width", 1).attr("opacity", 0.55);
      g.append("circle").attr("cx", x(mde / 100)).attr("cy", y(0.8)).attr("r", 6).attr("fill", "var(--ink)").attr("stroke", "var(--page)").attr("stroke-width", 2);
      g.append("text").attr("x", x(mde / 100) + (narrow ? -10 : 10)).attr("y", y(0.8) + (narrow ? -10 : 18)).attr("text-anchor", narrow ? "end" : "start").attr("class", "t-strong").text(`80% at about ${mde} points`);
      gaps.sort((a, b) => a.gap - b.gap);
      gaps.forEach((o, gi) => {
        const meta = RUN_META[o.id];
        const grp = g.append("g").attr("class", meta.cls).attr("tabindex", 0).attr("role", "img").attr("aria-label", `${meta.short}: observed gap ${(o.gap * 100).toFixed(1)} points, power ${pct(power(o.gap, se))}`);
        grp.append("circle").attr("cx", x(o.gap)).attr("cy", y(power(o.gap, se))).attr("r", 7).attr("fill", "var(--c)").attr("stroke", "var(--page)").attr("stroke-width", 2);
        grp.append("circle").attr("class", "hit").attr("cx", x(o.gap)).attr("cy", y(power(o.gap, se))).attr("r", 16);
        const left = gi % 2 === 0 && gi < 2;
        grp.append("text").attr("x", x(o.gap) + (gi < 2 ? (left ? -12 : 12) : 0)).attr("y", y(power(o.gap, se)) + (gi < 2 ? (left ? -10 : -14) : -14)).attr("text-anchor", gi < 2 ? (left ? "end" : "start") : "middle").attr("class", "t-strong").text(narrow ? meta.short.split(" ")[0] : meta.short);
        attachTip(grp.node(), () => `<b>${esc(meta.short)}</b>` + trow("Observed gap", (o.gap * 100).toFixed(1) + " points") + trow("Power for a gap this size", pct(power(o.gap, se))));
      });
      plot.replaceChildren(svg.node());
    },
  });
  return api;
}

export function renderRetrieval(D) {
  const r = D.d.retrieval;
  const host = $("#fig-retrieval");
  if (!r) { host.replaceChildren(el("p", { class: "fig-sub" }, "Retrieval grid not available.")); return; }
  const grid = r.tables["Reading the grid"];
  const strat = r.tables["By embedding strategy (all repositories pooled)"];
  const parse = (s) => { const m = s.match(/([+\-−][\d.]+) pp \[([+\-−][\d.]+), ([+\-−][\d.]+)\]/); return m ? m.slice(1).map((v) => parseFloat(v.replace("−", "-"))) : null; };
  const items = grid.rows.map((row) => ({ cell: row[0], n: row[1], est: parse(row[2]), meets: row[3], text: row[2] })).filter((i) => i.est);
  figure(host, {
    id: "retrieval",
    title: "Chunk retrieval quality against the default, by strategy",
    sub: "Change in chunk MRR with its 95% interval over bugs; the rule asks for at least +10 points with an interval that excludes 0",
    caption: "No strategy meets the switch rule, so the default (truncate) stays. The intervals are wide because only 18 bugs were scored.",
    table: () => ({
      caption: "Retrieval strategies, all repositories pooled.",
      cols: strat.header.map((h, i) => ({ key: "c" + i, label: h })),
      rows: strat.rows.map((row) => Object.fromEntries(row.map((v, i) => ["c" + i, v]))),
    }),
    draw: (plot) => {
      const d3 = window.d3;
      const w = Math.floor(plot.getBoundingClientRect().width), narrow = w < 560;
      const labW = narrow ? 128 : 230, rowH = 32, H = items.length * rowH + 52;
      const iw = w - labW - 16;
      const lo = Math.min(-25, d3.min(items, (i) => i.est[1]) - 2), hi = Math.max(25, d3.max(items, (i) => i.est[2]) + 2);
      const x = d3.scaleLinear([lo, hi], [0, iw]);
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", "Change in chunk MRR by retrieval strategy");
      const g = svg.append("g").attr("transform", `translate(${labW},0)`);
      for (let t = Math.ceil(lo / 10) * 10; t <= hi; t += 10) { g.append("line").attr("class", "grid").attr("x1", x(t)).attr("x2", x(t)).attr("y1", 6).attr("y2", H - 34); g.append("text").attr("x", x(t)).attr("y", H - 16).attr("text-anchor", "middle").text((t > 0 ? "+" : t < 0 ? "−" : "") + Math.abs(t)); }
      g.append("line").attr("x1", x(0)).attr("x2", x(0)).attr("y1", 6).attr("y2", H - 34).attr("stroke", "var(--ink)").attr("stroke-width", 1.5).attr("opacity", 0.7);
      g.append("line").attr("x1", x(10)).attr("x2", x(10)).attr("y1", 6).attr("y2", H - 34).attr("stroke", "var(--accent)").attr("stroke-width", 2);
      g.append("text").attr("x", x(10) + 6).attr("y", 16).attr("class", "t-strong").text("switch rule: +10");
      g.append("text").attr("x", iw / 2).attr("y", H - 1).attr("text-anchor", "middle").text("points of chunk MRR against truncate / truncate");
      items.forEach((it, i) => {
        const cy = 30 + i * rowH;
        const grp = svg.append("g").attr("tabindex", 0).attr("role", "img").attr("aria-label", `${it.cell}: ${it.text}, meets the rule: ${it.meets}`);
        grp.append("text").attr("x", 0).attr("y", cy + 4).text(narrow ? it.cell.replace(" / ", "/") : it.cell);
        const gm = grp.append("g").attr("transform", `translate(${labW},${cy})`);
        gm.append("line").attr("x1", x(it.est[1])).attr("x2", x(it.est[2])).attr("y1", 0).attr("y2", 0).attr("stroke", "var(--ink)").attr("stroke-width", 2.5).attr("stroke-linecap", "round").attr("opacity", 0.8);
        gm.append("circle").attr("cx", x(it.est[0])).attr("r", 6).attr("fill", "var(--accent)").attr("stroke", "var(--page)").attr("stroke-width", 2);
        gm.append("rect").attr("class", "hit").attr("x", -labW).attr("y", -rowH / 2).attr("width", iw + labW).attr("height", rowH);
        attachTip(grp.node(), () => `<b>${esc(it.cell)}</b>` + trow("Change", `${it.est[0] > 0 ? "+" : ""}${it.est[0]} pp`) + trow("95% interval", `${it.est[1]} to ${it.est[2]}`) + trow("Meets the rule", it.meets));
      });
      plot.replaceChildren(svg.node());
    },
  });
}
