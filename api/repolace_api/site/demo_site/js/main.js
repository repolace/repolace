// Page assembly: the whole page is drawn from data/demo.json.
import { $, el, initTheme } from "/benchmark/js/core.js";
import { loadDemo, loadHandCheck } from "./data.js";
import { hero, handCheck, videoRun, issue, stages, tests, diff, calls, links } from "./render.js";
import { initWalkthrough } from "./walkthrough.js";

async function main() {
  initTheme();
  let d;
  try {
    d = await loadDemo();
  } catch (err) {
    console.error(err);
    $("#main").prepend(el("div", { class: "wrap" }, el("p", { class: "err" }, "The recorded run (data/demo.json) could not be loaded.")));
    return;
  }
  hero(d);
  issue(d);
  stages(d);
  tests(d);
  diff(d);
  calls(d);
  links(d);
  videoRun(d);
  handCheck(await loadHandCheck());
  initWalkthrough();
}

main().catch((e) => console.error(e));
