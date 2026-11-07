// Rendering. Every string from the data goes in through textContent or a text node, never parsed as markup.
import { $, el } from "/benchmark/js/core.js";
import { usd, int, secs, clock, plural } from "./fmt.js";

const SVG = "http://www.w3.org/2000/svg";
const svg = (tag, attrs = {}) => {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

// The app folds the pipeline's stage rows into seven nodes (api/repolace_api/ui/pipeline.py); the same folding here.
const NODES = [
  ["retrieve", "Retrieve", ["instance", "preflight", "embedder", "clone", "index", "retrieve"], "find the code that matters"],
  ["plan", "Plan", [], ""],
  ["edit", "Edit", ["branch", "agent"], "the agent reads, searches and edits"],
  ["test", "Test", ["verify_baseline", "record_baseline", "baseline_files"], "run the repository's tests in a sandbox"],
  ["review", "Review", ["review"], "check the change before it leaves"],
  ["push", "Push", ["squash", "push"], "put the change on a branch"],
  ["pr", "PR", ["pr", "finalize"], "open the pull request"],
];

export function foldStages(d) {
  const scored = d.test_runs.filter((r) => r.attempt >= 1).reduce((a, r) => a + r.duration_seconds, 0);
  return NODES.map(([key, label, names, what]) => {
    const rows = d.stages.filter((s) => names.includes(s.stage));
    let seconds = rows.reduce((a, s) => a + s.seconds, 0);
    if (key === "test") seconds += scored; // scored suites run inside the agent stage
    if (key === "edit") seconds = Math.max(0, seconds - scored);
    const failed = rows.some((s) => s.status === "failed");
    return { key, label, what, seconds, state: key === "plan" ? "skipped" : !rows.length ? "pending" : failed ? "failed" : "ok" };
  });
}

export function hero(d) {
  const pairs = [
    ["Outcome", `${d.outcome} (${d.status.replace("_", " ")})`],
    ["Total time", clock(d.duration_seconds)],
    ["Model cost", usd(d.cost_usd)],
  ];
  const rows = pairs.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v)));
  // The model id is a long identifier: set it in the mono face and let it break only after the slash.
  const slash = d.model.indexOf("/") + 1;
  const model = slash > 0 ? [d.model.slice(0, slash), el("wbr"), d.model.slice(slash)] : [d.model];
  rows.push(el("div", {}, el("dt", {}, "Model"), el("dd", { class: "mono-value" }, ...model)));
  $("#hero-kv").replaceChildren(...rows);
}

export function videoRun(d) {
  const v = d.video_run;
  const box = $("#video-run");
  if (!v || !box) return;
  const n = (v.pr_url || "").match(/\/pull\/(\d+)$/);
  const parts = [
    `The video's own run (task ${v.task_short_id}): ${v.outcome} (${v.status.replace("_", " ")}), ${plural(v.llm_call_count, "model call")}, ${usd(v.cost_usd)}, ${clock(v.duration_seconds)}. `,
  ];
  if (n && /^https:\/\/github\.com\//.test(v.pr_url)) {
    parts.push("Its pull request is ", el("a", { href: v.pr_url, rel: "noopener" }, `#${n[1]}`), `, ${v.pr_note}.`);
  }
  box.replaceChildren(...parts);
}

export function handCheck(text) {
  if (!text) return;
  $("#hand-check-text").textContent = text;
  $("#hand-check").hidden = false;
}

export function issue(d) {
  $("#issue-card").replaceChildren(
    el("h3", {}, d.issue.title),
    el("p", { class: "meta" }, `Issue #${d.issue.number} on ${d.repo}`),
    el("pre", {}, d.issue.body.trim()),
  );
}

export function stages(d) {
  const nodes = foldStages(d);
  const max = Math.max(...nodes.map((n) => n.seconds), 1);
  const longest = nodes.reduce((a, b) => (b.seconds > a.seconds ? b : a));
  $("#stages-text").textContent =
    `The run took ${clock(d.duration_seconds)} from start to pull request. The longest part was ${longest.label} (${secs(longest.seconds)}). The stage names are the ones the app shows; this recording started ${d.started_date}.`;
  $("#timeline").replaceChildren(...nodes.map((n) => {
    if (n.state === "skipped") {
      return el("li", { class: "skipped" }, el("span", { class: "name" }, n.label), el("span", { class: "skip-note" }, "skipped, not used yet"));
    }
    const bar = svg("svg", { viewBox: "0 0 100 10", preserveAspectRatio: "none", role: "img", "aria-label": `${n.label} took ${secs(n.seconds)}` });
    bar.append(svg("rect", { class: "track", x: 0, y: 0, width: 100, height: 10, rx: 3 }));
    bar.append(svg("rect", { class: "fill", x: 0, y: 0, width: Math.max(0.8, (n.seconds / max) * 100).toFixed(2), height: 10, rx: 3 }));
    return el("li", {},
      el("span", { class: "name" }, n.label, el("small", {}, n.what)),
      el("span", { class: "bar" }, bar),
      el("span", { class: "secs" }, n.state === "ok" ? secs(n.seconds) : `${n.state}, ${secs(n.seconds)}`));
  }));
}

function tiles(passed, failed) {
  const total = passed + failed;
  const cell = 22, gap = 5;
  const w = total * (cell + gap) - gap;
  const g = svg("svg", { viewBox: `0 0 ${Math.max(w, 1)} ${cell}`, role: "img", "aria-label": `${passed} passed, ${failed} failed` });
  for (let i = 0; i < total; i++) {
    const ok = i < passed;
    g.append(svg("rect", { class: ok ? "pass" : "fail", x: i * (cell + gap), y: 0, width: cell, height: cell, rx: 4 }));
    if (!ok) {
      g.append(svg("path", { d: `M${i * (cell + gap) + 6} 6 L${i * (cell + gap) + 16} 16 M${i * (cell + gap) + 16} 6 L${i * (cell + gap) + 6} 16`, stroke: "#fff", "stroke-width": 2, fill: "none", "stroke-linecap": "round" }));
    }
  }
  return g;
}

export function tests(d) {
  const base = d.test_runs.find((r) => r.attempt === 0);
  const after = d.test_runs.filter((r) => r.attempt >= 1).at(-1);
  if (!base || !after) return;
  $("#tests-text").textContent =
    `Before the agent touched anything, repolace ran the repository's tests once: ${base.passed_count} passed and ${base.failed_count} failed. After the change it ran them again: ${after.passed_count} passed and ${after.failed_count} failed. Each square is one test; only the counts are recorded here, not the test names.`;
  const row = (label, sub, r) => el("div", { class: "tb" },
    el("div", { class: "lab" }, label, el("small", {}, sub)),
    tiles(r.passed_count, r.failed_count),
    el("div", { class: "nums" }, el("b", {}, `${r.passed_count} passed`), `, ${r.failed_count} failed`, r.errors_count ? `, ${plural(r.errors_count, "collection error")}` : "", ` (${secs(r.duration_seconds)})`));
  $("#testbars").replaceChildren(
    row("Before the change", "baseline at the base commit", base),
    row("After the change", `attempt ${after.attempt}`, after),
    el("div", { class: "tb-key" },
      el("span", {}, el("span", { class: "sw pass" }), "passed"),
      el("span", {}, el("span", { class: "sw fail" }), "failed (crossed)")));
}

export function diffCounts(text) {
  let add = 0, del = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) add++;
    else if (line.startsWith("-")) del++;
  }
  return { add, del };
}

export function diff(d) {
  const { add, del } = diffCounts(d.diff);
  const files = d.diff.split("\n").filter((l) => l.startsWith("diff --git ")).length;
  $("#diff-text").textContent = `The agent changed ${plural(files, "file")} and added ${plural(add, "line")} while removing ${plural(del, "line")}. This is the patch exactly as it went into the pull request.`;
  const lines = d.diff.replace(/\n$/, "").split("\n");
  $("#diff-pre").replaceChildren(...lines.map((line) => {
    let kind = "ctx";
    if (line.startsWith("@@")) kind = "hunk";
    else if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")) kind = "file";
    else if (line.startsWith("+")) kind = "add";
    else if (line.startsWith("-")) kind = "del";
    return el("span", { class: `ln ${kind}` }, line === "" ? " " : line);
  }));
}

export function calls(d) {
  const sum = (k) => d.llm_calls.reduce((a, c) => a + c[k], 0);
  $("#calls-text").textContent =
    `The agent made ${plural(d.llm_calls.length, "model call")} on ${d.model}, ${usd(d.cost_usd)} in total. Input tokens that the provider served from its cache are listed separately and are billed at a lower rate.`;
  const th = (t) => el("th", { scope: "col" }, t);
  const num = (v) => el("td", {}, int(v));
  const table = el("table", { class: "data" },
    el("caption", {}, "Every model call of the run, in order"),
    el("thead", {}, el("tr", {}, th("Call"), th("Stage"), th("Model"), th("Input tokens"), th("of which cached"), th("Output tokens"), th("Cost"))),
    el("tbody", {}, d.llm_calls.map((c) => el("tr", {}, el("td", {}, String(c.n)), el("td", {}, c.stage), el("td", { class: "mono" }, c.model), num(c.input_tokens), num(c.cached_input_tokens), num(c.output_tokens), el("td", {}, usd(c.cost_usd))))),
    el("tfoot", {}, el("tr", {}, el("td", { colspan: 3 }, `Total, ${clock(d.duration_seconds)} of wall time`), num(sum("input_tokens")), num(sum("cached_input_tokens")), num(sum("output_tokens")), el("td", {}, usd(d.cost_usd)))));
  $("#calls-table").replaceChildren(table);
}

export function links(d) {
  // The three GitHub links come from the data, not from the HTML's fallbacks.
  $("#link-pr").href = d.pr_url;
  $("#link-issue").href = d.issue.url;
  $("#link-repo").href = "https://github.com/" + d.repo;
}
