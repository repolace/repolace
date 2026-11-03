// Variant against the baseline. The pre-registered rule comes first and is the only verdict; everything else is
// labelled exploratory. Tables always show all three variants, so the run filter can never hide a result.
import { $, el, figure, buildTable, onSelection, selected, RUN_META, pct, pp, f1, usd, pval, short, attachTip, trow, esc } from "./core.js";
import { pairCounts } from "./data.js";

const VARIANTS = ["sweep-e1", "sweep-r2", "sweep-m1"];

export function renderCompare(D) {
  const c0 = D.cmp["sweep-e1"];
  const rule = c0.primary.rule.replace(/^Fixed before any data:\s*/i, "").replace(/^./, (ch) => ch.toUpperCase());
  $("#prereg").innerHTML =
    `<span class="tag">Pre-registered</span>` +
    `<p><strong>The rule was written down before the variant runs started and is applied here as written.</strong> ${esc(rule)}</p>` +
    `<p>Three variants were tested against the same baseline, so the p-values are adjusted for three comparisons with Holm's method. Each variant is compared with the 35/54 baseline, not the 29/54 sweep-4 figure, because every variant had its own outage-lost rows re-run in place.</p>`;

  // ---- tiles: the 18 bugs on the first run of each pair
  const check = [];
  const wf = figure($("#fig-waffle"), {
    id: "waffle",
    title: "The 18 bugs, matched on their first run",
    sub: "Only bugs where exactly one of the two passed carry information about which is better",
    caption: "Each tile is one bug. Bugs that both configurations pass, or both fail, say nothing about which is better; the exact test counts only the filled coloured tiles.",
    table: () => ({
      caption: "Matched first runs: the McNemar table.",
      cols: [{ key: "v", label: "Variant" }, { key: "both", label: "Passed by both" }, { key: "base", label: "Baseline only" }, { key: "var", label: "Variant only" }, { key: "none", label: "Failed by both" }],
      rows: VARIANTS.map((v) => { const p = pairCounts(D, v); return { v: RUN_META[v].short, both: p.both, base: p.baseOnly, var: p.varOnly, none: p.none }; }),
    }),
    draw: (plot) => {
      const ids = selected().filter((id) => VARIANTS.includes(id));
      const legend = el("div", { class: "legend" },
        el("span", { class: "k" }, el("span", { class: "sq", style: "background:var(--ink);opacity:.82;border-color:var(--ink)" }), "passed by both"),
        el("span", { class: "k" }, el("span", { class: "sq", style: "background:var(--s-baseline);border-color:var(--s-baseline)" }), "only the baseline passed"),
        el("span", { class: "k" }, el("span", { class: "sq", style: "background:var(--s-e1);border-color:var(--s-e1)" }), "only the variant passed (in its colour)"),
        el("span", { class: "k" }, el("span", { class: "sq" }), "failed by both"));
      const rows = ids.map((v) => {
        const p = pairCounts(D, v);
        const m = D.cmp[v].primary.mcnemar_run_index_0;
        if (p.baseOnly !== m.baseline_only_passed || p.varOnly !== m.variant_only_passed || p.both !== m.both_passed) console.error("pairing mismatch for", v, p, m);
        const tiles = el("div", { class: `tiles ${RUN_META[v].cls}`, role: "img", "aria-label": `${RUN_META[v].short}: ${p.both} passed by both, ${p.baseOnly} only the baseline, ${p.varOnly} only the variant, ${p.none} failed by both` });
        let last = null;
        for (const t of p.tiles) {
          if (last && last !== t.kind) tiles.append(el("span", { class: "tile-gap" }));
          last = t.kind;
          const tile = el("span", { class: `tile ${t.kind}`, "aria-label": `${short(t.inst)}: ${t.kind === "both" ? "passed by both" : t.kind === "base" ? "only the baseline passed" : t.kind === "var" ? "only the variant passed" : "failed by both"}` });
          attachTip(tile, () => `<b>${esc(short(t.inst))}</b>` + `<div>${t.kind === "both" ? "Passed by both" : t.kind === "base" ? "Only the baseline passed" : t.kind === "var" ? "Only the variant passed" : "Failed by both"}</div>`);
          tiles.append(tile);
        }
        return el("div", { class: "waffle-row" },
          el("div", { class: "name" }, RUN_META[v].short, el("small", {}, `${p.baseOnly} for the baseline, ${p.varOnly} for the variant`)),
          tiles);
      });
      plot.replaceChildren(legend, el("div", { class: "waffles" }, rows.length ? rows : el("p", { class: "fig-sub" }, "Turn a variant on in the run filter above to see its tiles.")));
    },
  });
  onSelection(wf.redraw);

  // ---- the pre-registered table
  const prim = [];
  for (const v of VARIANTS) {
    const r = D.runs[v], c = D.cmp[v], m = c.primary.mcnemar_run_index_0;
    prim.push({
      v: RUN_META[v].short,
      passed: `${r.headline.passed} of ${r.headline.planned}`,
      rate: `${pct(r.headline.rate)}  [${pct(r.headline.ci_bootstrap_instances[0])} to ${pct(r.headline.ci_bootstrap_instances[1])}]`,
      b: m.baseline_only_passed, o: m.variant_only_passed,
      p: pval(m.p_exact_two_sided), h: pval(m.p_holm_adjusted),
      inside: c.primary.baseline_inside_variant_interval ? "yes" : "no",
    });
  }
  const secondary = ["baseline", ...VARIANTS].map((id) => {
    const s = id === "baseline" ? D.cmp["sweep-e1"].primary.secondary_metrics.baseline : D.cmp[id].primary.secondary_metrics.variant;
    return { v: RUN_META[id].short, fe: f1(s.median_first_edit_call), cap: pct(s.step_cap_share), cost: usd(s.median_cost_per_task_usd), sub: pct(s.submitted_share) };
  });
  $("#tbl-primary").replaceChildren(
    el("h3", {}, "The pre-registered result"),
    buildTable({
      caption: "Pass rate against the baseline interval, and the exact McNemar test on first runs (18 matched bugs).",
      cols: [{ key: "v", label: "Variant" }, { key: "passed", label: "Passed" }, { key: "rate", label: "Rate [interval over bugs]" }, { key: "b", label: "Only baseline passed" }, { key: "o", label: "Only variant passed" }, { key: "p", label: "Exact p" }, { key: "h", label: "Holm-adjusted p" }, { key: "inside", label: "Baseline inside its interval?" }],
      rows: prim,
      foot: "A pass-rate difference alone is never the verdict (benchmark.md); the test uses only the bugs where the two disagreed.",
    }),
    el("h3", {}, "The four secondary measures"),
    buildTable({
      caption: "Whole-run measures, baseline against each variant.",
      cols: [{ key: "v", label: "Run" }, { key: "fe", label: "Median call of the first edit" }, { key: "cap", label: "Hit the step cap" }, { key: "cost", label: "Median cost per task" }, { key: "sub", label: "Submitted" }],
      rows: secondary,
      foot: "Costs are censored by the $2 task cap and use Google AI Studio list prices, not billed Vertex rates.",
    }));

  // ---- confounds
  const e1 = D.runs["sweep-e1"], m1 = D.runs["sweep-m1"];
  const nr = e1.code_versions || {};
  const a9 = nr["9"] ?? 0, a21 = nr["21"] ?? 0;
  const noChange = m1.stop_reasons?.no_change?.n ?? 0;
  $("#confounds").innerHTML =
    `<span class="tag">Not a clean A/B</span>` +
    `<p><strong>Early-edit nudge (e1) changed under itself.</strong> The gateway's retry window went from 9 to 21 attempts on its branch at ${D.d.code_versions.e1_retry_window_commit.committed_at.slice(0, 10)}: ${a9} of its rows ran on 9 attempts and ${a21} on 21. The baseline, role split and Flash-editor runs all ran on 9, so this is an e1-specific confound.</p>` +
    `<p><strong>Flash editor (m1) mostly did not edit.</strong> ${noChange} of its ${m1.planned} attempts ended with no change to the code, and none of those passed. That is a different failure from the other runs, and it is why m1 is cheap per task.</p>` +
    `<p><strong>Rows were re-run in place.</strong> Rows lost to 429 errors in e1, r2 and m1 were archived and re-run in the same run; the baseline used a separate run for the same purpose. The unfinished runs (sweep-5, r1) are never compared on a rate.</p>`;

  // ---- exploratory
  renderExploratory(D);
}

function renderExploratory(D) {
  const host = $("#exploratory");
  const rowsT = VARIANTS.map((v) => {
    const c = D.cmp[v], e = c.exploratory;
    const mh = e.mantel_haenszel;
    return {
      v: RUN_META[v].short,
      diff: `${pp(e.paired_cluster_bootstrap_difference.estimate)}  [${pp(e.paired_cluster_bootstrap_difference.ci[0])}, ${pp(e.paired_cluster_bootstrap_difference.ci[1])}]`,
      sign: `${e.sign_test.instances_better} better, ${e.sign_test.instances_worse} worse, ${e.sign_test.tied} tied`,
      ps: pval(e.sign_test.p_exact_two_sided), ph: pval(e.sign_test.p_holm_adjusted),
      w: pval(e.wilcoxon_signed_rank.p), perm: pval(e.permutation_sign_flip.p),
      h: e.cohens_h.toFixed(2),
      mh: mh.defined ? `${mh.odds_ratio.toFixed(2)} [${mh.ci[0].toFixed(2)}, ${mh.ci[1].toFixed(2)}]` : "undefined",
    };
  });
  const allZero = VARIANTS.every((v) => D.cmp[v].exploratory.sign_test.instances_better === 0);
  const worse = VARIANTS.map((v) => `${RUN_META[v].short} ${D.cmp[v].exploratory.sign_test.instances_worse}`).join(", ");
  const m1p = D.cmp["sweep-m1"].exploratory.sign_test.p_holm_adjusted;
  const mhNote = D.cmp["sweep-m1"].exploratory.mantel_haenszel;
  host.replaceChildren(
    el("span", { class: "tag" }, "Exploratory, not a verdict"),
    el("p", { html:
      `<strong>These tests were not planned.</strong> They reuse the same 18 bugs as the pre-registered test, several p-values are unadjusted, and the baseline is itself a substitution. Read them as where to look next. ` +
      (allZero ? `One pattern is worth saying out loud: under every variant <strong>no bug passed more often than under the baseline</strong> (bugs that passed less often: ${worse}). With 18 bugs the exact test cannot call that a difference; it is also not evidence of none.` : "") }),
    buildTable({
      caption: "Exploratory statistics, variant against baseline, on the instances scored in both.",
      cols: [{ key: "v", label: "Variant" }, { key: "diff", label: "Paired difference in pass rate [bootstrap over bugs]" }, { key: "sign", label: "Bugs, variant vs baseline" }, { key: "ps", label: "Sign test p" }, { key: "ph", label: "Sign test, Holm p" }, { key: "w", label: "Wilcoxon p" }, { key: "perm", label: "Permutation p" }, { key: "h", label: "Cohen's h" }, { key: "mh", label: "Mantel-Haenszel odds ratio" }],
      rows: rowsT,
      foot: `Only the Flash-editor sign test is below 0.05 after Holm (${pval(m1p)}). ${mhNote.defined ? "" : "Its Mantel-Haenszel odds ratio is undefined: " + mhNote.reason + "."} Bugs that pass in every run of both configurations, or fail in every run of both, add nothing to a paired comparison, so many strata are empty.`,
    }),
    el("div", { id: "fig-flips" }));
  const flipsHost = $("#fig-flips", host);
  const items = [];
  for (const v of VARIANTS) for (const f of D.cmp[v].exploratory.flips) items.push({ v, ...f });
  figure(flipsHost, {
    id: "flips",
    title: "Bugs whose pass count changed, out of three runs",
    sub: "From the baseline (blue) to the variant (its own colour)",
    caption: allZero ? "Every change points left: no bug was solved more often by a variant. Each row is one bug." : "Each row is one bug whose pass count differs between the two configurations.",
    table: () => ({
      caption: "Bugs where the variant's passes differ from the baseline's.",
      cols: [{ key: "v", label: "Variant" }, { key: "bug", label: "Bug" }, { key: "b", label: "Baseline passes (of 3)" }, { key: "o", label: "Variant passes (of 3)" }],
      rows: items.map((f) => ({ v: RUN_META[f.v].short, bug: short(f.instance_id), b: f.baseline_passes, o: f.variant_passes })),
    }),
    draw: (plot) => {
      const d3 = window.d3;
      const w = Math.floor(plot.getBoundingClientRect().width);
      const narrow = w < 560;
      const labW = narrow ? 138 : 230, rowH = 26, gapH = 28;
      let yy = 22;
      const pos = [];
      let lastV = null;
      for (const it of items) { if (lastV && lastV !== it.v) yy += gapH; if (lastV !== it.v) { pos.push({ head: it.v, y: yy }); yy += 20; } lastV = it.v; pos.push({ it, y: yy }); yy += rowH; }
      const H = yy + 34;
      const iw = w - labW - 24;
      const x = d3.scaleLinear([0, 3], [0, iw]);
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "img").attr("aria-label", "Bugs whose pass count differs between baseline and variant");
      const g = svg.append("g").attr("transform", `translate(${labW},0)`);
      for (let t = 0; t <= 3; t++) {
        g.append("line").attr("class", "grid").attr("x1", x(t)).attr("x2", x(t)).attr("y1", 12).attr("y2", H - 28);
        g.append("text").attr("x", x(t)).attr("y", H - 10).attr("text-anchor", t === 0 ? "start" : t === 3 ? "end" : "middle").text(t);
      }
      g.append("text").attr("x", iw / 2).attr("y", 8).attr("text-anchor", "middle").text("passes out of 3 runs");
      for (const p of pos) {
        if (p.head) { svg.append("text").attr("x", 0).attr("y", p.y + 8).attr("class", "t-strong").text(RUN_META[p.head].short); continue; }
        const it = p.it, cy = p.y + rowH / 2;
        const grp = svg.append("g").attr("class", RUN_META[it.v].cls).attr("tabindex", 0).attr("role", "img").attr("aria-label", `${short(it.instance_id)}: baseline ${it.baseline_passes} of 3, ${RUN_META[it.v].short} ${it.variant_passes} of 3`);
        grp.append("text").attr("x", 14).attr("y", cy + 4).text(short(it.instance_id));
        const gm = grp.append("g").attr("transform", `translate(${labW},${cy})`);
        gm.append("line").attr("x1", x(it.variant_passes)).attr("x2", x(it.baseline_passes)).attr("y1", 0).attr("y2", 0).attr("stroke", "var(--axis)").attr("stroke-width", 2.5);
        gm.append("circle").attr("cx", x(it.baseline_passes)).attr("r", 6).attr("fill", "var(--s-baseline)").attr("stroke", "var(--page)").attr("stroke-width", 2);
        gm.append("circle").attr("cx", x(it.variant_passes)).attr("r", 6).attr("fill", "var(--c)").attr("stroke", "var(--page)").attr("stroke-width", 2);
        gm.append("rect").attr("class", "hit").attr("x", -labW).attr("y", -rowH / 2).attr("width", iw + labW).attr("height", rowH);
        attachTip(grp.node(), () => `<b>${esc(short(it.instance_id))}</b>` + trow("Baseline", `${it.baseline_passes} of 3`) + trow(RUN_META[it.v].short, `${it.variant_passes} of 3`));
      }
      plot.replaceChildren(svg.node());
    },
  });
}
