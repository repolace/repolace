// Page assembly. Every sentence that quotes a number is built here from retrieval.json, so the text cannot drift from the data.
import { $, el, esc, initTheme, initReveal, initNavSpy, buildTable } from "/benchmark/js/core.js";
import { loadData, derive } from "./data.js";
import { renderGrid } from "./grid.js";
import { renderForest, renderPower } from "./delta.js";
import { renderBugs, renderRepos, renderSweep } from "./instances.js";
import { fpp, ciPP, fnum, ppct, pv, plural, IX_PLAIN, Q_PLAIN, INDEX } from "./shared.js";

const html = (sel, s) => { $(sel).innerHTML = s; };
const code = (s) => `<code>${esc(s)}</code>`;
const cap = (s) => s[0].toUpperCase() + s.slice(1);
const BASE = "truncate / truncate";

async function main() {
  initTheme();
  let data;
  try { data = await loadData(); } catch (err) {
    console.error(err);
    $("#main").prepend(el("div", { class: "wrap" }, el("p", { class: "err" }, "The report data (data/retrieval.json) could not be loaded.")));
    return;
  }
  const D = derive(data);
  banner(D);
  hero(D);
  overview(D);
  prose(D);
  renderGrid(D);
  renderForest(D);
  renderBugs(D);
  renderPower(D);
  renderRepos(D);
  renderSweep(D);
  method(D);
  next(D);
  footer(D);
  initReveal();
  initNavSpy();
}

function banner(D) {
  const b = $("#banner");
  if (D.d.synthetic) {
    b.className = "banner synthetic";
    b.innerHTML = `<span class="tag">Synthetic data</span><b>These numbers come from a seeded random fixture used to test this page.</b> They are not a measurement and must not be quoted.`;
  } else if (!D.ready) {
    b.className = "banner";
    b.innerHTML = `<span class="tag">Per-bug figures in progress</span>${esc(D.d.awaiting.reason)} Until they are ready, this page shows the aggregate grid and marks every panel that needs per-bug results as pending.`;
  }
}

const metRows = (D) => D.deltas("chunk_mrr").filter((r) => r.meets_switch_rule);
const nRepos = (D) => (D.ready ? D.d.meta.plans.length : D.d.aggregate.plan.length);
const baseDist = (D) => D.d.distributions?.cells.find((c) => c.key === BASE)?.chunk_mrr;

function hero(D) {
  const met = metRows(D);
  const best = D.best;
  let h, ans;
  if (!D.anyMet) {
    h = `None of the ${D.comps} changes we tried clearly beat today’s default at finding the right code.`;
    ans = `<b>The short answer. </b>None of the ${D.comps} variants clearly helped. The best one, ${code(best.cell)}, scored ${fnum(best.mean_pp, 1)} points ${best.mean_pp >= 0 ? "higher" : "different"} than the default on average. With only ${D.n} bugs that is inside the noise: its 95% interval runs from ${fnum(best.ci_pp[0], 1)} to ${fnum(best.ci_pp[1], 1)} points, so the true effect could be zero or even negative. We required a gain of ${D.minGain} points that was clearly above zero, and nothing reached it. Nothing changes: the default stays ${code(D.d.design.baseline)}.`;
  } else {
    h = `${plural(met.length, "change")} cleared the bar we set, but with only ${D.n} bugs it is a lead, not a finding.`;
    ans = `<b>The short answer. </b>${met.map((r) => code(r.cell)).join(", ")} gained at least ${D.minGain} points over the default with an interval clearly above zero. With seven comparisons on ${D.n} bugs, treat that as a lead to confirm before changing anything.`;
  }
  $("#headline").textContent = h;
  html("#answer", ans);
  const t = D.d.truncation;
  html("#why", `<b>Why we ran this. </b>Finding the right code is the first step of every fix attempt, and the agent can only work with what search hands it. The search tool’s embedding model reads just the first ${t.max_seq_length_tokens} tokens of each chunk of code, so about ${ppct(t.share_over_budget, 0)} of the chunks we measured are cut off before the model sees them. The question was whether keeping a smarter ${t.max_seq_length_tokens} tokens fixes that.`);
  const kv = (dt, dd, small) => el("div", {}, el("dt", {}, dt), el("dd", {}, dd, small ? el("small", {}, " " + small) : null));
  $("#hero-kv").replaceChildren(
    kv("Bugs tested", String(D.n), `real bugs from ${nRepos(D)} open-source projects`),
    kv("Setups compared", String(D.keys.length), "4 ways to prepare code × 2 ways to shorten the bug text"),
    kv("Cleared the bar", `${met.length} of ${D.comps}`, `needed +${D.minGain} points, clearly above 0`),
    kv("Best gain over the default", fpp(best?.mean_pp), best ? `${best.cell}; 95% interval ${ciPP(best.ci_pp)}` : ""),
    kv(D.ready ? `Smallest gain ${D.n} bugs can reliably show` : "Smallest gain detectable (approx.)", D.mde == null ? "n/a" : `${D.ready ? "" : "≈ "}${fnum(D.mde, 0)} points`, "noticed 80% of the time"));
}

const GLOSSARY = [
  ["Chunk", "A function, a method, a class outline or the top level of a file, cut out of the code so the search can score each piece on its own."],
  ["Embedding", "A list of numbers that a small AI model makes for a piece of text, so that texts with similar meaning get similar numbers. The search compares these lists. This model reads text; it does not write code."],
  ["Token and the 128-token limit", "A token is a word-piece, roughly three or four characters of code. The embedding model reads at most 128 tokens of a chunk and never sees the rest."],
  ["Top 20 results", "For each bug report the search returns its 20 best-matching chunks, best first. Everything on this page is scored on those 20."],
  ["Recall@k (R@5, R@10, R@20)", "The share of the changed code that appears among the first k results. R@5 asks about the first five."],
  ["Reciprocal rank and MRR", "Reciprocal rank is 1 divided by the position of the first correct result. A first right result at position 4 scores 1/4 = 0.25; at position 1 it scores 1; missing from the top 20 it scores 0. MRR is the average over bugs. Chunk MRR counts a hit on the changed code piece, file MRR a hit on the changed file."],
  ["Percentage points (pp)", "The gap between two percentages. Going from 8% to 12% is 4 percentage points."],
  ["95% interval", "The range that the true value plausibly lies in, given the bugs we had. If the range includes 0, the real effect could be nothing."],
  ["Paired comparison", "Each bug is scored under both setups and the two scores are subtracted, so a hard bug does not count against one setup and not the other."],
  ["Bootstrap", "Draw as many bugs as we have at random, with repeats, from the bugs we have. Do that thousands of times and recompute the score each time. The spread of the results gives the interval."],
  ["p-value and Holm correction", "A p-value says how often a gap this big would appear by luck if nothing really differed. Trying seven setups makes a lucky win likelier, so the Holm correction raises the p-values to allow for that."],
  ["Power and minimum detectable effect", "Power is the chance a test notices a real gain. The minimum detectable effect is the smallest real gain it would notice 80% of the time."],
  ["Exploratory", "Not planned before the data existed. Treat it as a hint to check later, not as a result."],
];

function overview(D) {
  html("#overview-text", `<p class="lede">The test asks one narrow question: given only a bug report, does the search put the code that the real fix changed near the top of its results?</p>`);
  const steps = [
    ["Pick a real bug", "Take a bug report from an open-source project whose fix is already known. The lines that fix changed are the answer key."],
    ["Search", "Give the search only the bug report. It scans the project’s code, cut into chunks, and returns its best 20 guesses."],
    ["Check", "Look for the answer-key lines among the 20 results. The nearer the top they appear, the higher the score."],
    ["Repeat and compare", `Do this for ${D.n} bugs in ${nRepos(D)} projects with each of ${D.keys.length} search setups, then compare every setup with today’s default on the same bugs.`],
  ];
  $("#steps").replaceChildren(...steps.map(([t, d]) => el("li", {}, el("b", {}, t), d)));
  html("#no-model", `<b>No AI model writes any code in this test.</b> It measures only the search step that runs before one would.`);

  html("#strategies",
    `<p>A setup has two choices, because both the code and the bug text can be longer than the 128 tokens the model reads. The names in code type are the labels used in the charts and tables.</p>` +
    `<p><b>How the code is prepared</b> (the “index strategy”):</p><dl class="strat">` +
    INDEX.map((s) => `<div><dt>${esc(s)}</dt><dd>${esc(cap(IX_PLAIN[s]))}.${s === "truncate" ? " This is the baseline every other setup is compared with." : ""}</dd></div>`).join("") +
    `</dl><p><b>How the bug text is shortened</b> (the “query strategy”):</p><dl class="strat">` +
    ["truncate", "head_tail"].map((s) => `<div><dt>${esc(s)}</dt><dd>${esc(cap(Q_PLAIN[s]))}.</dd></div>`).join("") +
    `</dl><p>Four times two is eight setups. In charts a setup is written <code>how code is prepared / how bug text is shortened</code>, so ${code(BASE)} is today’s default.</p>`);

  $("#glossary").replaceChildren(...GLOSSARY.map(([t, d]) => el("div", {}, el("dt", {}, t), el("dd", {}, d))));

  const reposTop = D.ready ? `${D.d.repos.top_two.map((r) => r.split("/")[1]).join(" and ")} supply ${D.d.repos.top_two_instances} of them` : "";
  const items = [
    `<b>It is not the agent’s success rate.</b> How often the agent fixes bugs is measured separately, in the <a href="/benchmark/">benchmark report</a>.`,
    `<b>It is only ${D.n} bugs</b> from ${nRepos(D)} Python projects${reposTop ? `; ${reposTop}` : ""}. Small effects cannot be seen with a sample this small.`,
    `<b>The bugs are public benchmark bugs</b>, so a model may have seen them before. They are also the bugs the agent is benchmarked on.`,
    `<b>It tests retrieval alone.</b> A setup that finds code better could still make no difference to whether the agent fixes the bug.`,
    `<b>Only one rule was fixed in advance.</b> The ${D.minGain}-point switch rule was written before any numbers existed. The tests, effect sizes, per-project view and agent link are exploratory.`,
  ];
  $("#notshow").replaceChildren(...items.map((h) => el("li", { html: h })));
}

function prose(D) {
  const best = D.best;
  const base = D.byKey[BASE];
  html("#grid-text",
    `<p class="lede">The table and charts below give the score of every setup on the ${D.n} bugs. Higher is better.</p>` +
    (D.ready
      ? `<p>Search is weak whichever way it is set up. With today’s default, the right code piece shows up in the top 20 for ${ppct(base.metrics.chunk_r20.mean, 0)} of the changed pieces, and the average reciprocal rank is ${ppct(base.metrics.chunk_mrr.mean, 0)}.</p>`
      : "") +
    `<p>Because the numbers come from only ${D.n} bugs, every score carries a wide interval, shown as thin bars in the charts.</p>`);
  html("#grid-note", `<strong>Chunk</strong> scores ask about the changed code pieces; <strong>file</strong> scores ask only about the changed files. A bug with nothing to look for in one of the two is skipped for that score, so the counts under each setup can differ.`);

  const mets = metRows(D);
  const holmMin = D.ready ? Math.min(...D.deltas("chunk_mrr").map((r) => r.p_holm)) : null;
  html("#gains-text",
    `<p class="lede">Before any results existed we wrote down the rule for changing the default: a setup must beat it by at least ${D.minGain} percentage points on average, and the 95% interval must stay entirely above zero.</p>` +
    (D.ready
      ? `<p>${mets.length ? `${plural(mets.length, "setup")} met the rule: ${mets.map((r) => code(r.cell)).join(", ")}.` : "No setup met the rule."} The biggest average gain was ${code(best.cell)} at ${fpp(best.mean_pp)}, with a 95% interval of ${ciPP(best.ci_pp)}.${best.ci_pp[0] <= 0 ? " That interval includes zero." : ""}</p>` +
        `<p>The smallest Holm-adjusted p-value of the seven is ${pv(holmMin)}. These tests are exploratory: they were not in the original plan, and after trying seven setups on ${D.n} bugs a small p-value would be more likely luck than a result.</p>`
      : `<p>${mets.length ? `${plural(mets.length, "setup")} met the rule.` : "No setup met the rule."} The biggest gain was ${code(best.cell)} at ${fpp(best.mean_pp)} (interval ${ciPP(best.ci_pp)}). Significance tests and effect sizes need the per-bug differences and appear when they are ready.</p>`));
  html("#gains-note", `<strong>Why paired?</strong> Bugs differ enormously in how easy they are to find. Scoring each bug under both setups and subtracting removes that, and the interval redraws whole bugs, never single chunks.`);
  html("#prereg", `<span class="tag">Rule written in advance</span><p>${esc(D.d.design.preregistered)}</p><p><b>Verdict: ${D.anyMet ? `${plural(mets.length, "setup")} cleared the bar. The default is still ${code(BASE)} until that is confirmed on more bugs.` : `no setup cleared the bar, so the default stays ${code(BASE)}.`}</b></p>`);

  if (D.ready) {
    const dist = baseDist(D);
    const u = D.d.unreachable;
    const missN = Math.round(dist.share_miss * dist.values.length);
    const r1 = Math.round(dist.share_rank1 * dist.values.length);
    html("#bugs-text",
      `<p class="lede">An average hides how lumpy the results are. With today’s default, the right code is missing from the top 20 for ${missN} of ${dist.values.length} bugs, and it is the very first result for ${r1}.</p>` +
      `<p>The three figures below show the same scores three ways: bug by bug, as dots, and as stacked bars. A gain in the average usually comes from a handful of bugs moving from “missed” to “found”.</p>` +
      (u.total_hunks
        ? `<p>${plural(u.total_hunks, "changed code piece")} in ${plural(u.instances_with_any, "bug")} could not be found by any chunk. They are left out of the chunk scores and not counted as misses.</p>`
        : `<p>Every changed piece of code lay inside some chunk, so none had to be left out of the chunk scores.</p>`));
    html("#bugs-note", `<strong>Reciprocal rank</strong> is 1 divided by the position of the first correct result, and 0 if it is not in the top 20. Position 4 scores 0.25.`);
    const P = D.d.power;
    html("#power-text",
      `<p class="lede">With ${P.n} bugs, the gap between two setups on each bug varies by about ${fnum(P.median_sd_pp, 0)} points. A real gain would have to reach about ${fnum(P.mde_pp_median_sd, 0)} points before a test like ours would notice it 80% of the time.</p>` +
      `<p>Our ${D.minGain}-point bar is ${P.mde_pp_median_sd > D.minGain ? "below" : "at or above"} that, so a real ${D.minGain}-point gain would be noticed only about ${Math.round((P.power_at_min_gain_median_sd ?? 0) * 100)}% of the time. A null result here means “probably no large gain”, not “no gain”.</p>`);
    html("#power-note", `<strong>How this was computed.</strong> ${esc(P.basis)}`);
    const R = D.d.repos;
    html("#repos-text", `<p class="lede">${R.top_two.map((r) => r.split("/")[1]).join(" and ")} supply ${R.top_two_instances} of the ${R.total_instances} bugs, so the overall numbers are mostly about those two projects.</p><p>Each project has only one to six bugs, so these are descriptions, with no intervals and no tests. Use this view to see whether one project drives a pattern, not to rank setups within a project.</p>`);
    html("#repos-note", `Projects differ in size, in naming and in how much text their functions hold. All of that changes what the first 128 tokens of a chunk contain.`);
    html("#pf-text", `<p class="lede">The agent that fixes bugs starts from what search finds. If search mattered, bugs whose code the default search found should pass more often in the agent’s benchmark.</p><p>The grid cannot answer this by itself, and this is the weakest panel on the page, for the reason stated beneath it.</p>`);
    html("#pf-note", `<strong>The agent’s benchmark run</strong> is the official one in the <a href="/benchmark/">benchmark report</a> (29 of 54 attempts passed). Its three attempts at each bug are summarised as a pass rate.`);
  } else {
    html("#bugs-text", `<p class="lede">Which bugs are found, in which setups, and how the first correct chunk is distributed.</p><p>These panels need the per-bug results.</p>`);
    html("#power-text", `<p class="lede">With 18 bugs, a difference has to be large to be seen.</p><p>The power curve is drawn from the observed spread of the paired differences, which needs the per-bug results.</p>`);
    html("#repos-text", `<p class="lede">Five projects supply the 18 bugs; two of them, pytest and sphinx, supply 12.</p><p>The per-project breakdown needs the per-bug results. It is descriptive only: one to six bugs per project.</p>`);
    html("#pf-text", `<p class="lede">Does a bug whose code the default search finds also tend to pass in the agent benchmark?</p><p>This exploratory panel needs the per-bug retrieval results.</p>`);
    html("#pf-note", `<strong>The agent’s benchmark run</strong> is the official one in the <a href="/benchmark/">benchmark report</a> (29 of 54 attempts passed).`);
  }
}

function method(D) {
  const t = D.d.truncation;
  html("#met-text",
    `<p class="lede">The answer key is the old-side Python files and changed lines of the real fix, read from its patch. No model is called, and the patch is never shown to an agent.</p>` +
    `<p>Intervals are 95% percentile bootstraps that redraw bugs, never chunks or queries, with ${D.d.meta.bootstrap_resamples.toLocaleString("en-US")} redraws and seed ${D.d.meta.seed}. Search breaks score ties by chunk id, so the order among tied chunks can change between runs: a small difference between setups can be tie noise.</p>` +
    `<p>Indexing handles Python only for now, and all ${D.n} bugs are Python. The bugs come from SWE-bench Verified, a public collection of real bug fixes; we chose a small subset of it, the same ${D.n} bugs the agent is benchmarked on.</p>`);
  html("#met-note", `<strong>Not yet tried:</strong> an embedding model with a 512-token window as another variable in the grid.`);
  html("#trunc-text", `<p>${esc(t.model)} reads at most ${t.max_seq_length_tokens} tokens. Measured over ${t.chunks_measured} chunks of repolace’s own code (not of the benchmark projects), ${ppct(t.share_over_budget, 1)} are longer than that and are embedded from their opening tokens alone. The median chunk is ${t.median_chunk_tokens} tokens, so the typical chunk sits just under the limit.</p>`);
  const kv = (dt, dd) => el("div", {}, el("dt", {}, dt), el("dd", {}, dd));
  const bt = t.share_over_budget_by_type;
  $("#trunc-kv").replaceChildren(kv("Over the limit", ppct(t.share_over_budget, 1)), kv("Functions", ppct(bt.function, 1)), kv("Modules", ppct(bt.module, 1)), kv("Class outlines", ppct(bt.class_skeleton, 1)), kv("Methods", ppct(bt.method, 1)), kv("Longest chunk", `${t.longest_chunk_tokens} tokens`));

  const cells = D.d.denominators?.cells || D.d.aggregate.cells.map((c) => ({ key: c.key, file_scored: c.file_scored, chunk_scored: c.chunk_scored }));
  $("#denominators").replaceChildren(
    el("p", { class: "measure" }, `A bug with nothing to look for in one kind of score is skipped for that kind, not counted as a miss. File and chunk scores therefore have their own counts out of ${D.n} bugs.`),
    buildTable({ caption: "Bugs scored per setup.", cols: [{ key: "c", label: "Setup" }, { key: "f", label: "File scores" }, { key: "k", label: "Chunk scores" }], rows: cells.map((c) => ({ c: c.key, f: `${c.file_scored} of ${D.n}`, k: `${c.chunk_scored} of ${D.n}` })) }),
    ...(D.ready && (D.d.meta.skipped_instances.length || D.d.meta.dropped_targets.length)
      ? [el("p", { class: "fig-sub" }, `${plural(D.d.meta.skipped_instances.length, "bug")} skipped and ${plural(D.d.meta.dropped_targets.length, "gold file")} dropped as unreachable; both are listed in the data file.`)] : []));

  const M = [
    ["Percentile bootstrap over bugs", "primary", "resample the n scored bugs with replacement; mean of each resample; 2.5th and 97.5th percentiles", "Used for every interval. The unit is the bug, because the setups and queries of one bug are not independent."],
    ["Paired difference", "primary", "d_i = metric(setup, bug i) − metric(truncate / truncate, bug i), over bugs scored in both", "Mean of d_i in percentage points, with a bootstrap interval of the mean."],
    ["Switch rule", "primary", "mean(d_i) ≥ 10 pp and the 95% interval's lower end > 0, on chunk MRR", "Written before any number existed. Only chunk MRR can meet it."],
    ["Sign-flip permutation test", "exploratory", "randomly flip the sign of each d_i; p = share of flips with |sum| at least the observed (+1 smoothing)", "Exploratory; not in the original plan. Holm-adjusted across the seven comparisons of each metric."],
    ["Effect size d_z", "descriptive", "d_z = mean(d_i) / SD(d_i)", "Paired standardised mean difference."],
    ["Minimum detectable effect and power", "descriptive", "MDE = (z(1 − α/2) + z(power)) × SD(d_i) / √n; power(δ) = Φ(δ√n/SD − z) + Φ(−δ√n/SD − z)", "Normal approximation at α = 0.05 and 80% power, using the SD observed in this data."],
    ["Spearman correlation and AUC", "exploratory", "ρ between baseline chunk RR and the agent's pass rate over bugs; AUC = P(RR of a solved bug > RR of an unsolved one) + half ties", "Intervals by redrawing bugs; p by shuffling pass rates. 17 or 18 bugs: very low power."],
  ];
  $("#methods").replaceChildren(...M.map(([name, role, formula, detail]) => el("details", {}, el("summary", {}, name, el("span", { class: `role ${role === "primary" ? "primary" : ""}` }, role === "primary" ? "planned in advance" : role)), el("div", { class: "formula" }, formula), el("p", {}, detail))));
  $("#downloads").replaceChildren(...[["data/retrieval.json", "retrieval.json"], ["data/retrieval.schema.json", "retrieval.schema.json"]].map(([h, l]) => el("a", { href: h, download: "" }, l)));
  $("#regen").textContent = "uv run --all-packages python BENCHMARK/analysis/retrieval_stats.py eval/runs/<run>/report.json";
}

function next(D) {
  html("#next-text",
    `<p class="lede">The default stays ${code(BASE)}: nothing here justifies changing how repolace prepares code for search.</p>` +
    `<p>The next test the project’s notes name is a longer-context embedding model (512 tokens instead of ${D.d.truncation.max_seq_length_tokens}), which would cover most chunks outright and can be combined with any of the four ways of preparing code. Separately, a larger set of bugs would be needed to notice a gain as small as the best one seen here${D.ready ? `, since ${D.n} bugs can reliably show only gains of about ${fnum(D.mde, 0)} points or more` : ""}.</p>`);
}

function footer(D) {
  const m = D.d.meta;
  $("#foot").innerHTML =
    `<p>Generated ${esc(m.generated_at.slice(0, 16).replace("T", " "))} UTC. Seed ${m.seed}, ${m.bootstrap_resamples.toLocaleString("en-US")} bootstrap resamples, ${m.permutations.toLocaleString("en-US")} permutations.</p>` +
    `<p>This search test belongs to the agent benchmark: <a href="/benchmark/">read the benchmark report</a> for how often the agent fixes the same ${D.n} bugs.</p>` +
    `<p>Fonts: Fraunces, Geist and Geist Mono under the SIL Open Font License (see <a href="/benchmark/fonts/LICENSE-Fraunces.txt">licences</a>). Charts: D3 7.8.5 (ISC licence), served from this site.</p>` +
    `<p><a href="/">repolace home</a> · <a href="/benchmark/">Benchmark</a> · <a href="/pricing">Pricing</a> · <a href="/waitlist">Join the waitlist</a> · <a href="/documentation/">Docs</a> · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p>`;
}

main().catch((e) => console.error(e));
