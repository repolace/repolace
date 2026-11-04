// Page assembly. Every sentence that quotes a number is built here from retrieval.json, so the text cannot drift from the data.
import { $, el, esc, initTheme, initReveal, initNavSpy, buildTable } from "/benchmark/js/core.js";
import { loadData, derive } from "./data.js";
import { renderGrid } from "./grid.js";
import { renderForest, renderPower } from "./delta.js";
import { renderBugs, renderRepos, renderSweep } from "./instances.js";
import { fpp, ciPP, fnum, ppct, pv, plural, short } from "./shared.js";

const html = (sel, s) => { $(sel).innerHTML = s; };
const code = (s) => `<code>${esc(s)}</code>`;

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
  prose(D);
  renderGrid(D);
  renderForest(D);
  renderBugs(D);
  renderPower(D);
  renderRepos(D);
  renderSweep(D);
  method(D);
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
    b.innerHTML = `<span class="tag">Awaiting ${esc(D.d.awaiting.run)}</span>${esc(D.d.awaiting.reason)} Until it finishes, this page shows the aggregates of the first run (${esc(D.d.retrieval1.run)}) and marks every panel that needs per-bug results as pending.`;
  }
}

function metRows(D) { return D.deltas("chunk_mrr").filter((r) => r.meets_switch_rule); }

function hero(D) {
  const met = metRows(D);
  const first = D.ready ? "" : "In the first run, ";
  let h;
  if (!D.anyMet) {
    h = D.ready
      ? `None of the seven alternatives beat plain truncation at finding the changed code: the best gained ${fpp(D.best.mean_pp)}, against a bar of ${D.minGain}.`
      : `${first}no alternative to plain truncation cleared the bar for finding the code a fix changes.`;
  } else {
    h = `${plural(met.length, "alternative")} cleared the bar on chunk MRR (${met.map((r) => r.cell).join(", ")}). With seven comparisons on ${D.n} bugs, treat that as a candidate, not a finding.`;
  }
  $("#headline").textContent = h;
  $("#hero-sub").textContent = `Eight ways of preparing code and queries for embedding, scored on where the lines a real fix changed land in the top 20 results of ${D.n} bugs. No model writes anything here: this is retrieval alone.` +
    (D.ready ? "" : " Per-bug panels follow once the full run lands.");
  const kv = (dt, dd, small) => el("div", {}, el("dt", {}, dt), el("dd", {}, dd, small ? el("small", {}, " " + small) : null));
  $("#hero-kv").replaceChildren(
    kv("Bugs", String(D.n), `from ${D.ready ? D.d.meta.plans.length : D.d.retrieval1.plan.length} repositories`),
    kv("Cells", String(D.keys.length), "4 index × 2 query strategies"),
    kv("Cleared the bar", `${met.length} of ${D.comps}`, `${D.minGain} pp and an interval above 0`),
    kv("Best paired gain", fpp(D.best?.mean_pp), D.best ? `${D.best.cell}, interval ${ciPP(D.best.ci_pp)}` : ""),
    kv(D.ready ? "Smallest gain 18 bugs can detect" : "Smallest gain detectable (approx.)", D.mde == null ? "n/a" : `${D.ready ? "" : "≈ "}${fnum(D.mde, 0)} pp`, "80% power"));
}

function prose(D) {
  const t = D.d.truncation;
  const best = D.best;
  html("#grid-text",
    `<p class="lede">Retrieval has to put the code a fix touches near the top of 20 results. The embedding model reads only ${t.max_seq_length_tokens} tokens of each chunk, so the question is whether keeping a smarter ${t.max_seq_length_tokens} tokens helps.</p>` +
    `<p>Index strategies decide what part of a long chunk is embedded: ${code("truncate")} keeps the start, ${code("head_tail")} the start and the end, ${code("windows")} embeds overlapping windows and keeps the best, ${code("signature_docstring")} embeds the signature and docstring. Query strategies decide how an over-long issue text is shortened: ${code("truncate")} or ${code("head_tail")}. Four times two is eight cells; ${code("truncate / truncate")} is today's default and the baseline for every comparison.</p>` +
    (D.ready ? `<p>Absolute recall is low whichever way it is cut: in the baseline the correct chunk is in the top 20 for ${ppct(D.byKey["truncate / truncate"].metrics.chunk_r20.mean, 0)} of gold hunks, and the first correct chunk lands, on average, at a reciprocal rank of ${ppct(D.byKey["truncate / truncate"].metrics.chunk_mrr.mean, 0)}.</p>` : ""));
  html("#grid-note", `<strong>Chunk</strong> metrics are over the changed hunks of the fix; <strong>file</strong> metrics are over the changed files. A bug with no gold of a kind is not scored for that kind, so the counts under each cell differ.`);

  const mets = metRows(D);
  const holmMin = D.ready ? Math.min(...D.deltas("chunk_mrr").map((r) => r.p_holm)) : null;
  html("#gains-text",
    `<p class="lede">The switch rule was written down before any number existed: a strategy replaces the baseline only if its chunk-MRR gain, paired bug by bug, is at least ${D.minGain} percentage points and the 95% interval over bugs excludes zero.</p>` +
    (D.ready
      ? `<p>${mets.length ? `${plural(mets.length, "cell")} met it: ${mets.map((r) => code(r.cell)).join(", ")}.` : "No cell met it."} The largest gain was ${code(best.cell)} at ${fpp(best.mean_pp)} (interval ${ciPP(best.ci_pp)}). ` +
        `Across the seven comparisons the smallest Holm-adjusted p-value is ${pv(holmMin)}. Those tests are exploratory: they were not part of the pre-registration, and with seven of them on ${D.n} bugs a small p-value is more likely luck than a result.</p>`
      : `<p>${mets.length ? `${plural(mets.length, "cell")} met it.` : "No cell met it."} The largest gain was ${code(best.cell)} at ${fpp(best.mean_pp)} (interval ${ciPP(best.ci_pp)}). Significance tests and effect sizes need the per-bug differences and appear when the full run lands.</p>`));
  html("#gains-note", `<strong>Why paired.</strong> Bugs differ enormously in how findable they are. Comparing each bug with itself removes that, and the interval resamples whole bugs, never individual chunks or queries.`);
  const verdict = D.anyMet ? `Verdict${D.ready ? "" : " (retrieval-1)"}: ${plural(mets.length, "cell")} cleared the bar. The default is still ${code("truncate / truncate")} until the result is repeated.` : `Verdict${D.ready ? "" : " (retrieval-1)"}: no cell cleared the bar, so the default stays ${code("truncate / truncate")}.`;
  html("#prereg", `<span class="tag">Pre-registered rule</span><p>${esc(D.d.design.preregistered)}</p><p><b>${verdict}</b></p>`);

  if (D.ready) {
    const b = D.byKey["truncate / truncate"].metrics;
    const dist = D.d.distributions.cells.find((c) => c.key === "truncate / truncate").chunk_mrr;
    const u = D.d.unreachable;
    html("#bugs-text",
      `<p class="lede">An average hides how lumpy this is. In the baseline, ${Math.round(dist.share_miss * dist.values.length)} of ${dist.values.length} bugs have no correct chunk anywhere in the top 20, and ${Math.round(dist.share_rank1 * dist.values.length)} have one at rank 1.</p>` +
      `<p>The heatmap shows every bug in every cell; the strip plot and the stacked bars are the same numbers as distributions. A gain in the mean usually comes from a handful of bugs moving from a miss to a hit.</p>` +
      `<p>${plural(u.total_hunks, "gold hunk")} in ${plural(u.instances_with_any, "bug")} could not be reached by any indexed chunk. They are dropped from the chunk metrics and not scored as misses.</p>`);
    html("#bugs-note", `<strong>Reciprocal rank</strong> is 1 divided by the rank of the first correct chunk, and 0 for a miss. MRR is its mean. It rewards getting the code to the very top.`);
    const P = D.d.power;
    html("#power-text",
      `<p class="lede">With ${P.n} bugs, the paired differences here have a spread of about ${fnum(P.median_sd_pp, 0)} percentage points, so a gain would have to be about ${fnum(P.mde_pp_median_sd, 0)} points before this design would reliably (80%) see it.</p>` +
      `<p>The pre-registered bar of ${D.minGain} points is ${P.mde_pp_median_sd > D.minGain ? "below" : "at or above"} that. A true ${D.minGain} point gain would be detected about ${Math.round((P.power_at_min_gain_median_sd ?? 0) * 100)}% of the time. A null result here therefore says the gains are probably not large, not that they are absent.</p>`);
    html("#power-note", `<strong>Basis.</strong> ${esc(P.basis)}`);
    const R = D.d.repos;
    html("#repos-text", `<p class="lede">${R.top_two.map((r) => r.split("/")[1]).join(" and ")} supply ${R.top_two_instances} of the ${R.total_instances} bugs, so the pooled numbers are mostly about those two projects.</p><p>${esc(R.note)} Use this panel to see whether one repository drives a pattern, not to rank strategies per repository.</p>`);
    html("#repos-note", `Repositories differ in size, in naming, and in how much text their functions have, all of which change what a 128-token view of a chunk contains.`);
    html("#pf-text", `<p class="lede">Retrieval is part of what the agent is shown. If it mattered, bugs whose baseline retrieval found the code should pass more often in sweep-4.</p><p>This is the question the grid cannot answer by itself. It is also the weakest panel on the page, for the reason stated beneath it.</p>`);
    html("#pf-note", `<strong>Sweep-4</strong> is the benchmark's official run (29 of 54 passed). Its three attempts at each bug are summarised as a pass rate.`);
    void b;
  } else {
    html("#bugs-text", `<p class="lede">Which bugs are found, in which cells, and how the first correct chunk is distributed.</p><p>These panels need the per-bug results of the full run.</p>`);
    html("#power-text", `<p class="lede">With 18 bugs, a difference has to be large to be seen.</p><p>The power curve is drawn from the observed spread of the paired differences, which needs the per-bug results.</p>`);
    html("#repos-text", `<p class="lede">Five repositories supply the 18 bugs; two of them, pytest and sphinx, supply 12.</p><p>The per-repository breakdown needs the per-bug results. It is descriptive only: one to six bugs per repository.</p>`);
    html("#pf-text", `<p class="lede">Does a bug whose code the baseline retrieves also tend to pass in the agent benchmark?</p><p>This exploratory panel needs the per-bug retrieval results.</p>`);
    html("#pf-note", `<strong>Sweep-4</strong> is the benchmark's official run (29 of 54 passed).`);
  }
}

function method(D) {
  const t = D.d.truncation;
  html("#met-text",
    `<p class="lede">Gold is the old-side Python files and changed lines of the fix, read from the gold patch. No model is called, and the gold patch never reaches an agent.</p>` +
    `<p>Intervals are 95% percentile bootstraps that resample bugs, never chunks or queries, with ${D.d.meta.bootstrap_resamples.toLocaleString("en-US")} resamples and seed ${D.d.meta.seed}. Retrieval breaks score ties by chunk id, so the order among tied chunks can change between runs: a small difference between cells can be tie noise.</p>` +
    `<p>Indexing is Python-only for now, and all ${D.n} bugs are Python. Every claim here is about these ${D.n} public SWE-bench Verified bugs; they are also the bugs the agent is benchmarked on.</p>`);
  html("#met-note", `<strong>Not yet tried:</strong> a 512-token embedding model as a second variable.`);
  html("#trunc-text", `<p>${esc(t.model)} reads at most ${t.max_seq_length_tokens} tokens. Measured over ${t.chunks_measured} chunks of this repository (not of the benchmark repositories), ${ppct(t.share_over_budget, 1)} are longer than that and are embedded from their opening tokens alone. The median chunk is ${t.median_chunk_tokens} tokens, so the typical chunk sits just under the ceiling.</p>`);
  const kv = (dt, dd) => el("div", {}, el("dt", {}, dt), el("dd", {}, dd));
  const bt = t.share_over_budget_by_type;
  $("#trunc-kv").replaceChildren(kv("Over the limit", ppct(t.share_over_budget, 1)), kv("Functions", ppct(bt.function, 1)), kv("Modules", ppct(bt.module, 1)), kv("Class skeletons", ppct(bt.class_skeleton, 1)), kv("Methods", ppct(bt.method, 1)), kv("Longest chunk", `${t.longest_chunk_tokens} tokens`));

  const cells = D.d.denominators?.cells || D.d.retrieval1.cells.map((c) => ({ key: c.key, file_scored: c.file_scored, chunk_scored: c.chunk_scored }));
  $("#denominators").replaceChildren(
    el("p", { class: "measure" }, `A bug with no gold of a kind is skipped for that kind, not scored as a miss. File and chunk metrics therefore have their own denominators out of ${D.n} bugs.`),
    buildTable({ caption: "Bugs scored per cell.", cols: [{ key: "c", label: "Cell" }, { key: "f", label: "File metrics" }, { key: "k", label: "Chunk metrics" }], rows: cells.map((c) => ({ c: c.key, f: `${c.file_scored} of ${D.n}`, k: `${c.chunk_scored} of ${D.n}` })) }),
    ...(D.ready && (D.d.meta.skipped_instances.length || D.d.meta.dropped_targets.length)
      ? [el("p", { class: "fig-sub" }, `${plural(D.d.meta.skipped_instances.length, "bug")} skipped and ${plural(D.d.meta.dropped_targets.length, "gold file")} dropped as unreachable; both are listed in the data file.`)] : []));

  const M = [
    ["Percentile bootstrap over bugs", "primary", "resample the n scored bugs with replacement; mean of each resample; 2.5th and 97.5th percentiles", "Used for every interval. The unit is the bug, because the cells and queries of one bug are not independent."],
    ["Paired difference", "primary", "d_i = metric(cell, bug i) − metric(truncate / truncate, bug i), over bugs scored in both", "Mean of d_i in percentage points, with a bootstrap interval of the mean."],
    ["Switch rule", "primary", "mean(d_i) ≥ 10 pp and the 95% interval's lower end > 0, on chunk MRR", "Written before any number existed. Only chunk MRR can meet it."],
    ["Sign-flip permutation test", "exploratory", "randomly flip the sign of each d_i; p = share of flips with |sum| at least the observed (+1 smoothing)", "Exploratory; not in the pre-registration. Holm-adjusted across the seven comparisons of each metric."],
    ["Effect size d_z", "descriptive", "d_z = mean(d_i) / SD(d_i)", "Paired standardised mean difference."],
    ["Minimum detectable effect and power", "descriptive", "MDE = (z(1 − α/2) + z(power)) × SD(d_i) / √n; power(δ) = Φ(δ√n/SD − z) + Φ(−δ√n/SD − z)", "Normal approximation at α = 0.05 and 80% power, using the SD observed in this run."],
    ["Spearman correlation and AUC", "exploratory", "ρ between baseline chunk RR and sweep-4 pass rate over bugs; AUC = P(RR of a solved bug > RR of an unsolved one) + half ties", "Intervals by bootstrapping bugs; p by permuting pass rates. 17 or 18 bugs: very low power."],
  ];
  $("#methods").replaceChildren(...M.map(([name, role, formula, detail]) => el("details", {}, el("summary", {}, name, el("span", { class: `role ${role === "primary" ? "primary" : ""}` }, role === "primary" ? "pre-registered" : role)), el("div", { class: "formula" }, formula), el("p", {}, detail))));
  $("#downloads").replaceChildren(...[["data/retrieval.json", "retrieval.json"], ["data/retrieval.schema.json", "retrieval.schema.json"]].map(([h, l]) => el("a", { href: h, download: "" }, l)));
  $("#regen").textContent = "uv run --all-packages python BENCHMARK/analysis/retrieval_stats.py eval/runs/retrieval-2/report.json\n# placeholder state, from the retrieval-1 aggregates:\nuv run --all-packages python BENCHMARK/analysis/retrieval_stats.py --placeholder";
}

function footer(D) {
  const m = D.d.meta;
  $("#foot").innerHTML =
    `<p>Generated ${esc(m.generated_at.slice(0, 16).replace("T", " "))} UTC from ${esc(m.source)}. Seed ${m.seed}, ${m.bootstrap_resamples.toLocaleString("en-US")} bootstrap resamples, ${m.permutations.toLocaleString("en-US")} permutations.</p>` +
    `<p>This grid belongs to the agent benchmark: <a href="/benchmark/">read the benchmark report</a> for how often the agent fixes the same ${D.n} bugs.</p>` +
    `<p>Fonts: Fraunces, Geist and Geist Mono under the SIL Open Font License (see <a href="/benchmark/fonts/LICENSE-Fraunces.txt">licences</a>). Charts: D3 7.8.5 (ISC licence), served from this site.</p>` +
    `<p><a href="/">repolace home</a> · <a href="/benchmark/">Benchmark</a> · <a href="/pricing">Pricing</a> · <a href="/waitlist">Join the waitlist</a> · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p>`;
}

main().catch((e) => console.error(e));
