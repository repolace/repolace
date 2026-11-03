// Shared plumbing: run metadata, the one global run filter, formatting, tooltip, the figure shell with its table twin.

export const RUN_META = {
  baseline: { short: "Baseline", name: "Baseline (35/54, substitution)", cls: "c-baseline", locked: true },
  "sweep-4": { short: "Sweep-4 as run", chip: "Sweep-4 as run", name: "Sweep-4 as run (29/54, 12 rows lost)", cls: "c-sweep-4", open: true },
  "sweep-e1": { short: "Early-edit nudge", name: "Early-edit nudge", cls: "c-sweep-e1" },
  "sweep-r2": { short: "Role split", name: "Role split", cls: "c-sweep-r2" },
  "sweep-m1": { short: "Flash editor", name: "Flash editor, Pro for the rest", cls: "c-sweep-m1" },
  "sweep-5": { short: "Flash, single agent", chip: "Flash (partial)", name: "Flash, single agent", cls: "c-sweep-5", partial: true },
  "sweep-r1": { short: "Role split, first try", chip: "Split v1 (partial)", name: "Role split, first try", cls: "c-sweep-r1", partial: true },
};
export const FULL_ORDER = ["baseline", "sweep-e1", "sweep-r2", "sweep-m1"];
export const ALL_ORDER = ["baseline", "sweep-4", "sweep-e1", "sweep-r2", "sweep-m1", "sweep-5", "sweep-r1"];
export const DEFAULT_ON = new Set(FULL_ORDER);

export const $ = (sel, root = document) => root.querySelector(sel);
export const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k === "style") n.style.cssText = v; // CSSOM, so it is allowed by the CSP; setAttribute("style") is not
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) n.append(kid.nodeType ? kid : document.createTextNode(kid));
  return n;
};
export const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

export const pct = (x, d = 0) => (x == null ? "n/a" : (x * 100).toFixed(d) + "%");
export const pp = (x, d = 1) => (x >= 0 ? "+" : "−") + Math.abs(x * 100).toFixed(d) + " pp";
export const usd = (x, d = 2) => (x == null ? "n/a" : "$" + x.toFixed(d));
export const f1 = (x) => (x == null ? "n/a" : (+x).toFixed(1));
export const f2 = (x) => (x == null ? "n/a" : (+x).toFixed(2));
export const pval = (p) => (p == null ? "n/a" : p < 0.001 ? "<0.001" : p.toFixed(3));
export const short = (id) => id.replace(/^[^_]+__/, "");

// ---------- the one global run filter ----------
const selection = new Set(DEFAULT_ON);
const listeners = new Set();
export const isOn = (id) => selection.has(id);
export const selected = () => ALL_ORDER.filter((id) => selection.has(id));
export const onSelection = (fn) => listeners.add(fn);
export function toggleRun(id) {
  if (RUN_META[id].locked) return;
  selection.has(id) ? selection.delete(id) : selection.add(id);
  listeners.forEach((fn) => fn());
}

// ---------- tooltip: enhances, never gates; focus shows the same as hover ----------
const tip = () => document.getElementById("tip");
export function showTip(html, x, y) {
  const t = tip();
  t.innerHTML = html;
  t.classList.add("on");
  const w = t.offsetWidth, h = t.offsetHeight;
  const left = Math.min(Math.max(8, x + 14), window.innerWidth - w - 8);
  const top = y + 18 + h > window.innerHeight ? Math.max(8, y - h - 14) : y + 18;
  t.style.left = left + "px";
  t.style.top = top + "px";
}
export const hideTip = () => tip().classList.remove("on");
export function attachTip(node, htmlFn) {
  const at = (e) => {
    const r = node.getBoundingClientRect();
    const x = e.clientX ?? r.left + r.width / 2;
    const y = e.clientY ?? r.top + r.height / 2;
    showTip(typeof htmlFn === "function" ? htmlFn() : htmlFn, x, y);
  };
  node.addEventListener("mousemove", at);
  node.addEventListener("mouseenter", at);
  node.addEventListener("mouseleave", hideTip);
  node.addEventListener("focus", (e) => {
    const r = node.getBoundingClientRect();
    showTip(typeof htmlFn === "function" ? htmlFn() : htmlFn, r.left + r.width / 2 - 14, r.top + r.height / 2 - 18);
  });
  node.addEventListener("blur", hideTip);
  node.addEventListener("keydown", (e) => e.key === "Escape" && hideTip());
}
export const trow = (k, v) => `<div class="row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`;

// ---------- redraw when the box changes size ----------
export function fit(host, draw) {
  let last = -1;
  const run = () => {
    const w = Math.floor(host.getBoundingClientRect().width);
    if (w < 40 || w === last) return;
    last = w;
    draw(w);
  };
  new ResizeObserver(() => requestAnimationFrame(run)).observe(host);
  run();
  return () => { last = -1; run(); };
}

// ---------- figure shell: title, sub, table-view twin, caption ----------
export function figure(host, { id, title, sub, caption, draw, table, tools }) {
  const fig = el("figure", { class: "fig", id: id || null });
  const body = el("div", { class: "fig-body" });
  const plot = el("div", { class: "plot" });
  const tableHost = el("div", { class: "fig-table" });
  body.append(plot, tableHost);
  const toggle = el("button", { class: "pill", type: "button", "aria-pressed": "false" }, "Table view");
  const toolsBox = el("div", { class: "fig-tools" }, tools || [], toggle);
  const head = el("div", { class: "fig-head" }, el("div", {}, el("span", { class: "fig-title" }, title), sub ? el("div", { class: "fig-sub" }, sub) : null), toolsBox);
  fig.append(head, body);
  if (caption) fig.append(el("figcaption", { html: caption }));
  host.replaceChildren(fig);

  const renderTable = () => {
    const t = typeof table === "function" ? table() : table;
    tableHost.replaceChildren(buildTable(t));
  };
  toggle.addEventListener("click", () => {
    const on = fig.classList.toggle("is-table");
    toggle.setAttribute("aria-pressed", on ? "true" : "false");
    toggle.textContent = on ? "Chart view" : "Table view";
    if (on) renderTable();
  });
  const redraw = () => {
    if (!window.d3) return;
    try { draw(plot, fig); } catch (err) { console.error("figure", id, err); plot.replaceChildren(el("div", { class: "err" }, "This chart could not be drawn; use the table view.")); }
    if (fig.classList.contains("is-table")) renderTable();
  };
  const redrawSized = () => { fit(plot, () => redraw()); };
  if (!window.d3) {
    plot.replaceChildren(el("div", { class: "err" }, "The chart library did not load. The table view has every number."));
    fig.classList.add("is-table");
    toggle.setAttribute("aria-pressed", "true");
    toggle.textContent = "Chart view";
    renderTable();
  } else {
    redrawSized();
  }
  return { fig, plot, redraw, renderTable };
}

export function buildTable({ caption, cols, rows, foot }) {
  const t = el("table", { class: "data" });
  if (caption) t.append(el("caption", {}, caption));
  t.append(el("thead", {}, el("tr", {}, cols.map((c) => el("th", { scope: "col" }, c.label)))));
  t.append(el("tbody", {}, rows.map((r) => el("tr", {}, cols.map((c) => el("td", {}, r[c.key] ?? ""))))));
  const wrap = el("div", { class: "tablewrap" }, t);
  if (foot) wrap.append(el("p", { class: "fig-sub", style: "margin-top:.6rem" }, foot));
  return wrap;
}

// ---------- theme + reveal ----------
export function initTheme() {
  const btn = document.getElementById("theme");
  const root = document.documentElement;
  const isDark = () => (root.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
  btn.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch (e) { /* private window: the toggle still works for this visit */ }
  });
}
export function initReveal() {
  const nodes = document.querySelectorAll(".rise");
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || !("IntersectionObserver" in window)) {
    nodes.forEach((n) => n.classList.add("is-in"));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); }
  }, { rootMargin: "0px 0px -8% 0px" });
  nodes.forEach((n) => io.observe(n));
}
export function initNavSpy() {
  const links = [...document.querySelectorAll(".topbar nav a")];
  const map = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
  if (!("IntersectionObserver" in window)) return;
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const a = map.get(e.target.id);
      if (a && e.isIntersecting) { links.forEach((l) => l.removeAttribute("aria-current")); a.setAttribute("aria-current", "true"); }
    }
  }, { rootMargin: "-30% 0px -60% 0px" });
  for (const id of map.keys()) { const s = document.getElementById(id); if (s) io.observe(s); }
}
