// The one authored moment: the bootstrap distribution assembles, then the interval draws under it.
import { $, el, esc, pct, fit, attachTip, trow } from "./core.js";

export function renderHero(D) {
  const b = D.runs["sweep-4"]; // the official, as-run figure; the 35/54 substitution is the comparisons' baseline and is explained in the note below
  const host = $("#hero-fig");
  const { passed, planned, rate } = b.headline;
  const [lo, hi] = b.headline.ci_bootstrap_instances;
  const nInst = b.instances.length;

  // Guard: the headline sentence is fixed wording; its numbers must be the data's.
  const spans = [...document.querySelectorAll("#headline .n")];
  const want = [planned, nInst, passed];
  spans.forEach((s, i) => { if (s.textContent !== String(want[i])) { console.error("headline mismatch", s.textContent, want[i]); s.textContent = String(want[i]); } });

  // Histogram of the kept resamples, one bin per whole pass count.
  const samples = b.bootstrap_samples;
  const counts = new Map();
  for (const v of samples) { const k = Math.round(v * planned); counts.set(k, (counts.get(k) || 0) + 1); }
  const bins = [...counts.entries()].sort((a, c) => a[0] - c[0]);
  const keptPerResample = Math.round((D.d.meta.bootstrap_resamples || 20000) / samples.length);

  const wrap = el("figure", { class: "fig", style: "margin:0" });
  const plot = el("div", { class: "plot" });
  const tableHost = el("div", { class: "fig-table" });
  const toggle = el("button", { class: "pill", type: "button", "aria-pressed": "false" }, "Table view");
  const cap = el("figcaption", { html:
    `Resampling the ${nInst} bugs with replacement ${D.d.meta.bootstrap_resamples.toLocaleString("en-US")} times gives pass rates from ${pct(bins[0][0] / planned)} to ${pct(bins.at(-1)[0] / planned)}. ` +
    `The middle 95% runs from <strong>${pct(lo)}</strong> to <strong>${pct(hi)}</strong>.` });
  wrap.append(el("div", { class: "fig-head" }, el("span", { class: "fig-title" }, `How much the ${Math.round(rate * 100)}% could move`), el("div", { class: "fig-tools" }, toggle)), el("div", { class: "fig-body" }, plot, tableHost), cap);
  host.replaceChildren(wrap);
  let played = false;

  const draw = (w) => {
    if (!window.d3) return;
    const d3 = window.d3;
    const narrow = w < 560;
    const H = narrow ? 250 : 300;
    const m = { l: 8, r: 8, t: 30, b: narrow ? 92 : 100 };
    const iw = w - m.l - m.r, ih = H - m.t - m.b;
    const x = d3.scaleLinear([0, 1], [0, iw]);
    const maxC = d3.max(bins, (d) => d[1]);
    const y = d3.scaleLinear([0, maxC], [ih, 0]);
    const bw = Math.max(2, x(1 / planned) - x(0));
    const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "img")
      .attr("aria-label", `Histogram of bootstrap pass rates. The 95 percent interval runs from ${pct(lo)} to ${pct(hi)}, around ${passed} of ${planned}.`);
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

    // interval wash behind the bars
    g.append("rect").attr("x", x(lo)).attr("width", x(hi) - x(lo)).attr("y", -6).attr("height", ih + 6).attr("fill", "var(--s-baseline)").attr("opacity", 0.07);
    const bars = g.append("g");
    for (const [k, c] of bins) {
      const inside = k / planned >= lo - 1e-9 && k / planned <= hi + 1e-9;
      const dist = Math.abs(k / planned - rate);
      bars.append("rect").attr("class", "bar")
        .attr("x", x(k / planned) - bw / 2 + 1).attr("width", Math.max(1, bw - 2))
        .attr("y", y(c)).attr("height", ih - y(c)).attr("rx", Math.min(3, bw / 3))
        .attr("fill", inside ? "var(--s-baseline)" : "var(--axis)").attr("opacity", inside ? 0.85 : 0.9)
        .style("--d", Math.round(dist * 1400) + "ms");
    }
    // axis
    const ax = g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`);
    ax.append("line").attr("x1", 0).attr("x2", iw).attr("y1", 0.5).attr("y2", 0.5);
    for (const t of [0, 0.25, 0.5, 0.75, 1]) {
      ax.append("line").attr("x1", x(t)).attr("x2", x(t)).attr("y1", 0).attr("y2", 5);
      ax.append("text").attr("x", x(t)).attr("y", 20).attr("text-anchor", t === 0 ? "start" : t === 1 ? "end" : "middle").text(pct(t));
    }
    // the interval, drawn after the bars
    const by = ih + 52;
    const len = x(hi) - x(lo);
    const br = g.append("g");
    br.append("line").attr("class", "reveal-line").attr("x1", x(lo)).attr("x2", x(hi)).attr("y1", by).attr("y2", by)
      .attr("stroke", "var(--ink)").attr("stroke-width", 2.5).attr("stroke-linecap", "round").style("--len", len);
    for (const [v, anchor, label] of [[lo, "end", pct(lo)], [hi, "start", pct(hi)]]) {
      br.append("line").attr("class", "reveal-fade").attr("x1", x(v)).attr("x2", x(v)).attr("y1", by - 7).attr("y2", by + 7).attr("stroke", "var(--ink)").attr("stroke-width", 2.5).attr("stroke-linecap", "round");
      br.append("text").attr("class", "reveal-fade t-strong t-big").attr("x", x(v) + (anchor === "end" ? -10 : 10)).attr("y", by + 5).attr("text-anchor", anchor).text(label);
    }
    br.append("circle").attr("class", "reveal-fade").attr("cx", x(rate)).attr("cy", by).attr("r", 7).attr("fill", "var(--s-baseline)").attr("stroke", "var(--page)").attr("stroke-width", 2.5);
    br.append("text").attr("class", "reveal-fade").attr("x", x(rate)).attr("y", by - 16).attr("text-anchor", "middle").text(`${passed} of ${planned} = ${pct(rate)}`);
    br.append("text").attr("class", "reveal-fade").attr("x", (x(lo) + x(hi)) / 2).attr("y", by + 30).attr("text-anchor", "middle").text(narrow ? "95% interval over the 18 bugs" : "95% interval, resampling whole bugs");

    // keyboard and pointer access to the numbers
    const hit = g.append("rect").attr("class", "hit").attr("x", 0).attr("y", -6).attr("width", iw).attr("height", ih + 6).attr("tabindex", 0)
      .attr("aria-label", `Resampled pass rates between ${pct(bins[0][0] / planned)} and ${pct(bins.at(-1)[0] / planned)}; interval ${pct(lo)} to ${pct(hi)}`);
    attachTip(hit.node(), () => trow("Point estimate", `${passed}/${planned} = ${pct(rate, 1)}`) + trow("95% interval", `${pct(lo, 1)} to ${pct(hi, 1)}`) + trow("Resamples", D.d.meta.bootstrap_resamples.toLocaleString("en-US")));
    plot.replaceChildren(svg.node());
    if (played) host.classList.add("is-in");
  };

  tableHost.append(tableFor(bins, planned, keptPerResample));
  toggle.addEventListener("click", () => {
    const on = wrap.classList.toggle("is-table");
    toggle.setAttribute("aria-pressed", on ? "true" : "false");
    toggle.textContent = on ? "Chart view" : "Table view";
  });
  fit(plot, draw);

  const play = () => { played = true; host.classList.add("is-in"); };
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || !("IntersectionObserver" in window)) play();
  else {
    // let the fonts settle so the first frame of the moment is the real layout
    (document.fonts?.ready || Promise.resolve()).then(() => requestAnimationFrame(() => requestAnimationFrame(play)));
  }

  // The substitution and subset note sits directly under the figure, as the report rules require.
  const s4 = D.runs["sweep-4"], s4b = D.runs["sweep-4b"];
  $("#hero-note").innerHTML =
    `<strong>${s4.passed} of ${s4.planned} is the official figure, as run.</strong> ${s4.buckets.harness_error} of its rows were lost to Vertex 429 errors and count as non-passes. ` +
    `Sweep-4b re-ran those rows and passed ${s4b.passed}; substituting them gives ${D.runs.baseline.passed}, a hand-stated substitution that the comparisons below use as their baseline, not a single run. ` +
    `The ${nInst} bugs are a filtered subset of SWE-bench Verified that were probably seen in training, so this is not the published Verified score.`;
}

function tableFor(bins, planned, kept) {
  const t = el("table", { class: "data" });
  t.append(el("caption", {}, `Bootstrap pass rates (baseline). Every ${kept}th sorted resample is kept in report.json, so counts are in units of ${kept} resamples.`));
  t.append(el("thead", {}, el("tr", {}, ["Passes out of " + planned, "Pass rate", "Approx. resamples"].map((h) => el("th", { scope: "col" }, h)))));
  t.append(el("tbody", {}, bins.map(([k, c]) => el("tr", {}, [el("td", {}, String(k)), el("td", {}, pct(k / planned, 1)), el("td", {}, String(c * kept))]))));
  return el("div", { class: "tablewrap" }, t);
}
