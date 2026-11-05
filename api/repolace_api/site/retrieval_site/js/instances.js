// Everything that needs the per-bug run: the heatmap, reciprocal-rank distributions, repository dot plot and the pass/fail panel.
import { $, el, figure, attachTip, trow } from "/benchmark/js/core.js";
import { colorOf, hollow, mark, parse, ppct, ci, fnum, short, pendingBox, IX_COLOR, INDEX, sign, pv, plural } from "./shared.js";

const rankOf = (rr) => (rr == null || rr <= 0 ? null : Math.round(1 / rr));
const cellName = (k) => k.replace("signature_docstring", "sig_docstring");

export function renderBugs(D) {
  if (!D.ready) {
    $("#fig-heat").replaceChildren(pendingBox("The bug-by-bug heatmap and the distributions of where the first correct chunk lands need the per-instance results."));
    return;
  }
  heat(D); strip(D); bins(D);
}

function heat(D) {
  const H = D.d.heatmap;
  let kind = "chunk_rr", order = "retrieval";
  const kindSel = el("select", { class: "sel", "aria-label": "Metric" }, el("option", { value: "chunk_rr" }, "Chunk reciprocal rank"), el("option", { value: "file_rr" }, "File reciprocal rank"));
  const ordSel = el("select", { class: "sel", "aria-label": "Sort the bugs by" }, el("option", { value: "retrieval" }, "Best found first"), el("option", { value: "repo" }, "By repository"));
  kindSel.addEventListener("change", () => { kind = kindSel.value; api.redraw(); });
  ordSel.addEventListener("change", () => { order = ordSel.value; api.redraw(); });
  const rowsNow = () => {
    const r = [...H.rows];
    if (order === "repo") r.sort((a, b) => a.repo.localeCompare(b.repo) || a.instance.localeCompare(b.instance));
    return r;
  };
  const api = figure($("#fig-heat"), {
    id: "heat",
    title: "Where the first correct chunk landed, bug by bug",
    sub: "Rows are bugs, columns the eight cells. Darker means nearer the top of the 20 results.",
    tools: [kindSel, ordSel],
    caption: "A cell shows the rank of the first correct chunk (1 is best). A hatched cell is a miss: nothing correct in the top 20. A dashed empty cell was not scored for that bug (no gold of that kind). Hover or focus a cell for the exact value.",
    table: () => ({
      caption: "Reciprocal rank per bug and cell (rank in brackets).",
      cols: [{ key: "b", label: "Bug" }, { key: "r", label: "Repository" }, ...H.cells.map((c) => ({ key: c, label: c }))],
      rows: rowsNow().map((r) => ({ b: short(r.instance), r: r.repo.split("/")[1], ...Object.fromEntries(H.cells.map((c) => { const v = r[kind][c]; return [c, v == null ? "not scored" : v === 0 ? "miss" : `${fnum(v, 2)} (rank ${rankOf(v)})`]; })) })),
    }),
    draw: (plot) => {
      const rows = rowsNow();
      const grid = el("div", { class: "grid", style: `grid-template-columns: max-content repeat(${H.cells.length}, 66px);` });
      grid.append(el("div"));
      for (const c of H.cells) grid.append(el("div", { class: `colh ${hollow(c) ? "hollow" : ""}`, style: `--c:${colorOf(c)}`, title: c }, ...c.split(" / ").map((t, i) => el("span", {}, i ? "q: " + t : t.replace("signature_docstring", "sig_doc")))));
      for (const r of rows) {
        grid.append(el("div", { class: "lab" }, short(r.instance), el("span", { class: "repo" }, r.repo.split("/")[1])));
        for (const c of H.cells) {
          const v = r[kind][c];
          const rank = rankOf(v);
          const cls = v == null ? "na" : v === 0 ? "miss" : "hit";
          const pct = v == null || v === 0 ? 0 : Math.round(18 + 82 * v);
          const b = el("button", { class: `hcell ${cls}`, type: "button", style: v ? `background:color-mix(in srgb, var(--accent) ${pct}%, var(--surface));${pct < 52 ? "color:var(--ink)" : ""}` : "",
            "aria-label": `${short(r.instance)}, ${c}: ${v == null ? "not scored" : v === 0 ? "miss" : "rank " + rank}` }, v == null ? "–" : v === 0 ? "" : String(rank));
          attachTip(b, () => `<b>${short(r.instance)}</b>` + trow("cell", c) + trow(kind === "chunk_rr" ? "chunk RR" : "file RR", v == null ? "not scored" : v === 0 ? "miss (0)" : `${fnum(v, 3)}, rank ${rank}`));
          grid.append(b);
        }
      }
      plot.replaceChildren(el("div", { class: "heat-ret" }, grid, el("div", { class: "legend", style: "margin-top:.7rem" },
        el("span", { class: "k ramp" }, "rank 20", el("span", { class: "bar" }), "rank 1"), el("span", { class: "k" }, el("span", { class: "sq lost" }), "miss"), el("span", { class: "k" }, "– not scored"))));
    },
  });
}

function strip(D) {
  const R = D.d.distributions.cells;
  let kind = "chunk_mrr";
  const sel = el("select", { class: "sel", "aria-label": "Metric" }, el("option", { value: "chunk_mrr" }, "Chunk RR"), el("option", { value: "file_mrr" }, "File RR"));
  sel.addEventListener("change", () => { kind = sel.value; api.redraw(); });
  const api = figure($("#fig-strip"), {
    id: "strip",
    title: "Distribution of reciprocal rank, one dot per bug",
    sub: "Reciprocal rank is 1 when the first correct chunk is first, 0.5 when second, 0 for a miss. The bar is the median.",
    tools: [sel],
    caption: "Most dots sit at 0: for most bugs the top 20 chunks hold nothing the fix changed. A mean over these values is driven by the few bugs found at rank 1 to 3, which is why the intervals are wide.",
    table: () => ({
      caption: "Share of bugs by outcome, and median.",
      cols: [{ key: "c", label: "Cell" }, { key: "n", label: "Scored" }, { key: "m", label: "Miss" }, { key: "r1", label: "Rank 1" }, { key: "md", label: "Median RR" }],
      rows: R.map((c) => ({ c: c.key, n: c[kind].values.length, m: ppct(c[kind].share_miss, 0), r1: ppct(c[kind].share_rank1, 0), md: fnum(c[kind].median, 2) })),
    }),
    draw: (plot) => {
      const d3 = window.d3;
      const w = Math.floor(plot.getBoundingClientRect().width);
      const narrow = w < 560, labW = narrow ? 118 : 190, rowH = 36, top = 22, H = top + R.length * rowH + 30;
      const x = d3.scaleLinear([0, 1], [labW + 8, w - 16]);
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", "Reciprocal rank of every bug in every cell");
      for (const t of [0, 0.25, 0.5, 0.75, 1]) {
        svg.append("line").attr("class", "grid").attr("x1", x(t)).attr("x2", x(t)).attr("y1", top - 6).attr("y2", H - 24);
        svg.append("text").attr("x", x(t)).attr("y", H - 8).attr("text-anchor", t === 0 ? "start" : t === 1 ? "end" : "middle").text(t);
      }
      R.forEach((c, i) => {
        const cy = top + i * rowH + rowH / 2;
        mark(svg, c.key, 8, cy, 5);
        svg.append("text").attr("x", 22).attr("y", cy + 4).attr("class", "t-strong").text(narrow ? cellName(c.key).replace(" / ", "/") : cellName(c.key));
        const vals = c[kind].values;
        const seen = new Map();
        vals.forEach((v) => {
          const k = v.rr.toFixed(3); const j = seen.get(k) || 0; seen.set(k, j + 1);
          const dy = (j % 4 - 1.5) * 5.2;
          const dot = mark(svg, c.key, x(v.rr), cy + dy, 3.6);
          dot.attr("opacity", 0.9);
          attachTip(dot.node(), () => `<b>${short(v.instance)}</b>` + trow(c.key, v.rr === 0 ? "miss" : `RR ${fnum(v.rr, 3)}, rank ${rankOf(v.rr)}`));
        });
        const md = c[kind].median ?? 0;
        svg.append("line").attr("class", "med").attr("x1", x(md)).attr("x2", x(md)).attr("y1", cy - 13).attr("y2", cy + 13);
      });
      plot.replaceChildren(svg.node());
    },
  });
}

function bins(D) {
  const names = D.d.distributions.rank_bins;
  const R = D.d.distributions.cells;
  const shade = (i) => `color-mix(in srgb, var(--accent) ${[100, 78, 58, 38, 22][i]}%, var(--surface))`;
  figure($("#fig-bins"), {
    id: "bins",
    title: "Where the first correct chunk lands, as a share of bugs",
    sub: "Chunk reciprocal rank, grouped by rank",
    caption: "Each bar is the 18 bugs (or those scored) of one cell. Ranks 1 and 2 are where an agent reading the first few results would find the code; the empty segment is the bugs for which the top 20 held nothing.",
    table: () => ({ caption: "Bugs by rank of the first correct chunk (chunk RR).", cols: [{ key: "c", label: "Cell" }, ...names.map((n) => ({ key: n, label: n === "miss" ? "Miss" : "Rank " + n }))], rows: R.map((c) => ({ c: c.key, ...c.chunk_mrr.bins })) }),
    draw: (plot) => {
      const d3 = window.d3;
      const w = Math.floor(plot.getBoundingClientRect().width);
      const narrow = w < 560, labW = narrow ? 118 : 190, rowH = 30, top = 4, H = top + R.length * rowH + 52;
      const x = d3.scaleLinear([0, 1], [labW + 8, w - 12]);
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", "Share of bugs by rank bucket for every cell");
      R.forEach((c, i) => {
        const cy = top + i * rowH + 4;
        const tot = Object.values(c.chunk_mrr.bins).reduce((a, b) => a + b, 0) || 1;
        mark(svg, c.key, 8, cy + 10, 5);
        svg.append("text").attr("x", 22).attr("y", cy + 14).attr("class", "t-strong").text(narrow ? cellName(c.key).replace(" / ", "/") : cellName(c.key));
        let acc = 0;
        names.forEach((n, j) => {
          const cnt = c.chunk_mrr.bins[n] || 0;
          if (!cnt) return;
          const x0 = x(acc / tot), x1 = x((acc + cnt) / tot); acc += cnt;
          const r = svg.append("rect").attr("x", x0 + 1).attr("width", Math.max(1, x1 - x0 - 2)).attr("y", cy).attr("height", 20).attr("rx", 3)
            .attr("fill", n === "miss" ? "var(--surface)" : shade(j)).attr("stroke", n === "miss" ? "var(--axis)" : "none").attr("tabindex", 0).attr("role", "img").attr("aria-label", `${c.key}, ${n === "miss" ? "miss" : "rank " + n}: ${plural(cnt, "bug")}`);
          if (x1 - x0 > 20) svg.append("text").attr("x", (x0 + x1) / 2).attr("y", cy + 14).attr("text-anchor", "middle").attr("class", "annot-strong").attr("fill", j < 3 && n !== "miss" ? "var(--page)" : "var(--ink)").style("fill", j < 3 && n !== "miss" ? "var(--page)" : "var(--ink)").text(cnt);
          attachTip(r.node(), () => `<b>${c.key}</b>` + trow(n === "miss" ? "miss" : "rank " + n, plural(cnt, "bug")));
        });
      });
      const ly = H - 26;
      names.forEach((n, j) => {
        const lx = labW + 8 + j * Math.min(86, (w - labW - 20) / names.length);
        svg.append("rect").attr("x", lx).attr("y", ly).attr("width", 12).attr("height", 12).attr("rx", 3).attr("fill", n === "miss" ? "var(--surface)" : shade(j)).attr("stroke", n === "miss" ? "var(--axis)" : "none");
        svg.append("text").attr("x", lx + 17).attr("y", ly + 10).text(n === "miss" ? "miss" : "rank " + n);
      });
      plot.replaceChildren(svg.node());
    },
  });
}

export function renderRepos(D) {
  if (!D.ready) { $("#fig-repos").replaceChildren(pendingBox("The per-repository breakdown needs the per-instance results.")); return; }
  const Rp = D.d.repos.repos.slice().sort((a, b) => b.instances - a.instances || a.repo.localeCompare(b.repo));
  let metric = "chunk_mrr";
  const sel = el("select", { class: "sel", "aria-label": "Metric" }, el("option", { value: "chunk_mrr" }, "Chunk MRR"), el("option", { value: "file_mrr" }, "File MRR"));
  sel.addEventListener("change", () => { metric = sel.value; api.redraw(); });
  const val = (c) => c[metric];
  const api = figure($("#fig-repos"), {
    id: "repos",
    title: "Mean reciprocal rank by repository",
    sub: "Descriptive only: one to six bugs per repository, no intervals, no tests",
    tools: [sel],
    caption: `Each row is a repository with its number of bugs. Dots are the eight cells; the row is split into four lanes, one per index strategy. ${D.d.repos.top_two.map((r) => r.split("/")[1]).join(" and ")} hold ${D.d.repos.top_two_instances} of the ${D.d.repos.total_instances} bugs, so the pooled numbers are mostly about those two.`,
    table: () => ({
      caption: "Mean reciprocal rank per repository and cell.",
      cols: [{ key: "r", label: "Repository (bugs)" }, ...D.keys.map((k) => ({ key: k, label: k }))],
      rows: Rp.map((r) => ({ r: `${r.repo} (${r.instances})`, ...Object.fromEntries(r.cells.map((c) => [c.cell, val(c) == null ? "n/a" : ppct(val(c))])) })),
    }),
    draw: (plot) => {
      const d3 = window.d3;
      const w = Math.floor(plot.getBoundingClientRect().width);
      const narrow = w < 560, labW = narrow ? 112 : 190, rowH = 58, top = 12, H = top + Rp.length * rowH + 34;
      const mx = Math.max(0.1, Math.ceil(Math.max(...Rp.flatMap((r) => r.cells.map((c) => val(c) ?? 0))) * 10) / 10);
      const x = d3.scaleLinear([0, mx], [labW + 8, w - 14]);
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", "Mean reciprocal rank per repository");
      for (const t of x.ticks(5)) {
        svg.append("line").attr("class", "grid").attr("x1", x(t)).attr("x2", x(t)).attr("y1", top).attr("y2", H - 26);
        svg.append("text").attr("x", x(t)).attr("y", H - 8).attr("text-anchor", "middle").text(Math.round(t * 100) + "%");
      }
      Rp.forEach((r, i) => {
        const cy = top + i * rowH + rowH / 2;
        svg.append("text").attr("x", 0).attr("y", cy - 2).attr("class", "t-strong").text(r.repo.split("/")[1]);
        svg.append("text").attr("x", 0).attr("y", cy + 14).attr("class", "annot").text(plural(r.instances, "bug"));
        r.cells.forEach((c) => {
          const v = val(c);
          if (v == null) return;
          const lane = INDEX.indexOf(parse(c.cell).s);
          const dot = mark(svg, c.cell, x(v), cy + (lane - 1.5) * 11, 4.2);
          dot.attr("tabindex", 0).attr("role", "img").attr("aria-label", `${r.repo}, ${c.cell}: ${ppct(v)}`);
          attachTip(dot.node(), () => `<b>${r.repo}</b> (${plural(r.instances, "bug")})` + trow(c.cell, ppct(v)) + (c.delta_pp != null ? trow("vs baseline", sign(c.delta_pp) + " pp") : ""));
        });
      });
      plot.replaceChildren(svg.node());
    },
  });
}

export function renderSweep(D) {
  const box = $("#pf-box");
  if (!D.ready) { box.replaceChildren(pendingBox("The relation between retrieval and the sweep-4 outcome needs the per-instance retrieval results.")); box.className = ""; return; }
  const S = D.d.sweep_link;
  if (!S.available) { box.replaceChildren(el("p", {}, "The sweep-4 task table was not available when this page was built, so this panel is empty.")); return; }
  const sp = S.spearman, au = S.auc;
  box.replaceChildren(
    el("span", { class: "tag" }, "Exploratory"),
    el("p", {}, `${S.instances} bugs have both a retrieval score and at least one sweep-4 run that finished. ${S.rows_excluded_harness_loss} of the ${S.rows_total} sweep-4 rows have no outcome (the harness lost them, mostly to provider rate limits) and are left out; they are not counted as failures. One test, not corrected for anything, on very few bugs.`),
    sp ? el("dl", { class: "pf-stats" },
      el("div", {}, el("dt", {}, "Spearman rank correlation"), el("dd", {}, `${fnum(sp.rho, 2)}  (95% interval ${fnum(sp.ci?.[0], 2)} to ${fnum(sp.ci?.[1], 2)})`)),
      el("div", {}, el("dt", {}, "Permutation p"), el("dd", {}, pv(sp.p_permutation))),
      el("div", {}, el("dt", {}, `Chance a solved bug was better retrieved than an unsolved one (${au.n_ever_passed} vs ${au.n_never_passed} bugs)`), el("dd", {}, `${fnum(au.value, 2)}  (${fnum(au.ci?.[0], 2)} to ${fnum(au.ci?.[1], 2)})`)),
      el("div", {}, el("dt", {}, "Pass rate, retrieval hit vs miss"), el("dd", {}, `${ppct(S.hit_split.hit.mean_pass_rate, 0)} (${S.hit_split.hit.instances} bugs) vs ${ppct(S.hit_split.miss.mean_pass_rate, 0)} (${S.hit_split.miss.instances})`))) : null,
    el("p", { style: "margin-top:.8rem" }, S.power_note || ""));
  const per = S.per_instance;
  figure($("#fig-sweep"), {
    id: "sweep",
    title: "Retrieval of the baseline cell against the sweep-4 pass rate",
    sub: "One dot per bug. Horizontal: chunk reciprocal rank of truncate / truncate. Vertical: share of its finished sweep-4 runs that passed.",
    caption: "Dots at the left edge are bugs the baseline retrieval missed entirely. The agent can also search the repository with its own tools, so it does not depend only on this retrieval; a weak link would not be surprising.",
    table: () => ({ caption: "Per bug: baseline retrieval and sweep-4 outcome.", cols: [{ key: "b", label: "Bug" }, { key: "r", label: "Chunk RR" }, { key: "p", label: "Passed / finished runs" }, { key: "l", label: "Rows lost" }],
      rows: per.map((p) => ({ b: short(p.instance), r: fnum(p.rr, 3), p: `${p.passes} / ${p.runs}`, l: p.lost_rows })) }),
    draw: (plot) => {
      const d3 = window.d3;
      const w = Math.floor(plot.getBoundingClientRect().width);
      const H = Math.min(360, Math.max(260, w * 0.5)), m = { l: 48, r: 16, t: 14, b: 44 };
      const x = d3.scaleLinear([0, 1], [m.l + 16, w - m.r - 8]);
      const y = d3.scaleLinear([0, 1], [H - m.b - 10, m.t + 10]);
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", "Retrieval success against sweep-4 pass rate, one dot per bug");
      for (const t of [0, 0.25, 0.5, 0.75, 1]) {
        svg.append("line").attr("class", "grid").attr("x1", m.l).attr("x2", w - m.r).attr("y1", y(t)).attr("y2", y(t));
        svg.append("text").attr("x", m.l - 6).attr("y", y(t) + 4).attr("text-anchor", "end").text(Math.round(t * 100) + "%");
        svg.append("text").attr("x", x(t)).attr("y", H - 24).attr("text-anchor", "middle").text(t);
      }
      svg.append("text").attr("x", (m.l + w - m.r) / 2).attr("y", H - 4).attr("text-anchor", "middle").attr("class", "annot").text("chunk reciprocal rank, truncate / truncate");
      const seen = new Map();
      per.forEach((p) => {
        const k = `${p.rr.toFixed(3)}/${p.pass_rate.toFixed(2)}`; const j = seen.get(k) || 0; seen.set(k, j + 1);
        const dot = svg.append("circle").attr("cx", x(p.rr) + (j % 3 - 1) * 9 * (j ? 1 : 0)).attr("cy", y(p.pass_rate) - Math.floor(j / 3) * 9).attr("r", 6)
          .attr("fill", "var(--accent)").attr("stroke", "var(--surface)").attr("stroke-width", 2).attr("tabindex", 0).attr("role", "img")
          .attr("aria-label", `${short(p.instance)}: retrieval RR ${fnum(p.rr, 2)}, passed ${p.passes} of ${p.runs}`);
        attachTip(dot.node(), () => `<b>${short(p.instance)}</b>` + trow("chunk RR", fnum(p.rr, 3)) + trow("passed", `${p.passes} of ${p.runs} finished runs`));
      });
      plot.replaceChildren(svg.node());
    },
  });
}
