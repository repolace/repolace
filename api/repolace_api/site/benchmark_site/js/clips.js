// Four Manim explainers, owned by another stream (video/clip1..4.{mp4,webm,jpg}). The page is finished without them:
// when a file is missing the slot shows its title and transcript instead. Clips play only while on screen, never with
// reduced motion, and always have a pause button and a transcript.
import { $, el } from "./core.js";

export function renderClips(D) {
  const b = D.runs.baseline, c = b.clustering;
  const [lo, hi] = b.headline.ci_bootstrap_instances;
  const mde = D.d.power.approx_minimum_detectable_gap_pp_two_independent_runs;
  const indep = (3 / c.design_effect_3_runs).toFixed(1);
  const defs = {
    1: { title: "Why 54 runs is really 18 bugs", cap: "Resample whole bugs, watch the pass-rate histogram build.",
      text: `The ${b.instances.length} bugs are drawn again with replacement, ${D.d.meta.bootstrap_resamples.toLocaleString("en-US")} times. Each draw gives a pass rate; stacked together they form the histogram. The middle 95% runs from ${(lo * 100).toFixed(0)}% to ${(hi * 100).toFixed(0)}%.` },
    2: { title: "Runs of one bug move together", cap: `Intraclass correlation ${c.icc_oneway.toFixed(2)}: three runs are worth about ${indep} independent ones.`,
      text: `Three runs on the same bug usually agree, so they clump at 0, 1, 2 or 3 passes instead of spreading like 54 independent coin flips. The intraclass correlation is ${c.icc_oneway.toFixed(2)} and the design effect ${c.design_effect_3_runs.toFixed(2)}: the 54 rows carry about as much evidence as ${c.effective_n_rows.toFixed(0)} independent ones.` },
    3: { title: "Only the disagreements count", cap: "Matched bugs flip between baseline and variant; the exact test uses only the flips.",
      text: "Each bug is matched between the baseline and a variant. Bugs that both pass or both fail say nothing about which is better. Only bugs where exactly one passes count, and the exact McNemar test asks how likely that split would be if the two were equal." },
    4: { title: "What forty points means", cap: `A real gap is detected 80% of the time only at about ${mde} points.`,
      text: `Slide the true gap between two configurations from 0 to 60 points. With 18 bugs the chance of detecting it reaches 80% only near ${mde} points, which is why no variant here can be called better or worse on the pre-registered test.` },
  };
  const mount = (host, ids) => {
    host.replaceChildren(...ids.map((n) => clip(n, defs[n])));
  };
  mount($("#clips-why"), [1, 2]);
  mount($("#clips-pair"), [3]);
  mount($("#clips-power"), [4]);
}

function clip(n, def) {
  const stage = el("div", { class: "stage is-empty" });
  const fallback = el("div", { class: "fallback" }, el("b", {}, def.title), el("span", {}, "Animation not rendered yet. The transcript below says what it shows."));
  stage.append(fallback);
  const btn = el("button", { class: "playbtn", type: "button", "aria-label": `Pause or play: ${def.title}` }, "Pause");
  stage.append(btn);
  const fig = el("figure", { class: "clip rise" }, stage,
    el("figcaption", {}, el("strong", {}, def.title + ". "), def.cap),
    el("details", {}, el("summary", {}, "Transcript"), el("p", {}, def.text)));

  const base = `video/clip${n}`;
  // Only wire the player up if the file exists; a 404 must not leave a broken video box.
  fetch(`${base}.mp4`, { method: "HEAD" }).then((r) => {
    if (!r.ok) return;
    const v = el("video", { muted: "", playsinline: "", loop: "", preload: "metadata", poster: `${base}.jpg`, "aria-label": def.title });
    v.muted = true;
    v.append(el("source", { src: `${base}.webm`, type: "video/webm" }), el("source", { src: `${base}.mp4`, type: "video/mp4" }));
    stage.prepend(v);
    stage.classList.remove("is-empty");
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    let userPaused = reduce;
    btn.textContent = reduce ? "Play" : "Pause";
    btn.addEventListener("click", () => {
      if (v.paused) { userPaused = false; v.play().catch(() => {}); btn.textContent = "Pause"; }
      else { userPaused = true; v.pause(); btn.textContent = "Play"; }
    });
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((es) => {
        for (const e of es) {
          if (e.isIntersecting && !userPaused) v.play().catch(() => {});
          else v.pause();
        }
      }, { threshold: 0.45 }).observe(stage);
    } else if (!reduce) v.play().catch(() => {});
  }).catch(() => {});
  return fig;
}
