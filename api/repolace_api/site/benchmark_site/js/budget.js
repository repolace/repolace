// Where the 40 steps and the dollars go: first-edit timing, how attempts ended, tool mix, cost curves, cost per pass.
import { $, el, figure, onSelection, selected, RUN_META, pct, f1, usd, attachTip, trow, esc } from "./core.js";
import { STOP_LABEL, STOP_ORDER } from "./data.js";

const legendOf = (ids) => {
  const n = el("div", { class: "legend" });
  ids.forEach((id) => {
    const m = RUN_META[id];
    const sq = el("span", { class: "sq", style: `${m.partial || m.open ? "background:transparent;" : "background:var(--c);"}border-color:var(--c);${m.partial ? "border-style:dashed;" : ""}` });
    n.append(el("span", { class: `k ${m.cls}` }, sq, m.short));
  });
  return n;
};

export function renderBudget(D) {
  ecdf(D);
  const neutral = (pctInk) => `color-mix(in srgb, var(--ink) ${pctInk}%, var(--surface))`;
  const stopCats = [
    { key: "submitted", label: STOP_LABEL.submitted, fill: neutral(80), dark: true },
    { key: "step_cap", label: STOP_LABEL.step_cap, fill: neutral(52), dark: true },
    { key: "no_change", label: STOP_LABEL.no_change, fill: neutral(30) },
    { key: "budget_wall", label: STOP_LABEL.budget_wall, fill: neutral(18) },
    { key: "llm_error", label: STOP_LABEL.llm_error, fill: neutral(11) },
    { key: "lost", label: STOP_LABEL.lost, fill: "url(#hatch)" },
  ];
  const holder = $("#fig-ends");
  holder.replaceChildren(el("div", { id: "fig-ends-a" }), el("div", { id: "fig-ends-b" }));
  stacked($("#fig-ends-a"), D, {
    id: "ends", title: "How the attempts ended", sub: "Share of all planned rows; the number inside is a count",
    caption: "Hitting the step cap is not the same as failing: many capped attempts still passed (hover a segment). \"Lost or unfinished\" is rows that never produced a result.",
    cats: stopCats,
    rowOf: (id) => {
      const r = D.runs[id], sr = r.stop_reasons || {};
      const vals = {}; let sum = 0;
      for (const k of Object.keys(STOP_LABEL)) if (k !== "lost") { const n = sr[k]?.n || 0; vals[k] = n; sum += n; }
      vals.lost = Math.max(0, r.planned - sum);
      return { vals, total: r.planned, pass: Object.fromEntries(Object.keys(sr).map((k) => [k, sr[k].passed])) };
    },
  });
  const toolCats = [
    { key: "read", label: "Reading code", fill: neutral(30) },
    { key: "run", label: "Running tests and scripts", fill: neutral(66), dark: true },
    { key: "edit", label: "Editing", fill: "var(--accent)", dark: true },
    { key: "submit", label: "Submitting", fill: neutral(90), dark: true },
  ];
  stacked($("#fig-ends-b"), D, {
    id: "toolmix", title: "What the tool calls were", sub: "Share of each run's tool calls",
    caption: "Editing is a small slice everywhere: most of the agent's calls are running things and reading. The Flash editor reads far more and edits far less.",
    cats: toolCats,
    rowOf: (id) => {
      const tm = D.runs[id].behaviour?.tool_mix; if (!tm) return null;
      const vals = {}; let s = 0; for (const c of toolCats) { vals[c.key] = tm[c.key] || 0; s += vals[c.key]; }
      return { vals, total: s || 1, share: true, calls: tm.tool_calls };
    },
  });
  curves(D);
  perPass(D);
}

// ---------- first-edit ECDF ----------
function ecdf(D) {
  const api = figure($("#fig-ecdf"), {
    id: "ecdf",
    title: "How early does the agent make its first edit?",
    sub: "Share of the attempts that edited at all, by the call number of their first edit",
    caption: (() => {
      const b = D.runs.baseline.behaviour, e = D.runs["sweep-e1"].behaviour, m = D.runs["sweep-m1"].behaviour;
      return `The pre-registered measure is the median call of the first edit. The early-edit nudge barely moved it (call ${f1(e.first_edit_call.median)} against ${f1(b.first_edit_call.median)}). ` +
        `Each curve covers only the attempts that edited at all: the Flash editor's rests on ${m.first_edit_call.n} attempts, and ${m.never_edited} of its attempts never edited.`;
    })(),
    table: () => {
      const ids = selected();
      return {
        caption: "First-edit call number of each run (attempts that edited at all).",
        cols: [{ key: "r", label: "Run" }, { key: "n", label: "Attempts that edited" }, { key: "never", label: "Never edited" }, { key: "p10", label: "10th pct" }, { key: "med", label: "Median" }, { key: "p90", label: "90th pct" }, { key: "auc", label: "P(passing edits earlier than failing)" }],
        rows: ids.map((id) => { const b = D.runs[id].behaviour, f = b.first_edit_call; return { r: RUN_META[id].short, n: f.n, never: b.never_edited ?? 0, p10: f1(f.p10), med: f1(f.median), p90: f1(f.p90), auc: b.auc_passed_edits_earlier == null ? "n/a" : b.auc_passed_edits_earlier.toFixed(2) }; }),
      };
    },
    draw: (plot) => {
      const d3 = window.d3, ids = selected();
      const w = Math.floor(plot.getBoundingClientRect().width), narrow = w < 560;
      const m = { l: narrow ? 40 : 54, r: 16, t: 14, b: 40 }, H = narrow ? 300 : 360;
      const iw = w - m.l - m.r, ih = H - m.t - m.b;
      const maxX = Math.max(40, ...ids.map((id) => D.runs[id].behaviour.first_edit_ecdf.at(-1)[0]));
      const x = d3.scaleLinear([0, maxX], [0, iw]), y = d3.scaleLinear([0, 1], [ih, 0]);
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", "Cumulative share of attempts that have edited by each call number");
      const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
      for (const t of [0, 0.25, 0.5, 0.75, 1]) { g.append("line").attr("class", "grid").attr("x1", 0).attr("x2", iw).attr("y1", y(t)).attr("y2", y(t)); g.append("text").attr("x", -8).attr("y", y(t) + 4).attr("text-anchor", "end").text(pct(t)); }
      for (let t = 0; t <= maxX; t += 10) g.append("text").attr("x", x(t)).attr("y", ih + 20).attr("text-anchor", "middle").text(t);
      g.append("line").attr("class", "axis").attr("x1", 0).attr("x2", iw).attr("y1", ih).attr("y2", ih);
      g.append("text").attr("x", iw / 2).attr("y", ih + 36).attr("text-anchor", "middle").text("call number of the first edit");
      const line = d3.line().x((p) => x(p[0])).y((p) => y(p[1])).curve(d3.curveStepAfter);
      ids.forEach((id) => {
        const r = D.runs[id], meta = RUN_META[id];
        const pts = [[0, 0], ...r.behaviour.first_edit_ecdf];
        const grp = g.append("g").attr("class", meta.cls).attr("tabindex", 0).attr("role", "img")
          .attr("aria-label", `${meta.short}: median first edit at call ${f1(r.behaviour.first_edit_call.median)}`);
        grp.append("path").attr("d", line(pts)).attr("fill", "none").attr("stroke", meta.partial ? "var(--s-partial)" : "var(--c)").attr("stroke-width", id === "baseline" ? 3 : 2).attr("stroke-linejoin", "round").attr("stroke-dasharray", meta.partial ? "3 4" : meta.open ? "6 4" : null);
        const med = r.behaviour.first_edit_call.median;
        grp.append("circle").attr("cx", x(med)).attr("cy", y(0.5)).attr("r", 5).attr("fill", meta.partial || meta.open ? "var(--page)" : "var(--c)").attr("stroke", "var(--page)").attr("stroke-width", 2);
        grp.append("path").attr("d", line(pts)).attr("fill", "none").attr("stroke", "transparent").attr("stroke-width", 24).attr("class", "hit");
        attachTip(grp.node(), () => `<b>${esc(meta.short)}</b>` + trow("Median first edit", "call " + f1(med)) + trow("10th to 90th pct", `${f1(r.behaviour.first_edit_call.p10)} to ${f1(r.behaviour.first_edit_call.p90)}`) + trow("Edited at all", `${r.behaviour.first_edit_call.n} attempts`) + trow("Never edited", String(r.behaviour.never_edited ?? 0)));
      });
      g.append("text").attr("x", x(0) + 6).attr("y", y(0.5) - 8).text("dots mark each run's median");
      plot.replaceChildren(legendOf(ids), svg.node());
    },
  });
  onSelection(api.redraw);
}

// ---------- stacked bars ----------
function stacked(host, D, { id, title, sub, caption, cats, rowOf }) {
  const api = figure(host, {
    id, title, sub, caption,
    table: () => {
      const ids = selected();
      return {
        caption: title,
        cols: [{ key: "r", label: "Run" }, ...cats.map((c) => ({ key: c.key, label: c.label }))],
        rows: ids.map((rid) => { const row = rowOf(rid); const o = { r: RUN_META[rid].short }; for (const c of cats) o[c.key] = row ? (row.share ? pct(row.vals[c.key], 1) : String(row.vals[c.key])) : "n/a"; return o; }),
      };
    },
    draw: (plot) => {
      const d3 = window.d3, ids = selected().filter((rid) => rowOf(rid));
      const w = Math.floor(plot.getBoundingClientRect().width), narrow = w < 560;
      const labW = narrow ? 112 : 150, rowH = 40, H = ids.length * rowH + 14;
      const iw = w - labW - 10;
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "img").attr("aria-label", title);
      const defs = svg.append("defs");
      const pat = defs.append("pattern").attr("id", "hatch").attr("width", 6).attr("height", 6).attr("patternUnits", "userSpaceOnUse").attr("patternTransform", "rotate(45)");
      pat.append("rect").attr("width", 6).attr("height", 6).attr("fill", "var(--surface)");
      pat.append("line").attr("x1", 0).attr("x2", 0).attr("y1", 0).attr("y2", 6).attr("stroke", "var(--axis)").attr("stroke-width", 2.5);
      ids.forEach((rid, ri) => {
        const row = rowOf(rid), cy = ri * rowH + 6;
        const total = row.share ? 1 : row.total;
        const g = svg.append("g").attr("tabindex", 0).attr("role", "img")
          .attr("aria-label", `${RUN_META[rid].short}: ` + cats.map((c) => `${c.label} ${row.share ? pct(row.vals[c.key], 0) : row.vals[c.key]}`).join(", "));
        g.append("text").attr("x", 0).attr("y", cy + 22).attr("class", "t-strong").text(RUN_META[rid].short);
        let x0 = labW;
        for (const c of cats) {
          const v = row.vals[c.key] || 0; if (!v) continue;
          const wseg = (v / total) * iw;
          const rect = g.append("rect").attr("x", x0 + 1).attr("y", cy + 6).attr("width", Math.max(0, wseg - 2)).attr("height", 24).attr("rx", 3).attr("fill", c.fill).attr("stroke", c.key === "lost" ? "var(--axis)" : "none");
          if (wseg > 30) g.append("text").attr("x", x0 + wseg / 2).attr("y", cy + 22).attr("text-anchor", "middle").attr("fill", textOn(c)).style("fill", textOn(c)).style("font-size", "12px").style("font-weight", 600).text(row.share ? pct(v, 0) : v);
          x0 += wseg;
        }
        g.append("rect").attr("class", "hit").attr("x", 0).attr("y", cy).attr("width", w).attr("height", rowH - 6);
        attachTip(g.node(), () => `<b>${esc(RUN_META[rid].short)}</b>` + cats.map((c) => {
          const v = row.vals[c.key] || 0; if (!v) return "";
          const passed = row.pass && row.pass[c.key] != null ? ` (${row.pass[c.key]} passed)` : "";
          return trow(c.label, (row.share ? pct(v, 1) : v) + passed);
        }).join("") + (row.calls ? trow("Tool calls", String(row.calls)) : ""));
      });
      plot.replaceChildren(catLegend(cats), svg.node());
    },
  });
  onSelection(api.redraw);
}
const textOn = (c) => (c.dark ? "var(--page)" : "var(--ink)");
function catLegend(cats) {
  const n = el("div", { class: "legend" });
  cats.forEach((c) => n.append(el("span", { class: "k" }, el("span", { class: "sq", style: c.key === "lost" ? "background-image:repeating-linear-gradient(45deg,var(--axis) 0 2px,transparent 2px 5px);border-style:dashed" : `background:${c.fill}` }), c.label)));
  return n;
}

// ---------- cumulative cost curves ----------
function curves(D) {
  const cc = D.d.cost_curves || {};
  const api = figure($("#fig-curves"), {
    id: "curves",
    title: "Cumulative cost by call number",
    sub: "Mean over the attempts still running at that call",
    caption: "The baseline is not drawn: it is a substitution of two runs, and its per-call timeline lives in the two separate runs. Points where fewer than five attempts were still running are cut.",
    table: () => {
      const ids = selected().filter((id) => cc[id]);
      return { caption: "Mean cumulative cost (USD) at calls 10, 20, 30 and 40.", cols: [{ key: "r", label: "Run" }, ...[10, 20, 30, 40].map((c) => ({ key: "c" + c, label: "Call " + c }))],
        rows: ids.map((id) => { const o = { r: RUN_META[id].short }; for (const c of [10, 20, 30, 40]) { const p = cc[id].find((q) => q[0] === c && q[2] >= 5); o["c" + c] = p ? usd(p[1]) : "n/a"; } return o; }) };
    },
    draw: (plot) => {
      const d3 = window.d3, ids = selected().filter((id) => cc[id]);
      const w = Math.floor(plot.getBoundingClientRect().width), narrow = w < 560;
      if (!ids.length) { plot.replaceChildren(el("p", { class: "fig-sub" }, "Turn on a run that has a per-call timeline (every run except the baseline and sweep-4).")); return; }
      const m = { l: narrow ? 44 : 54, r: narrow ? 70 : 150, t: 14, b: 40 }, H = narrow ? 300 : 340;
      const iw = w - m.l - m.r, ih = H - m.t - m.b;
      const series = ids.map((id) => ({ id, pts: cc[id].filter((q) => q[2] >= 5) }));
      const maxY = d3.max(series, (s) => d3.max(s.pts, (p) => p[1])) * 1.05;
      const maxX = d3.max(series, (s) => d3.max(s.pts, (p) => p[0]));
      const x = d3.scaleLinear([0, Math.max(40, maxX)], [0, iw]), y = d3.scaleLinear([0, maxY], [ih, 0]).nice();
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", "Mean cumulative cost by call");
      const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
      y.ticks(4).forEach((t) => { g.append("line").attr("class", "grid").attr("x1", 0).attr("x2", iw).attr("y1", y(t)).attr("y2", y(t)); g.append("text").attr("x", -8).attr("y", y(t) + 4).attr("text-anchor", "end").text(usd(t)); });
      for (let t = 0; t <= 40; t += 10) g.append("text").attr("x", x(t)).attr("y", ih + 20).attr("text-anchor", "middle").text(t);
      g.append("line").attr("class", "axis").attr("x1", 0).attr("x2", iw).attr("y1", ih).attr("y2", ih);
      g.append("text").attr("x", iw / 2).attr("y", ih + 36).attr("text-anchor", "middle").text("call number");
      const line = d3.line().x((p) => x(p[0])).y((p) => y(p[1]));
      const ends = [];
      series.forEach((s) => {
        const meta = RUN_META[s.id];
        const grp = g.append("g").attr("class", meta.cls).attr("tabindex", 0).attr("role", "img").attr("aria-label", `${meta.short}: ${usd(s.pts.at(-1)[1])} by call ${s.pts.at(-1)[0]}`);
        grp.append("path").attr("d", line(s.pts)).attr("fill", "none").attr("stroke", meta.partial ? "var(--s-partial)" : "var(--c)").attr("stroke-width", 2.2).attr("stroke-dasharray", meta.partial ? "3 4" : null).attr("stroke-linejoin", "round");
        grp.append("path").attr("d", line(s.pts)).attr("fill", "none").attr("stroke", "transparent").attr("stroke-width", 22).attr("class", "hit");
        const last = s.pts.at(-1);
        ends.push({ id: s.id, y: y(last[1]), last });
        attachTip(grp.node(), () => `<b>${esc(meta.short)}</b>` + trow("By call " + last[0], usd(last[1])) + trow("Attempts still running", String(last[2])));
      });
      ends.sort((a, b) => a.y - b.y);
      for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 15) ends[i].y = ends[i - 1].y + 15;
      ends.forEach((e) => g.append("text").attr("x", x(e.last[0]) + 8).attr("y", e.y + 4).attr("class", "t-strong").text(narrow ? RUN_META[e.id].short.split(" ")[0] : RUN_META[e.id].short));
      plot.replaceChildren(svg.node());
    },
  });
  onSelection(api.redraw);
}

// ---------- cost per pass ----------
function perPass(D) {
  const api = figure($("#fig-cpp"), {
    id: "cpp",
    title: "Dollars per passing fix",
    sub: "Dot: spend in the database divided by passes. Ring: with spend on re-run attempts added back. Line: 95% interval over bugs",
    caption: "m1 looks cheapest per pass only because most of its attempts ended without an edit: it is cheap because it does less, not because it does the same work for less. Prices are Google AI Studio list rates, not billed Vertex rates, and every task is capped at $2.",
    table: () => {
      const ids = selected().filter((id) => !D.runs[id].partial);
      return { caption: "Cost per passing fix (USD).", cols: [{ key: "r", label: "Run" }, { key: "pass", label: "Pass rate" }, { key: "cpp", label: "Per pass" }, { key: "ci", label: "95% interval" }, { key: "arch", label: "With re-run spend" }, { key: "task", label: "Mean per task" }],
        rows: ids.map((id) => { const r = D.runs[id], c = r.cost; return { r: RUN_META[id].short, pass: pct(r.headline.rate), cpp: usd(c.per_pass.usd), ci: `${usd(c.per_pass.ci_bootstrap_instances[0])} to ${usd(c.per_pass.ci_bootstrap_instances[1])}`, arch: usd(c.per_pass_with_archived_usd), task: usd(c.per_task.mean) }; }) };
    },
    draw: (plot) => {
      const d3 = window.d3;
      const all = selected(), ids = all.filter((id) => !D.runs[id].partial);
      const w = Math.floor(plot.getBoundingClientRect().width), narrow = w < 560;
      const labW = narrow ? 120 : 190, rowH = 46, H = ids.length * rowH + 44;
      const iw = w - labW - 16;
      const maxX = d3.max(ids, (id) => D.runs[id].cost.per_pass.ci_bootstrap_instances[1]) * 1.05;
      const x = d3.scaleLinear([0, maxX], [0, iw]).nice();
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "group").attr("aria-label", "Cost per passing fix");
      const g = svg.append("g").attr("transform", `translate(${labW},0)`);
      x.ticks(5).forEach((t) => { g.append("line").attr("class", "grid").attr("x1", x(t)).attr("x2", x(t)).attr("y1", 4).attr("y2", H - 30); g.append("text").attr("x", x(t)).attr("y", H - 12).attr("text-anchor", "middle").text(usd(t, 1)); });
      ids.forEach((id, i) => {
        const r = D.runs[id], c = r.cost, meta = RUN_META[id], cy = 26 + i * rowH;
        const grp = svg.append("g").attr("class", meta.cls).attr("tabindex", 0).attr("role", "img").attr("aria-label", `${meta.short}: ${usd(c.per_pass.usd)} per pass, ${usd(c.per_pass_with_archived_usd)} with re-run spend`);
        grp.append("text").attr("x", 0).attr("y", cy - 2).attr("class", "t-strong").text(meta.short);
        grp.append("text").attr("x", 0).attr("y", cy + 14).text(`${pct(r.headline.rate)} pass · ${usd(c.per_task.mean)} per task`);
        const gm = grp.append("g").attr("transform", `translate(${labW},${cy})`);
        const [lo, hi] = c.per_pass.ci_bootstrap_instances;
        gm.append("line").attr("x1", x(lo)).attr("x2", x(hi)).attr("y1", 0).attr("y2", 0).attr("stroke", "var(--c)").attr("stroke-width", 3).attr("stroke-linecap", "round");
        for (const v of [lo, hi]) gm.append("line").attr("x1", x(v)).attr("x2", x(v)).attr("y1", -6).attr("y2", 6).attr("stroke", "var(--c)").attr("stroke-width", 2.5).attr("stroke-linecap", "round");
        gm.append("circle").attr("cx", x(c.per_pass.usd)).attr("r", 7).attr("fill", "var(--c)").attr("stroke", "var(--page)").attr("stroke-width", 2);
        if (Math.abs(c.per_pass_with_archived_usd - c.per_pass.usd) > 0.005) {
          gm.append("circle").attr("cx", x(c.per_pass_with_archived_usd)).attr("r", 7).attr("fill", "var(--page)").attr("stroke", "var(--c)").attr("stroke-width", 2.5);
        }
        gm.append("rect").attr("class", "hit").attr("x", -labW).attr("y", -rowH / 2).attr("width", iw + labW).attr("height", rowH);
        attachTip(grp.node(), () => `<b>${esc(meta.short)}</b>` + trow("Per pass", usd(c.per_pass.usd)) + trow("With re-run spend", usd(c.per_pass_with_archived_usd)) + trow("Interval", `${usd(lo)} to ${usd(hi)}`) + trow("Per task (mean)", usd(c.per_task.mean)));
      });
      const note = all.length !== ids.length ? el("p", { class: "fig-sub" }, "Unfinished runs are left out: their cost per pass is not comparable.") : null;
      plot.replaceChildren(...[svg.node(), note].filter(Boolean));
    },
  });
  onSelection(api.redraw);
}
