// The forest of paired gains against the switch-rule bar, and the power analysis behind how large a gain 18 bugs can show.
import { $, el, figure, attachTip, trow } from "/benchmark/js/core.js";
import { colorOf, hollow, mark, fpp, ciPP, pv, fnum, sign, isBase } from "./shared.js";

const MET = [["chunk_mrr", "Chunk MRR (the pre-registered metric)"], ["file_mrr", "File MRR"],
  ["chunk_r5", "Chunk R@5"], ["chunk_r10", "Chunk R@10"], ["chunk_r20", "Chunk R@20"], ["file_r5", "File R@5"], ["file_r10", "File R@10"], ["file_r20", "File R@20"]];

export function renderForest(D) {
  let metric = "chunk_mrr";
  const avail = D.ready ? MET : MET.slice(0, 2);
  const sel = el("select", { class: "sel", "aria-label": "Metric" }, avail.map(([v, l]) => el("option", { value: v }, l)));
  sel.addEventListener("change", () => { metric = sel.value; api.redraw(); });
  const rows = () => D.deltas(metric);
  const primary = () => metric === "chunk_mrr";

  const api = figure($("#fig-forest"), {
    id: "forest",
    title: "Paired gain over truncate / truncate, in percentage points",
    sub: "Each bug is compared with itself. Bars are the 95% interval over bugs.",
    tools: [sel],
    caption: `The dashed line is the pre-registered bar of ${D.minGain} percentage points, and a strategy only clears it if its whole interval is also above zero. ` +
      (D.mde != null ? `The pale band marks gains smaller than about ${Math.round(D.mde)} points, which 18 bugs could not reliably detect (80% power, see the power section). ` : "") +
      "Seven comparisons on the same few bugs: whichever looks best is a candidate, not a finding.",
    table: () => ({
      caption: `${MET.find((m) => m[0] === metric)[1]}: paired difference against truncate / truncate.`,
      cols: [{ key: "c", label: "Cell" }, { key: "n", label: "Paired" }, { key: "m", label: "Mean gain" }, { key: "ci", label: "95% interval" },
        ...(D.ready ? [{ key: "wl", label: "Better / worse / equal" }, { key: "dz", label: "d_z" }, { key: "p", label: "p (sign-flip)" }, { key: "h", label: "Holm-adjusted p" }] : []), { key: "r", label: "Meets the bar" }],
      rows: rows().map((r) => ({
        c: r.cell, n: r.paired, m: fpp(r.mean_pp), ci: ciPP(r.ci_pp),
        wl: D.ready ? `${r.wins} / ${r.losses} / ${r.ties}` : "", dz: r.dz == null ? "" : fnum(r.dz, 2), p: pv(r.p), h: pv(r.p_holm),
        r: primary() ? (r.meets_switch_rule ? "yes" : "no") : "not applicable",
      })),
      foot: D.ready ? "p-values come from a paired sign-flip permutation test and are exploratory; Holm adjusts across the seven comparisons of each metric. d_z is the mean difference over its standard deviation." : null,
    }),
    draw: (plot) => {
      const d3 = window.d3;
      const data = rows();
      const w = Math.floor(plot.getBoundingClientRect().width);
      const narrow = w < 640;
      const labW = narrow ? 140 : 200, rightW = narrow ? 0 : D.ready ? 150 : 110, rowH = 40, top = 34;
      const H = top + data.length * rowH + 34;
      const m = { l: labW, r: rightW + 14 };
      const iw = w - m.l - m.r;
      const lo = Math.min(-10, ...data.map((r) => r.ci_pp?.[0] ?? r.mean_pp)) - 2;
      const hi = Math.max(D.minGain + 4, ...data.map((r) => r.ci_pp?.[1] ?? r.mean_pp)) + 2;
      const x = d3.scaleLinear([lo, hi], [0, iw]).nice();
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", "Paired gain of each cell over the baseline");
      const g = svg.append("g").attr("transform", `translate(${m.l},0)`);
      if (primary() && D.mde != null) g.append("rect").attr("x", x(-D.mde)).attr("width", x(D.mde) - x(-D.mde)).attr("y", top - 8).attr("height", data.length * rowH + 8).attr("fill", "var(--ink)").attr("opacity", 0.05);
      for (const t of x.ticks(6)) {
        g.append("line").attr("class", "grid").attr("x1", x(t)).attr("x2", x(t)).attr("y1", top - 8).attr("y2", H - 28);
        g.append("text").attr("x", x(t)).attr("y", H - 10).attr("text-anchor", "middle").text(t === 0 ? "0" : sign(t, 0));
      }
      g.append("text").attr("x", iw / 2).attr("y", 14).attr("text-anchor", "middle").attr("class", "annot").text("gain in percentage points (right is better)");
      g.append("line").attr("class", "zero").attr("x1", x(0)).attr("x2", x(0)).attr("y1", top - 8).attr("y2", H - 28);
      if (primary()) {
        g.append("line").attr("class", "thresh").attr("x1", x(D.minGain)).attr("x2", x(D.minGain)).attr("y1", top - 8).attr("y2", H - 28);
        g.append("text").attr("class", "annot-strong").attr("x", x(D.minGain) + 5).attr("y", top + 2).text(narrow ? "bar" : `the ${D.minGain} pp bar`);
      }
      data.forEach((r, i) => {
        const cy = top + i * rowH + rowH / 2;
        const grp = svg.append("g").attr("tabindex", 0).attr("role", "img")
          .attr("aria-label", `${r.cell}: ${fpp(r.mean_pp)}, interval ${ciPP(r.ci_pp)}${r.meets_switch_rule ? ", meets the bar" : ""}`);
        grp.append("rect").attr("x", 0).attr("y", cy - rowH / 2).attr("width", w).attr("height", rowH).attr("fill", "transparent");
        mark(grp, r.cell, 10, cy, 5);
        grp.append("text").attr("x", 24).attr("y", cy + 4).attr("class", "t-strong").text(narrow ? r.cell.replace(" / ", "/").replace("signature_docstring", "sig_doc") : r.cell);
        const gg = grp.append("g").attr("transform", `translate(${m.l},0)`);
        if (r.ci_pp) gg.append("line").attr("stroke", colorOf(r.cell)).attr("stroke-width", 2.5).attr("stroke-linecap", "round").attr("x1", x(r.ci_pp[0])).attr("x2", x(r.ci_pp[1])).attr("y1", cy).attr("y2", cy);
        mark(gg, r.cell, x(r.mean_pp), cy, 6);
        if (!narrow) {
          const t = grp.append("text").attr("x", w - 2).attr("y", cy - 2).attr("text-anchor", "end").attr("class", "t-strong").text(fpp(r.mean_pp));
          t.append("title").text(`${r.cell}`);
          grp.append("text").attr("x", w - 2).attr("y", cy + 13).attr("text-anchor", "end").attr("class", "annot")
            .text(D.ready && r.p_holm != null ? `Holm p ${pv(r.p_holm)}` : r.ci_pp ? `[${sign(r.ci_pp[0])}, ${sign(r.ci_pp[1])}]` : "");
        }
        attachTip(grp.node(), () => `<b>${r.cell}</b>` + trow("gain", fpp(r.mean_pp)) + trow("95% interval", ciPP(r.ci_pp)) + trow("paired bugs", r.paired)
          + (D.ready ? trow("better / worse / equal", `${r.wins} / ${r.losses} / ${r.ties}`) + trow("d_z", fnum(r.dz, 2)) + trow("p, Holm-adjusted", `${pv(r.p)}, ${pv(r.p_holm)}`) : "")
          + (primary() ? trow("meets the bar", r.meets_switch_rule ? "yes" : "no") : ""));
      });
      plot.replaceChildren(svg.node());
    },
  });
}

export function renderPower(D) {
  if (!D.ready) {
    $("#fig-power").replaceChildren(el("div", { class: "pending" }, el("b", {}, "The power curve is being computed. "),
      `From the intervals of the aggregate grid, the spread of the paired differences works out to about ${Math.round(D.d.aggregate.median_implied_sd_pp)} percentage points, which would put the smallest gain 18 bugs can reliably detect near ${Math.round(D.mde)} points. That is approximate: it is recovered from asymmetric intervals, not from the per-bug differences.`));
    return;
  }
  const P = D.d.power;
  const mde = P.mde_pp_median_sd;
  figure($("#fig-power"), {
    id: "power",
    title: "Chance of seeing a true gain, by size of the gain",
    sub: `${P.n} bugs, two-sided test at 5%, the spread of the differences observed in this run`,
    caption: `The bold curve uses the median spread over the seven comparisons (${fnum(P.median_sd_pp, 0)} points); the grey curves use each comparison's own. The dotted line is 80% power, reached at about ${fnum(mde, 0)} points. ` +
      `Taking the Holm correction for seven comparisons into account, it would take about ${fnum(P.mde_pp_median_sd_holm, 0)} points. The ${D.minGain} point bar sits at ${Math.round((P.power_at_min_gain_median_sd ?? 0) * 100)}% power.`,
    table: () => ({
      caption: "Smallest gain detectable at 80% power, per comparison.",
      cols: [{ key: "c", label: "Cell" }, { key: "o", label: "Observed gain" }, { key: "s", label: "SD of paired differences" }, { key: "m", label: "Detectable at 80%" }, { key: "h", label: "Holm-corrected" }, { key: "pw", label: `Power at ${D.minGain} pp` }],
      rows: P.per_comparison.map((r) => ({ c: r.cell, o: fpp(r.observed_pp), s: fnum(r.sd_pp, 1) + " pp", m: fnum(r.mde_pp, 1) + " pp", h: fnum(r.mde_pp_holm, 1) + " pp", pw: Math.round((r.power_at_min_gain ?? 0) * 100) + "%" })),
    }),
    draw: (plot) => {
      const d3 = window.d3;
      const w = Math.floor(plot.getBoundingClientRect().width);
      const H = 320, m = { l: 44, r: 14, t: 14, b: 40 };
      const x = d3.scaleLinear([0, 30], [m.l, w - m.r]);
      const y = d3.scaleLinear([0, 1], [H - m.b, m.t]);
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "img").attr("aria-label", "Power curves: chance of detecting a gain of a given size");
      for (const t of [0, 0.25, 0.5, 0.75, 1]) {
        svg.append("line").attr("class", "grid").attr("x1", m.l).attr("x2", w - m.r).attr("y1", y(t)).attr("y2", y(t));
        svg.append("text").attr("x", m.l - 6).attr("y", y(t) + 4).attr("text-anchor", "end").text(Math.round(t * 100) + "%");
      }
      for (const t of d3.range(0, 31, 5)) svg.append("text").attr("x", x(t)).attr("y", H - 20).attr("text-anchor", "middle").text(t);
      svg.append("text").attr("x", (m.l + w - m.r) / 2).attr("y", H - 2).attr("text-anchor", "middle").attr("class", "annot").text("true gain in percentage points");
      const line = d3.line().x((p) => x(p[0])).y((p) => y(p[1]));
      for (const c of P.curves) svg.append("path").attr("class", "ghost").attr("d", line(c.points));
      svg.append("path").attr("fill", "none").attr("stroke", "var(--ink)").attr("stroke-width", 2.5).attr("d", line(P.median_curve.points));
      svg.append("line").attr("stroke", "var(--ink-2)").attr("stroke-dasharray", "2 4").attr("x1", m.l).attr("x2", w - m.r).attr("y1", y(0.8)).attr("y2", y(0.8));
      svg.append("text").attr("class", "annot").attr("x", w - m.r).attr("y", y(0.8) - 5).attr("text-anchor", "end").text("80% power");
      svg.append("line").attr("class", "thresh").attr("x1", x(D.minGain)).attr("x2", x(D.minGain)).attr("y1", m.t).attr("y2", H - m.b);
      svg.append("text").attr("class", "annot-strong").attr("x", x(D.minGain) + 5).attr("y", m.t + 12).text(`the ${D.minGain} pp bar`);
      if (mde <= 30) {
        svg.append("circle").attr("cx", x(mde)).attr("cy", y(0.8)).attr("r", 5).attr("fill", "var(--ink)").attr("stroke", "var(--surface)").attr("stroke-width", 2);
        svg.append("text").attr("class", "annot-strong").attr("x", x(mde) + 8).attr("y", y(0.8) + 18).text(`${fnum(mde, 0)} pp`);
      }
      plot.replaceChildren(svg.node());
    },
  });

  const nc = P.n_curve;
  if (nc) {
    const need = nc.points.find((p) => p[1] >= 0.8)?.[0];
    figure($("#fig-npower"), {
      id: "npower",
      title: `Chance of seeing a ${D.minGain} point gain, by number of bugs`,
      sub: `Same spread (${fnum(nc.sd_pp, 0)} points), five per cent level`,
      caption: need ? `Eighty per cent power for a ${D.minGain} point gain would need about ${need} bugs, against ${P.n} here.` : `Even 100 bugs would not reach 80% power for a ${D.minGain} point gain at this spread.`,
      table: () => ({ caption: "Power by number of bugs.", cols: [{ key: "n", label: "Bugs" }, { key: "p", label: "Power" }], rows: nc.points.filter((p, i) => i % 3 === 0 || p[0] === P.n).map((p) => ({ n: p[0], p: Math.round(p[1] * 100) + "%" })) }),
      draw: (plot) => {
        const d3 = window.d3;
        const w = Math.floor(plot.getBoundingClientRect().width);
        const H = 260, m = { l: 44, r: 14, t: 12, b: 40 };
        const x = d3.scaleLinear([6, 100], [m.l, w - m.r]);
        const y = d3.scaleLinear([0, 1], [H - m.b, m.t]);
        const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "img").attr("aria-label", "Power to detect the bar by number of bugs");
        for (const t of [0, 0.25, 0.5, 0.75, 1]) {
          svg.append("line").attr("class", "grid").attr("x1", m.l).attr("x2", w - m.r).attr("y1", y(t)).attr("y2", y(t));
          svg.append("text").attr("x", m.l - 6).attr("y", y(t) + 4).attr("text-anchor", "end").text(Math.round(t * 100) + "%");
        }
        for (const t of [10, 20, 40, 60, 80, 100]) svg.append("text").attr("x", x(t)).attr("y", H - 20).attr("text-anchor", "middle").text(t);
        svg.append("text").attr("x", (m.l + w - m.r) / 2).attr("y", H - 2).attr("text-anchor", "middle").attr("class", "annot").text("number of bugs");
        svg.append("path").attr("fill", "none").attr("stroke", "var(--ink)").attr("stroke-width", 2.5).attr("d", d3.line().x((p) => x(p[0])).y((p) => y(p[1]))(nc.points));
        svg.append("line").attr("stroke", "var(--ink-2)").attr("stroke-dasharray", "2 4").attr("x1", m.l).attr("x2", w - m.r).attr("y1", y(0.8)).attr("y2", y(0.8));
        svg.append("line").attr("class", "thresh").attr("x1", x(P.n)).attr("x2", x(P.n)).attr("y1", m.t).attr("y2", H - m.b);
        svg.append("text").attr("class", "annot-strong").attr("x", x(P.n) + 5).attr("y", m.t + 12).text(`this run: ${P.n} bugs`);
        plot.replaceChildren(svg.node());
      },
    });
  }
}
