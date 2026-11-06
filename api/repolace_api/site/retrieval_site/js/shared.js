// Cell identity (colour = index strategy, mark = query strategy), number formats and the one legend. The plumbing (el, figure,
// tables, tooltip, theme) is the benchmark report's core.js, served from the same site.
import { el } from "/benchmark/js/core.js";

export const INDEX = ["truncate", "head_tail", "windows", "signature_docstring"];
export const IX_COLOR = {
  truncate: "var(--s-baseline)", head_tail: "var(--s-e1)", windows: "var(--s-r2)", signature_docstring: "var(--s-m1)",
};
export const IX_NAME = {
  truncate: "truncate", head_tail: "head_tail", windows: "windows", signature_docstring: "signature_docstring",
};
export const IX_PLAIN = {
  truncate: "keeps the first 128 tokens of each chunk and ignores the rest (today's default)",
  head_tail: "keeps the start and the end of each chunk and drops the middle",
  windows: "cuts each chunk into overlapping windows and averages them",
  signature_docstring: "uses only a function's signature and its documentation, not its body",
};
export const IX_SHORT = { truncate: "first part only", head_tail: "start and end", windows: "overlapping windows", signature_docstring: "signature and docs" };
export const Q_PLAIN = {
  truncate: "keeps the first 128 tokens of the bug text",
  head_tail: "keeps the start and the end of the bug text",
};
/** A cell in words: "windows / truncate" -> "windows (overlapping windows), bug text kept from the start". */
export const plainCell = (key) => { const { s, q } = parse(key); return `${s} (${IX_SHORT[s]}) with the bug text ${q === "truncate" ? "cut to its first part" : "cut to its start and end"}`; };
export const parse = (key) => { const [s, q] = key.split(" / "); return { s, q }; };
export const colorOf = (key) => IX_COLOR[parse(key).s];
export const hollow = (key) => parse(key).q === "head_tail";
export const isBase = (key) => key === "truncate / truncate";

const MINUS = "−";
export const sign = (v, d = 1) => (v < 0 ? MINUS : "+") + Math.abs(v).toFixed(d);
export const fpp = (v, d = 1) => (v == null ? "n/a" : sign(v, d) + " pp");
export const fnum = (v, d = 1) => (v == null ? "n/a" : (v < 0 ? MINUS : "") + Math.abs(v).toFixed(d));
export const ppct = (v, d = 1) => (v == null ? "n/a" : (v * 100).toFixed(d) + "%");
export const ci = (c, f = (x) => ppct(x)) => (c ? `${f(c[0])} to ${f(c[1])}` : "no interval");
export const ciPP = (c) => (c ? `${sign(c[0])} to ${sign(c[1])} pp` : "no interval");
export const pv = (p) => (p == null ? "n/a" : p < 0.001 ? "<0.001" : p.toFixed(3));
export const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
export const short = (id) => id.replace(/^[^_]+__/, "");

/** Draw a cell's mark (filled circle = query truncate, hollow = head_tail) into a d3 selection. */
export function mark(g, key, cx, cy, r = 5.5) {
  const c = colorOf(key);
  return g.append("circle").attr("cx", cx).attr("cy", cy).attr("r", r)
    .attr("fill", hollow(key) ? "var(--surface)" : c).attr("stroke", hollow(key) ? c : "var(--surface)").attr("stroke-width", hollow(key) ? 2.5 : 2);
}

/** Legend HTML node: four index colours and the two query marks. */
export function legend() {
  const idx = el("span", { class: "grp" }, el("span", { class: "lab" }, "How code is prepared (colour; “index strategy”)"),
    INDEX.map((s) => el("span", { class: "k", title: IX_PLAIN[s] }, el("span", { class: "sw", style: `background:${IX_COLOR[s]}` }), IX_NAME[s])));
  const q = el("span", { class: "grp" }, el("span", { class: "lab" }, "How the bug text is shortened (“query strategy”)"),
    el("span", { class: "k" }, el("span", { class: "mk solid" }), "first part (solid mark, solid line)"),
    el("span", { class: "k" }, el("span", { class: "mk" }), "start and end (hollow mark, dashed line)"));
  return el("div", { class: "cell-legend" }, idx, q);
}

/** Put a one-sentence "how to read" above a figure and a one-sentence "what it shows" below it. */
export function notes(host, how, shows) {
  host.prepend(el("p", { class: "how" }, el("b", {}, "How to read this chart. "), how));
  if (shows) host.append(el("p", { class: "takeaway" }, el("b", {}, "What it shows. "), shows));
}

export const pendingBox = (what) => el("div", { class: "pending", role: "note" },
  el("b", {}, "Being computed. "), what);
