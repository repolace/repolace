// Failure anatomy: how each way an attempt ended maps to its outcome. One run at a time (the select), ribbons sized by rows.
import { $, el, figure, onSelection, selected, RUN_META, pct, attachTip, trow, esc } from "./core.js";
import { STOP_LABEL, STOP_ORDER } from "./data.js";

const OUT_ORDER = ["passed", "failed", "harness_error", "unfinished"];
const OUT_LABEL = { passed: "Passed", failed: "Failed", harness_error: "Lost to an outage", unfinished: "Unfinished" };

export function renderFlow(D) {
  let pick = "baseline";
  const sel = el("select", { class: "sel", "aria-label": "Run to show" });
  const fill = () => {
    const ids = selected().filter((id) => D.d.failure_flow[id]);
    if (!ids.includes(pick)) pick = ids[0] || "baseline";
    sel.replaceChildren(...ids.map((id) => el("option", { value: id, selected: id === pick ? "" : null }, RUN_META[id].short)));
    sel.value = pick;
  };
  sel.addEventListener("change", () => { pick = sel.value; api.redraw(); });
  fill();

  const api = figure($("#fig-flow"), {
    id: "flow",
    title: "From how it ended to whether it passed",
    sub: "Left: why the attempt stopped. Right: the outcome. Ribbon width is the number of rows",
    tools: [sel],
    caption: (() => {
      const sr = D.runs.baseline.stop_reasons, nc = Object.keys(D.runs).filter((id) => D.runs[id].stop_reasons?.no_change).map((id) => D.runs[id].stop_reasons.no_change);
      const never = nc.length && nc.every((x) => x.passed === 0);
      return `Hitting the step cap is a common way for an attempt to stop, and it is not failure: in the baseline, ${sr.step_cap.passed} of the ${sr.step_cap.n} capped attempts still passed. ` +
        (never ? "An attempt that ended with no change to the code never passed in any run." : "");
    })(),
    table: () => {
      const flows = D.d.failure_flow[pick] || [];
      return { caption: `${RUN_META[pick].short}: rows by how they ended and their outcome.`, cols: [{ key: "s", label: "Ended" }, { key: "o", label: "Outcome" }, { key: "n", label: "Rows" }],
        rows: flows.map((f) => ({ s: STOP_LABEL[f.stop_reason] || f.stop_reason, o: OUT_LABEL[f.bucket] || f.bucket, n: f.n })) };
    },
    draw: (plot) => {
      const d3 = window.d3;
      const flows = (D.d.failure_flow[pick] || []).filter((f) => f.n > 0);
      if (!flows.length) { plot.replaceChildren(el("p", { class: "fig-sub" }, "No flow data for this run.")); return; }
      const w = Math.floor(plot.getBoundingClientRect().width), narrow = w < 560;
      const labW = narrow ? 104 : 168, padY = 10;
      const total = d3.sum(flows, (f) => f.n);
      const H = narrow ? 330 : 360;
      const stops = STOP_ORDER.filter((s) => flows.some((f) => f.stop_reason === s)).concat([...new Set(flows.map((f) => f.stop_reason))].filter((s) => !STOP_ORDER.includes(s)));
      const outs = OUT_ORDER.filter((o) => flows.some((f) => f.bucket === o));
      const avail = H - 24 - padY * Math.max(stops.length, outs.length);
      const k = avail / total;
      const sTot = Object.fromEntries(stops.map((s) => [s, d3.sum(flows.filter((f) => f.stop_reason === s), (f) => f.n)]));
      const oTot = Object.fromEntries(outs.map((o) => [o, d3.sum(flows.filter((f) => f.bucket === o), (f) => f.n)]));
      const place = (list, tot) => { let y = 12; const out = {}; for (const key of list) { out[key] = { y, h: tot[key] * k }; y += tot[key] * k + padY; } return out; };
      const L = place(stops, sTot), R = place(outs, oTot);
      const x0 = labW, x1 = w - labW, nodeW = 12;
      const svg = d3.create("svg").attr("viewBox", `0 0 ${w} ${H}`).attr("role", "img").attr("aria-label", `How ${RUN_META[pick].short} attempts ended and their outcomes`);
      const defs = svg.append("defs");
      const pat = defs.append("pattern").attr("id", "hatch2").attr("width", 6).attr("height", 6).attr("patternUnits", "userSpaceOnUse").attr("patternTransform", "rotate(45)");
      pat.append("rect").attr("width", 6).attr("height", 6).attr("fill", "var(--surface)");
      pat.append("line").attr("x1", 0).attr("x2", 0).attr("y1", 0).attr("y2", 6).attr("stroke", "var(--axis)").attr("stroke-width", 2.5);
      const cur = { l: Object.fromEntries(stops.map((s) => [s, L[s].y])), r: Object.fromEntries(outs.map((o) => [o, R[o].y])) };
      const cls = RUN_META[pick].cls;
      const ribbons = svg.append("g").attr("class", cls);
      const order = [...flows].sort((a, b) => stops.indexOf(a.stop_reason) - stops.indexOf(b.stop_reason) || outs.indexOf(a.bucket) - outs.indexOf(b.bucket));
      for (const f of order) {
        const h = f.n * k, yl = cur.l[f.stop_reason], yr = cur.r[f.bucket];
        cur.l[f.stop_reason] += h; cur.r[f.bucket] += h;
        const mx = (x0 + nodeW + x1) / 2;
        const path = `M${x0 + nodeW},${yl} C${mx},${yl} ${mx},${yr} ${x1},${yr} L${x1},${yr + h} C${mx},${yr + h} ${mx},${yl + h} ${x0 + nodeW},${yl + h} Z`;
        const fillv = f.bucket === "passed" ? "var(--c)" : f.bucket === "failed" ? "color-mix(in srgb, var(--ink) 22%, var(--surface))" : "url(#hatch2)";
        const p = ribbons.append("path").attr("d", path).attr("fill", fillv).attr("opacity", f.bucket === "passed" ? 0.9 : 1).attr("stroke", f.bucket === "passed" ? "none" : "var(--axis)").attr("stroke-width", f.bucket === "passed" ? 0 : 0.75).attr("tabindex", 0).attr("role", "img")
          .attr("aria-label", `${STOP_LABEL[f.stop_reason] || f.stop_reason} then ${OUT_LABEL[f.bucket] || f.bucket}: ${f.n} rows`);
        attachTip(p.node(), () => `<b>${esc(STOP_LABEL[f.stop_reason] || f.stop_reason)} → ${esc(OUT_LABEL[f.bucket] || f.bucket)}</b>` + trow("Rows", String(f.n)) + trow("Share of the run", pct(f.n / total)));
      }
      for (const s of stops) {
        svg.append("rect").attr("x", x0).attr("y", L[s].y).attr("width", nodeW).attr("height", Math.max(2, L[s].h)).attr("rx", 2).attr("fill", "var(--ink)").attr("opacity", 0.75);
        svg.append("text").attr("x", x0 - 10).attr("y", L[s].y + Math.max(2, L[s].h) / 2 - 2).attr("text-anchor", "end").attr("class", "t-strong").text(STOP_LABEL[s] || s);
        svg.append("text").attr("x", x0 - 10).attr("y", L[s].y + Math.max(2, L[s].h) / 2 + 13).attr("text-anchor", "end").text(`${sTot[s]} rows`);
      }
      for (const o of outs) {
        svg.append("rect").attr("x", x1).attr("y", R[o].y).attr("width", nodeW).attr("height", Math.max(2, R[o].h)).attr("rx", 2).attr("fill", o === "passed" ? "var(--c)" : "var(--ink)").attr("opacity", o === "passed" ? 1 : 0.75).attr("class", cls);
        svg.append("text").attr("x", x1 + nodeW + 10).attr("y", R[o].y + Math.max(2, R[o].h) / 2 - 2).attr("class", "t-strong").text(OUT_LABEL[o]);
        svg.append("text").attr("x", x1 + nodeW + 10).attr("y", R[o].y + Math.max(2, R[o].h) / 2 + 13).text(`${oTot[o]} rows`);
      }
      plot.replaceChildren(svg.node());
    },
  });
  onSelection(() => { fill(); api.redraw(); });
}
