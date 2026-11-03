// Instance map: 18 bugs by the three runs of every shown configuration. An HTML grid, so each cell is a real button
// with a real hit area; arrow keys move between cells (roving tabindex), and the table view carries the same data.
import { $, el, figure, onSelection, selected, RUN_META, attachTip, trow, esc, short, usd } from "./core.js";
import { STOP_LABEL } from "./data.js";

export function renderHeat(D) {
  let order = "difficulty";
  const sorter = el("select", { class: "sel", "aria-label": "Sort the bugs by" },
    el("option", { value: "difficulty" }, "Hardest last"),
    el("option", { value: "repo" }, "By repository"));
  sorter.addEventListener("change", () => { order = sorter.value; api.redraw(); });

  const runsShown = () => selected().filter((id) => D.runs[id]);
  const rowsFor = () => {
    const list = [...D.sortedInstances];
    if (order === "repo") list.sort((a, b) => a.repo.localeCompare(b.repo) || a.instance_id.localeCompare(b.instance_id));
    return list;
  };

  const api = figure($("#fig-heat"), {
    id: "heat",
    title: "Every attempt, bug by bug",
    sub: "Rows are bugs, grouped columns are runs, the three cells are the three runs of that bug",
    tools: [sorter],
    caption: "A filled cell is a pass in that run's own colour, an empty cell is a failure, and a hatched dashed cell is a row that was lost to an outage or never finished. Hover or focus a cell for how it ended.",
    table: () => {
      const ids = runsShown();
      return {
        caption: "Passes out of the three runs of each bug.",
        cols: [{ key: "bug", label: "Bug" }, ...ids.map((id) => ({ key: id, label: RUN_META[id].short }))],
        rows: rowsFor().map((i) => ({
          bug: short(i.instance_id),
          ...Object.fromEntries(ids.map((id) => {
            const cells = [0, 1, 2].map((k) => D.task(id, i.instance_id, k));
            const p = cells.filter((t) => t?.bucket === "passed").length;
            const lost = cells.filter((t) => !t || t.bucket === "harness_error" || t.bucket === "unfinished").length;
            return [id, `${p} of 3` + (lost ? ` (${lost} lost or unfinished)` : "")];
          })),
        })),
      };
    },
    draw: (plot) => {
      const ids = runsShown();
      const rows = rowsFor();
      const grid = el("div", { class: "heat-grid", role: "grid", "aria-label": "Outcome of every attempt, by bug and run" });
      const cols = ["max-content", ...ids.flatMap(() => ["28px", "28px", "28px", "16px"])];
      grid.style.gridTemplateColumns = cols.join(" ");
      grid.append(el("div", { class: "corner" }));
      for (const id of ids) {
        const meta = RUN_META[id];
        const g = el("div", { class: `group ${meta.cls} ${meta.partial ? "is-partial" : ""}`, style: "grid-column: span 3" }, meta.short);
        grid.append(g, el("div", { class: "gap" }));
      }
      const cells = [];
      rows.forEach((inst, ri) => {
        const di = D.diff[inst.instance_id];
        const cls = di ? di.class.replace(" solved", "") : "";
        grid.append(el("div", { class: "lab", role: "rowheader" }, short(inst.instance_id), el("span", { class: "cls" }, order === "difficulty" ? "" : inst.repo)));
        ids.forEach((id, ci) => {
          for (let k = 0; k < 3; k++) {
            const t = D.task(id, inst.instance_id, k);
            const b = t ? t.bucket : "unfinished";
            const btn = el("button", {
              class: `cell ${RUN_META[id].cls}`, type: "button", "data-b": b, "data-r": ri, "data-c": ci * 3 + k, tabindex: ri === 0 && ci === 0 && k === 0 ? 0 : -1,
              role: "gridcell", "aria-label": `${short(inst.instance_id)}, ${RUN_META[id].short}, run ${k + 1}: ${label(b)}${t?.stop_reason ? ", " + (STOP_LABEL[t.stop_reason] || t.stop_reason).toLowerCase() : ""}`,
            });
            attachTip(btn, () => `<b>${esc(short(inst.instance_id))}</b>` + trow("Run", `${RUN_META[id].short}, attempt ${k + 1}`) + trow("Outcome", label(b)) +
              (t?.stop_reason ? trow("Ended", STOP_LABEL[t.stop_reason] || t.stop_reason) : "") + (t?.cost != null ? trow("Cost", usd(t.cost)) : "") +
              (t?.calls != null ? trow("Model calls", String(t.calls)) : "") + (t?.first_edit != null ? trow("First edit at call", String(t.first_edit)) : ""));
            cells.push(btn);
            grid.append(btn);
          }
          grid.append(el("div", { class: "gap" }));
        });
      });
      // arrow-key navigation over the cell matrix
      const at = (r, c) => cells.find((n) => +n.dataset.r === r && +n.dataset.c === c);
      grid.addEventListener("keydown", (e) => {
        const t = e.target.closest?.(".cell");
        if (!t) return;
        let r = +t.dataset.r, c = +t.dataset.c;
        const maxC = ids.length * 3 - 1, maxR = rows.length - 1;
        if (e.key === "ArrowRight") c = Math.min(maxC, c + 1);
        else if (e.key === "ArrowLeft") c = Math.max(0, c - 1);
        else if (e.key === "ArrowDown") r = Math.min(maxR, r + 1);
        else if (e.key === "ArrowUp") r = Math.max(0, r - 1);
        else if (e.key === "Home") c = 0;
        else if (e.key === "End") c = maxC;
        else return;
        e.preventDefault();
        const n = at(r, c);
        if (n) { t.tabIndex = -1; n.tabIndex = 0; n.focus(); }
      });
      const legend = el("div", { class: "legend" },
        el("span", { class: "k" }, el("span", { class: "sq pass" }), "passed"),
        el("span", { class: "k" }, el("span", { class: "sq" }), "failed"),
        el("span", { class: "k" }, el("span", { class: "sq lost" }), "lost or unfinished"),
        el("span", { class: "k scrollhint" }, "Scroll the grid sideways for more runs"));
      plot.replaceChildren(el("div", { class: "heat" }, grid), legend);
    },
  });
  onSelection(api.redraw);
}

const label = (b) => ({ passed: "passed", failed: "failed", harness_error: "lost to an outage", unfinished: "unfinished", inadmissible: "inadmissible", passed_with_test_edit: "passed with a test edit" }[b] || b);
