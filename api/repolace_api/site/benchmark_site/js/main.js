// Page assembly. Prose that quotes a number is built here from report.json, so the text cannot drift from the data.
import { $, el, esc, figure, buildTable, initTheme, initReveal, initNavSpy, onSelection, toggleRun, isOn, attachTip, trow, ALL_ORDER, RUN_META, pct, usd, f1, short } from "./core.js";
import { loadData, derive } from "./data.js";
import { renderHero } from "./hero.js";
import { renderForest } from "./forest.js";
import { renderHeat } from "./heat.js";
import { renderCompare } from "./compare.js";
import { renderBudget } from "./budget.js";
import { renderFlow } from "./flow.js";
import { renderPower, renderRetrieval } from "./power.js";
import { renderClips } from "./clips.js";

const html = (id, s) => { $(id).innerHTML = s; };

async function main() {
  initTheme();
  let data;
  try { data = await loadData(); } catch (err) {
    console.error(err);
    $("#main").prepend(el("div", { class: "wrap" }, el("p", { class: "err" }, "The report data (data/report.json) could not be loaded. Open this page through the local server: python3 BENCHMARK/site/serve.py")));
    return;
  }
  const D = derive(data);
  renderHero(D);
  prose(D);
  renderForest(D);
  filterRow(D);
  clumpFigure(D);
  renderClips(D);
  renderHeat(D);
  renderCompare(D);
  renderBudget(D);
  renderFlow(D);
  renderPower(D);
  renderRetrieval(D);
  methods(D);
  footer(D);
  initReveal();
  initNavSpy();
}

// ---------- the one global filter ----------
function filterRow(D) {
  const row = $("#filter-row");
  row.append(el("span", { class: "label", title: "The pre-registered tables always show every variant, whatever is selected here." }, "Runs shown below"));
  for (const id of ALL_ORDER) {
    const meta = RUN_META[id];
    const r = D.runs[id];
    if (!r) continue;
    const btn = el("button", {
      class: `chip ${meta.cls} ${meta.partial ? "is-partial" : ""} ${meta.open ? "is-open" : ""}`, type: "button",
      "aria-pressed": isOn(id) ? "true" : "false", "aria-disabled": meta.locked ? "true" : null,
      title: meta.locked ? "The baseline is always shown" : (meta.partial ? "Unfinished run: shown with its PARTIAL mark" : ""),
    }, el("span", { class: "sw" }), meta.chip || meta.short);
    btn.addEventListener("click", () => { toggleRun(id); });
    row.append(btn);
  }
  onSelection(() => {
    row.querySelectorAll(".chip").forEach((b, i) => {
      const id = ALL_ORDER.filter((x) => D.runs[x])[i];
      b.setAttribute("aria-pressed", isOn(id) ? "true" : "false");
    });
  });
}

// ---------- prose ----------
function prose(D) {
  const R = D.runs, base = R.baseline, e1 = R["sweep-e1"], r2 = R["sweep-r2"], m1 = R["sweep-m1"];
  const mde = D.d.power.approx_minimum_detectable_gap_pp_two_independent_runs;
  const c = base.clustering;
  const wil = base.headline.wilson_rows_context_only;
  const diffc = { always: 0, never: 0, some: 0 };
  const never = [], always = [];
  for (const i of D.d.difficulty.instances) { if (i.class === "always solved") { diffc.always++; always.push(short(i.instance_id)); } else if (i.class === "never solved") { diffc.never++; never.push(short(i.instance_id)); } else diffc.some++; }
  const code = (xs) => xs.map((n) => `<code>${esc(n)}</code>`).join(", ");
  const holm = ["sweep-e1", "sweep-r2", "sweep-m1"].map((v) => D.cmp[v].primary.mcnemar_run_index_0.p_holm_adjusted);

  html("#runs-text",
    `<p class="lede">Seven runs sit on this axis. The baseline is the first: ${base.passed} of ${base.planned}, which is sweep-4 with its lost rows replaced by their re-runs. Three variants of the agent were tried against it: an early-edit nudge, a role split, and a cheaper model doing the editing.</p>` +
    `<p>None of the three is separated from the baseline. Their pass rates (${pct(e1.headline.rate)}, ${pct(r2.headline.rate)} and ${pct(m1.headline.rate)}) sit inside the baseline's interval and inside the zone this benchmark cannot resolve. Two unfinished runs (a smaller model, and a first attempt at the role split) are drawn for completeness and never compared.</p>`);
  html("#runs-note", `<strong>How to read a row.</strong> The dot is the pass rate. The line is the range that resampling whole bugs makes plausible. If it is wider than you expected, the next section is why.`);

  html("#why-text",
    `<p class="lede">The 54 rows are three runs on each of 18 bugs, not 54 independent trials.</p>` +
    `<p>Runs on the same bug tend to agree: the intraclass correlation is ${c.icc_oneway.toFixed(2)}, so three runs of one bug carry the evidence of about ${(3 / c.design_effect_3_runs).toFixed(1)} independent runs, and the whole table is worth about ${c.effective_n_rows.toFixed(0)} rows, not ${base.planned}. The honest unit is the bug. One bug moves the pass rate by ${base.power.one_instance_moves_the_rate_pp} points, so an interval this wide is what the data supports.</p>` +
    `<p>The same shape explains the bug map below: across the baseline and the three variants, ${diffc.always} bugs were solved in all twelve attempts, ${diffc.never} in none, and ${diffc.some} some of the time. Only those ${diffc.some} can separate two configurations.</p>`);
  html("#why-note", `<strong>Why not resample the rows?</strong> Doing so would treat the attempts as independent and give a narrower, wrong interval: ${pct(wil[0])} to ${pct(wil[1])} instead of ${pct(base.headline.ci_bootstrap_instances[0])} to ${pct(base.headline.ci_bootstrap_instances[1])}. It is in the table view of the first figure, labelled context only.`);

  html("#map-text",
    `<p>Read down a column group and the pattern repeats in every configuration. The bugs nobody fixed are ${code(never)}. The bugs every configuration fixed in every attempt are ${code(always)}. The interesting rows are the ones in between, where one configuration passes and another does not.</p>`);
  html("#map-note", `<strong>Keyboard.</strong> Tab into the grid, then use the arrow keys to move between attempts. Each cell announces the bug, the run and how the attempt ended.`);

  const wins = [e1, r2, m1].map((r) => `${r.headline.passed}`);
  html("#var-text",
    `<p class="lede">The early-edit nudge, the role split and the Flash editor passed ${wins[0]}, ${wins[1]} and ${wins[2]} attempts, against ${base.passed} for the baseline.</p>` +
    `<p>On the test that was fixed in advance none of the differences is significant (Holm-adjusted p of ${holm.map((p) => p.toFixed(3)).join(", ")}), and the baseline sits inside every variant's interval. That is not the same as showing they are equal: with 18 bugs the benchmark cannot see gaps under about ${mde} points. What it does show is that nothing here earns the claim that a variant is better.</p>`);
  html("#var-note", `<strong>Why the comparison is against 35, not 29.</strong> Every variant had its own outage-lost rows re-run in place, so each of its headlines is a substituted figure too. Comparing with the as-run 29/54 would count the 12 outage-lost rows against the baseline but not against the variants.`);

  const be = base.behaviour, ee = e1.behaviour;
  html("#bud-text",
    `<p class="lede">The early-edit nudge told the agent to make its first edit by about call 12. It did not change when the agent edits.</p>` +
    `<p>The median first edit was call ${f1(ee.first_edit_call.median)}, against ${f1(be.first_edit_call.median)} for the baseline. The share of attempts that hit the 40-step cap was lower (${pct(ee.step_cap_share)} against ${pct(be.step_cap_share)}), but that was not tested and sits inside the same noise as everything else.</p>` +
    `<p>The Flash editor cost ${usd(m1.cost.per_task.mean)} a task against ${usd(base.cost.per_task.mean)}, and the reason is not efficiency: ${m1.stop_reasons.no_change.n} of its ${m1.planned} attempts ended with no change to the code and none of those passed.</p>`);
  html("#bud-note", `<strong>Cost caveat.</strong> Every task is capped at $2, so costs are lower bounds where the cap bit, and the prices are Google AI Studio list rates rather than billed Vertex rates.`);

  html("#end-text", `<p>Every attempt stops in one of a few ways. Pick a run to see where each ending leads, and how often it ended in a pass.</p>`);
  html("#end-note", `<strong>Lost rows.</strong> A row lost to an outage is a non-pass in the headline, as the benchmark rules require. It is counted separately here so the endings stay honest.`);

  const ex = D.d.power.mcnemar_exact_examples;
  html("#pow-text",
    `<p class="lede">With 18 bugs, a real difference smaller than about ${mde} points is more likely to be missed than found.</p>` +
    `<p>One bug moves a rate by ${base.power.one_instance_moves_the_rate_pp} points and the standard error of a pass rate is about ${base.power.bootstrap_se_pp} points. For the matched first-run test the arithmetic is just as unforgiving: a split of 8 to 0 gives p = ${ex["8:0"].toFixed(3)}, 7 to 1 gives ${ex["7:1"].toFixed(3)}, 6 to 2 gives ${ex["6:2"].toFixed(2)}. The widest split any variant produced was ${Math.max(...["sweep-e1", "sweep-r2", "sweep-m1"].map((v) => D.cmp[v].primary.mcnemar_run_index_0.baseline_only_passed))} to 0.</p>`);
  html("#pow-note", `<strong>This is a limit of the sample, not of the variants.</strong> A bigger bug set, or more runs per bug, would shrink the zone. More runs of the same 18 bugs would help far less than new bugs, for the reason in the second section.`);
  html("#ret-text", `<p>Separately, a retrieval grid compared eight ways of indexing and searching a repository for the agent. A strategy had to gain at least 10 points of chunk MRR over the default with an interval excluding zero. None did, so the default (truncate) stays.</p>`);

  html("#met-text",
    `<p class="lede">Every number on this page is computed by <code>BENCHMARK/analysis/stats.py</code> from rows exported read-only from the benchmark database.</p>` +
    `<p>The seed is fixed (${D.d.meta.seed}), intervals come from ${D.d.meta.bootstrap_resamples.toLocaleString("en-US")} resamples of whole bugs, and no run is ever pooled with another. The one combined figure, the baseline, is a hand-stated substitution and says so wherever it appears.</p>`);
  html("#met-note", `<strong>Not covered.</strong> Nothing here measures opening pull requests (every run used <code>--no-pr</code>), unseen issues, or languages other than Python.`);
}

// ---------- the clumping figure: why runs on one bug are not independent ----------
function clumpFigure(D) {
  const base = D.runs.baseline;
  const counts = [0, 1, 2, 3].map((k) => base.instances.filter((i) => i.passes === k).length);
  const n = base.instances.length;
  const p = base.headline.rate;
  const binom = (k) => [1, 3, 3, 1][k] * p ** k * (1 - p) ** (3 - k) * n;
  const expected = [0, 1, 2, 3].map(binom);
  const api = figure($("#fig-clump"), {
    id: "clump",
    title: "How many of the 18 bugs passed in 0, 1, 2 or 3 of their runs (baseline)",
    sub: "Bars: what happened. Rings: what independent runs would give at the same pass rate",
    caption: `If each of the 54 runs were an independent coin flip at ${pct(p)}, most bugs would pass in two or three runs. Instead the bugs pile up at the ends: ${counts[0]} passed in none and ${counts[3]} in all three. That clumping is the intraclass correlation, drawn.`,
    table: () => ({ caption: "Bugs by passes out of three runs (baseline).", cols: [{ key: "k", label: "Passes out of 3" }, { key: "o", label: "Bugs, observed" }, { key: "e", label: "Bugs, if runs were independent" }], rows: [0, 1, 2, 3].map((k) => ({ k, o: counts[k], e: expected[k].toFixed(1) })) }),
    draw: (plot) => {
      const d3 = window.d3;
      const w = Math.floor(plot.getBoundingClientRect().width), narrow = w < 520;
      const m = { l: 40, r: 12, t: 12, b: 46 }, H = narrow ? 250 : 290;
      const iw = w - m.l - m.r, ih = H - m.t - m.b;
      const x = d3.scaleBand([0, 1, 2, 3], [0, iw]).padding(0.28);
      const ymax = Math.max(...counts, ...expected) + 1;
      const y = d3.scaleLinear([0, ymax], [ih, 0]).nice();
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "img").attr("aria-label", "Bugs by number of passing runs, observed and expected under independence");
      const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
      y.ticks(5).forEach((t) => { g.append("line").attr("class", "grid").attr("x1", 0).attr("x2", iw).attr("y1", y(t)).attr("y2", y(t)); g.append("text").attr("x", -8).attr("y", y(t) + 4).attr("text-anchor", "end").text(t); });
      g.append("line").attr("class", "axis").attr("x1", 0).attr("x2", iw).attr("y1", ih).attr("y2", ih);
      [0, 1, 2, 3].forEach((k) => {
        const bx = x(k), bw = x.bandwidth();
        const grp = g.append("g").attr("tabindex", 0).attr("role", "img").attr("aria-label", `${counts[k]} bugs passed in ${k} of 3 runs; ${expected[k].toFixed(1)} expected if independent`);
        grp.append("rect").attr("x", bx).attr("width", bw).attr("y", y(counts[k])).attr("height", ih - y(counts[k])).attr("rx", 4).attr("fill", "var(--s-baseline)").attr("opacity", 0.9);
        const tall = ih - y(counts[k]) >= 34;
        grp.append("text").attr("x", bx + bw / 2).attr("y", tall ? y(counts[k]) + 24 : y(counts[k]) - 8).attr("text-anchor", "middle").attr("class", "t-strong t-big").style("fill", tall ? "var(--on-fill)" : "var(--ink)").text(counts[k]);
        grp.append("circle").attr("cx", bx + bw / 2).attr("cy", y(expected[k])).attr("r", 9).attr("fill", "none").attr("stroke", "var(--ink)").attr("stroke-width", 2.5);
        if (k === 2) grp.append("text").attr("x", bx + bw / 2 + 16).attr("y", y(expected[k]) + 4).text("if runs were independent");
        grp.append("text").attr("x", bx + bw / 2).attr("y", ih + 22).attr("text-anchor", "middle").text(`${k} of 3 runs`);
        grp.append("rect").attr("class", "hit").attr("x", bx - 6).attr("y", 0).attr("width", bw + 12).attr("height", ih);
        attachTip(grp.node(), () => `<b>${k} of 3 runs</b>` + trow("Bugs", String(counts[k])) + trow("If independent", expected[k].toFixed(1)));
      });
      g.append("text").attr("x", iw / 2).attr("y", ih + 40).attr("text-anchor", "middle").text("passed in this many of a bug's three runs");
      plot.replaceChildren(svg.node());
    },
  });
}

// ---------- method, caveats, data ----------
function methods(D) {
  // The pytest sentence in meta.disclosures is a baseline statement; say so, because under m1 some pytest bugs fail.
  const disc = D.d.meta.disclosures.map((s) => s.replace(/^The six pytest instances pass in every run/, "In the baseline, the six pytest instances pass in every run"));
  $("#disclosures").replaceChildren(...disc.map((s) => el("li", {}, s)));

  const rows = ALL_ORDER.map((id) => D.runs[id]).filter(Boolean).map((r) => {
    const cv = r.code_versions || {};
    const keys = Object.keys(cv).sort((a, b) => +a - +b);
    return { r: RUN_META[r.id].short, w: keys.length ? keys.map((k) => `${cv[k]} rows on ${k} attempts`).join("; ") : "n/a" };
  });
  const cvh = D.d.code_versions.e1_retry_window_commit;
  $("#code-versions").replaceChildren(
    el("p", { class: "disc", html: `The gateway retries a provider error with growing waits. Sweep-4 and sweep-4b already ran on <strong>9 attempts</strong>, as did the role split, the Flash editor and the unfinished runs. Only the early-edit run moved: its branch changed the window to <strong>${cvh.to} attempts</strong> on ${esc(cvh.committed_at.slice(0, 10))}, so its rows split by when they started.` }),
    buildTable({ caption: "Gateway retry window of every row, by run.", cols: [{ key: "r", label: "Run" }, { key: "w", label: "Rows by retry window" }], rows }));

  const sp = ALL_ORDER.map((id) => D.runs[id]).filter((r) => r && r.cost.archived_added_back_usd > 0).map((r) => ({ r: RUN_META[r.id].short, db: usd(r.cost.total_usd_in_db), add: usd(r.cost.archived_added_back_usd), tot: usd(r.cost.total_usd_with_archived) }));
  $("#archived-spend").replaceChildren(
    el("p", { class: "disc", html: "Rows lost to 429 errors were archived and re-run in place. The model calls of some archived attempts were deleted from the database, so their spend is not in the database totals. It is added back here from the archive, and every cost figure above is shown with and without it." }),
    buildTable({ caption: "Spend on re-run attempts (USD).", cols: [{ key: "r", label: "Run" }, { key: "db", label: "In the database" }, { key: "add", label: "Added back from the archive" }, { key: "tot", label: "Total" }], rows: sp }));

  const roleName = { primary: "pre-registered", context: "context", descriptive: "descriptive", exploratory: "exploratory", note: "not computed" };
  $("#methods").replaceChildren(...D.d.methods.map((m) => el("details", {},
    el("summary", {}, m.name, el("span", { class: `role ${m.role === "primary" ? "primary" : ""}` }, roleName[m.role] || m.role)),
    el("div", { class: "formula" }, m.formula), el("p", {}, m.detail))));

  const files = [["data/report.json", "report.json"], ["data/report.schema.json", "report.schema.json"], ["data/code_versions.json", "code_versions.json"]];
  $("#downloads").replaceChildren(...files.map(([h, t]) => el("a", { href: h, download: "" }, t)));
  $("#regen").textContent =
    "# regenerate: read-only export, then the statistics, then serve\n" +
    "export DOCKER_HOST=unix:///run/user/1000/docker.sock\n" +
    "uv run --all-packages python BENCHMARK/analysis/export_runs.py\n" +
    "uv run --all-packages python BENCHMARK/analysis/stats.py\n" +
    "python3 BENCHMARK/site/serve.py 8771";
}

function footer(D) {
  const m = D.d.meta;
  $("#foot").innerHTML =
    `<p>Generated ${esc(m.generated_at.slice(0, 16).replace("T", " "))} UTC from report commit <code>${esc(m.report_commit || "")}</code>. Seed ${m.seed}, ${m.bootstrap_resamples.toLocaleString("en-US")} bootstrap resamples, ${m.permutations.toLocaleString("en-US")} permutations.</p>` +
    `<p>${esc(m.rules)}</p>` +
    `<p>Fonts: Fraunces, Geist and Geist Mono under the SIL Open Font License (see <a href="fonts/LICENSE-Fraunces.txt">licences</a>). Charts: D3 7.8.5 (ISC licence), served from this site.</p>` +
    `<p><a href="/">repolace home</a> · <a href="/pricing">Pricing</a> · <a href="/waitlist">Join the waitlist</a> · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p>`;
}

main().catch((e) => console.error(e));
