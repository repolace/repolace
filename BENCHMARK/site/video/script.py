"""Four short explainers for the benchmark report. Every number is read from ../data/report.json."""
import json
import math
import random
from pathlib import Path

from manim import *

REPORT = json.loads((Path(__file__).parent / ".." / "data" / "report.json").read_text())

BG, INK, MUTED, HAIR = "#1a1a19", "#f0efec", "#898781", "#2c2f33"
BLUE, ORANGE, AQUA, MAGENTA = "#3987e5", "#d95926", "#199e70", "#d55181"
MONO = "Adwaita Mono"

RUNS = {r["id"]: r for r in REPORT["runs"]}
BASE = RUNS["baseline"]
BASE_PASSES = [i["passes"] for i in BASE["instances"]]
RATE = BASE["headline"]["rate"]
CI = BASE["headline"]["ci_bootstrap_instances"]
SE_PP = BASE["headline"]["se"] * 100
ICC = BASE["clustering"]["icc_oneway"]
DEFF = BASE["clustering"]["design_effect_3_runs"]
EFF_N = BASE["clustering"]["effective_n_rows"]
BOOT = BASE["bootstrap_samples"]


def label(s, size=22, color=MUTED, **kw):
    return Text(s, font=MONO, font_size=size, color=color, **kw)


def run0(run_id):
    """run_index 0 pass flag per instance, in instance order (what the McNemar test pairs)."""
    return [next(o["bucket"] == "passed" for o in i["outcomes"] if o["run_index"] == 0) for i in RUNS[run_id]["instances"]]


def dots_for(outcomes, color, r=0.11):
    """One instance: stacked dots, filled when the run passed, a hollow ring when it did not."""
    ds = []
    for o in outcomes:
        if o == "passed":
            ds.append(Dot(radius=r, color=color))
        else:
            ds.append(Circle(radius=r, stroke_color=MUTED, stroke_width=2, fill_opacity=0))
    return VGroup(*ds).arrange(DOWN, buff=0.12)


def unit_box(group, pad=0.12):
    return RoundedRectangle(corner_radius=0.08, width=group.width + 2 * pad, height=group.height + 2 * pad,
                            stroke_color=HAIR, stroke_width=2, fill_opacity=0).move_to(group)


class Clip1(Scene):
    """Why 54 runs is really 18 instances."""

    def construct(self):
        self.camera.background_color = BG
        rng = random.Random(7)
        outcomes = [[o["bucket"] for o in sorted(i["outcomes"], key=lambda o: o["run_index"])] for i in BASE["instances"]]

        # 54 loose dots
        loose = VGroup(*[dots_for([b], BLUE)[0] for inst in outcomes for b in inst]).arrange_in_grid(rows=6, cols=9, buff=0.34)
        n54 = label("54 rows", 26, INK).next_to(loose, DOWN, buff=0.5)
        self.play(LaggedStart(*[FadeIn(d, scale=0.5) for d in loose], lag_ratio=0.02), FadeIn(n54), run_time=1.6)
        self.wait(0.4)

        # regroup into 18 units of 3
        xs = np.linspace(-6.4, 6.4, 18)
        cols, boxes = [], []
        for x, inst in zip(xs, outcomes):
            g = dots_for(inst, BLUE).move_to([x, 2.45, 0])
            cols.append(g)
            boxes.append(unit_box(g))
        flat = [d for g in cols for d in g]
        n18 = label("18 instances", 26, INK).move_to([0, 3.6, 0])
        self.play(*[Transform(a, b) for a, b in zip(loose, flat)], FadeOut(n54), run_time=1.6)
        self.play(LaggedStart(*[Create(b) for b in boxes], lag_ratio=0.03), FadeIn(n18), run_time=1.0)
        self.wait(0.5)

        # rate axis + resample slots
        ax_y, x0, x1 = -3.1, -5.5, 5.5
        fx = lambda p: x0 + (x1 - x0) * p
        axis = Line([x0, ax_y, 0], [x1, ax_y, 0], stroke_color=HAIR, stroke_width=3)
        ticks = VGroup(*[VGroup(Line([fx(p), ax_y, 0], [fx(p), ax_y - 0.12, 0], stroke_color=MUTED, stroke_width=2),
                                label(f"{int(p * 100)}", 18).move_to([fx(p), ax_y - 0.4, 0])) for p in (0, 0.25, 0.5, 0.75, 1)])
        pct = label("pass rate %", 18).move_to([x0 + 1.0, ax_y + 0.3, 0])
        self.play(Create(axis), FadeIn(ticks), FadeIn(pct), run_time=0.8)

        slot_y = 0.55
        slots = VGroup(*[Square(0.5, stroke_color=HAIR, stroke_width=2).move_to([x, slot_y, 0]) for x in xs])
        redraw_lbl = label("draw 18 instances with replacement", 20).move_to([0, slot_y + 0.85, 0])
        self.play(FadeIn(slots), FadeIn(redraw_lbl), run_time=0.6)

        rate_tex = label("", 24, INK)
        marks = VGroup()
        for k, speed in enumerate((1.4, 0.9, 0.7)):
            pick = [rng.randrange(18) for _ in range(18)]
            copies = VGroup(*[dots_for(outcomes[j], BLUE, r=0.07).move_to([xs[s], slot_y, 0]) for s, j in enumerate(pick)])
            sources = [boxes[j].copy().set_stroke(INK, 3) for j in pick]
            self.play(LaggedStart(*[Succession(FadeIn(src, run_time=0.05), FadeOut(src, run_time=0.1)) for src in sources], lag_ratio=0.35 if k == 0 else 0.1),
                      LaggedStart(*[FadeIn(c, scale=0.4) for c in copies], lag_ratio=0.35 if k == 0 else 0.1), run_time=speed + 0.8)
            p = sum(sum(1 for b in outcomes[j] if b == "passed") for j in pick) / 54
            mark = Dot(radius=0.11, color=INK).move_to([fx(p), ax_y, 0])
            new_lbl = label(f"{p * 100:.0f}%", 24, INK).move_to([0, slot_y - 0.9, 0])
            self.play(FadeIn(mark, shift=DOWN * 0.3), FadeIn(new_lbl) if k == 0 else Transform(rate_tex, new_lbl), run_time=0.5)
            if k == 0:
                rate_tex = new_lbl
            marks.add(mark)
            self.wait(0.25)
            self.play(FadeOut(copies), run_time=0.3)
        faint = VGroup()  # the instance row stays as a quiet backdrop, so the closing frame carries the idea alone
        for x, inst in zip(xs, outcomes):
            g = dots_for(inst, BLUE).move_to([x, 2.45, 0])
            for m in g:
                m.set_fill(opacity=0.3) if m.fill_opacity > 0 else m.set_stroke(opacity=0.45)
            box = unit_box(g).set_stroke(HAIR, opacity=1)
            faint.add(g, box)
        self.play(FadeOut(slots), FadeOut(redraw_lbl), FadeOut(rate_tex), FadeOut(VGroup(*boxes)), FadeIn(faint),
                  n18.animate.set_opacity(0.5), *[FadeOut(d) for d in loose], run_time=0.6)

        # histogram of the stored bootstrap rates
        edges = np.arange(0.2, 1.0001, 0.04)
        counts, _ = np.histogram(BOOT, bins=edges)
        hmax = 2.6
        bars = VGroup(*[Rectangle(width=(fx(edges[i + 1]) - fx(edges[i])) - 0.04, height=max(c, 0.001) / counts.max() * hmax,
                                  stroke_width=0, fill_color=BLUE, fill_opacity=0.55).move_to([(fx(edges[i]) + fx(edges[i + 1])) / 2, ax_y + (c / counts.max() * hmax) / 2, 0])
                        for i, c in enumerate(counts)])
        self.play(LaggedStart(*[GrowFromEdge(b, DOWN) for b in bars], lag_ratio=0.05), FadeOut(marks), run_time=2.2)
        self.wait(0.4)

        # interval closes around the point estimate
        lo, hi = CI
        y_int = ax_y + 3.55
        bracket = VGroup(Line([fx(lo), y_int, 0], [fx(hi), y_int, 0], stroke_color=INK, stroke_width=4),
                         Line([fx(lo), y_int - 0.14, 0], [fx(lo), y_int + 0.14, 0], stroke_color=INK, stroke_width=4),
                         Line([fx(hi), y_int - 0.14, 0], [fx(hi), y_int + 0.14, 0], stroke_color=INK, stroke_width=4))
        guides = VGroup(DashedLine([fx(lo), y_int, 0], [fx(lo), ax_y, 0], stroke_color=INK, stroke_width=1.5, dash_length=0.1),
                        DashedLine([fx(hi), y_int, 0], [fx(hi), ax_y, 0], stroke_color=INK, stroke_width=1.5, dash_length=0.1))
        pt = Dot(radius=0.16, color=BLUE).move_to([fx(RATE), y_int, 0])
        lo_t = label(f"{lo * 100:.0f}%", 28, INK).next_to(bracket[1], UP, buff=0.2)
        hi_t = label(f"{hi * 100:.0f}%", 28, INK).next_to(bracket[2], UP, buff=0.2)
        pt_t = label("35/54", 22, BLUE).next_to(pt, DOWN, buff=0.18)
        self.play(Create(bracket), FadeIn(guides), run_time=1.2)
        self.play(FadeIn(pt, scale=2), FadeIn(lo_t), FadeIn(hi_t), FadeIn(pt_t), run_time=0.8)
        self.wait(2.2)


class Clip2(Scene):
    """Runs of one instance move together."""

    def construct(self):
        self.camera.background_color = BG
        real = [BASE_PASSES.count(k) for k in range(4)]
        p = RATE
        indep = [18 * math.comb(3, k) * p ** k * (1 - p) ** (3 - k) for k in range(4)]
        base_y, hscale, bw = -1.2, 0.42, 1.25
        xs = [-4.5, -1.5, 1.5, 4.5]

        axis = Line([-6.2, base_y, 0], [6.2, base_y, 0], stroke_color=HAIR, stroke_width=3)
        trip = VGroup(*[dots_for(["passed"] * k + ["failed"] * (3 - k), BLUE, r=0.1).rotate(PI / 2).move_to([x, base_y - 0.55, 0]) for x, k in zip(xs, range(4))])
        self.play(Create(axis), LaggedStart(*[FadeIn(t) for t in trip], lag_ratio=0.15), run_time=1.0)
        cap = label("instances by how many of their 3 runs passed", 22).move_to([0, 3.55, 0])
        self.play(FadeIn(cap), run_time=0.5)

        # outlined: if runs were independent coin flips at 35/54
        out = VGroup(*[Rectangle(width=bw, height=max(v * hscale, 0.01), stroke_color=MUTED, stroke_width=3, fill_opacity=0)
                       .move_to([x, base_y + v * hscale / 2, 0]) for x, v in zip(xs, indep)])
        tag_i = VGroup(Line([0, 0, 0], [0.5, 0, 0], stroke_color=MUTED, stroke_width=3), label("independent runs", 20)).arrange(RIGHT, buff=0.2)
        self.play(LaggedStart(*[Create(o) for o in out], lag_ratio=0.2), run_time=1.6)
        tag_i.move_to([-4.6, 2.7, 0])
        self.play(FadeIn(tag_i), run_time=0.4)
        self.wait(0.8)

        # filled: the real counts
        bars = VGroup(*[Rectangle(width=bw - 0.1, height=max(v * hscale, 0.001), stroke_width=0, fill_color=BLUE, fill_opacity=0.9)
                        .move_to([x, base_y + v * hscale / 2, 0]) for x, v in zip(xs, real)])
        nums = VGroup(*[label(str(v), 28, INK).move_to([x, base_y + v * hscale + 0.38, 0]) for x, v in zip(xs, real)])
        tag_r = VGroup(Dot(radius=0.1, color=BLUE), label("the 18 real instances", 20, INK)).arrange(RIGHT, buff=0.2).move_to([-3.6, 2.2, 0])
        self.play(LaggedStart(*[GrowFromEdge(b, DOWN) for b in bars], lag_ratio=0.15), FadeIn(tag_r), run_time=1.8)
        self.play(FadeIn(nums), run_time=0.5)
        self.wait(1.0)

        # the cost of that: the design effect
        formula = MathTex(r"1+(3-1)\,\rho", r"=", f"{DEFF:.2f}", color=INK).move_to([-3.4, -2.95, 0])
        rho = MathTex(rf"\rho={ICC:.3f}", color=MUTED).scale(0.75).next_to(formula, DOWN, buff=0.2)
        eff = MathTex(r"54\ \text{rows}\ \approx", f"{EFF_N:.1f}", r"\ \text{independent}", color=INK).scale(0.85).move_to([3.4, -2.95, 0])
        eff[1].set_color(BLUE)
        formula[2].set_color(BLUE)
        self.play(Write(formula), run_time=1.2)
        self.play(FadeIn(rho), Write(eff), run_time=1.2)
        self.wait(3.4)


class Clip3(Scene):
    """Only the disagreements count (exact McNemar, run 0)."""

    def construct(self):
        self.camera.background_color = BG
        comp = next(c for c in REPORT["comparisons"] if c["variant"] == "sweep-e1")
        m = comp["primary"]["mcnemar_run_index_0"]
        base0, var0 = run0("baseline"), run0("sweep-e1")
        assert (sum(b and not v for b, v in zip(base0, var0)), sum(v and not b for b, v in zip(base0, var0))) == (m["baseline_only_passed"], m["variant_only_passed"])
        n = len(base0)
        ys = np.linspace(2.9, -3.1, n)
        xl, xr = -4.4, -0.4

        def dot(passed, color):
            return Dot(radius=0.1, color=color) if passed else Circle(radius=0.1, stroke_color=MUTED, stroke_width=2, fill_opacity=0)

        left = VGroup(*[dot(b, BLUE).move_to([xl, y, 0]) for b, y in zip(base0, ys)])
        right = VGroup(*[dot(v, ORANGE).move_to([xr, y, 0]) for v, y in zip(var0, ys)])
        links = VGroup(*[Line([xl, y, 0], [xr, y, 0], stroke_color=HAIR, stroke_width=2) for y in ys])
        head_l = label("baseline", 22, BLUE).move_to([xl, 3.55, 0])
        head_r = label("e1", 22, ORANGE).move_to([xr, 3.55, 0])
        self.play(FadeIn(head_l), FadeIn(head_r), Create(links, lag_ratio=0.03), run_time=1.0)
        self.play(LaggedStart(*[FadeIn(d, scale=0.5) for d in left], lag_ratio=0.03), LaggedStart(*[FadeIn(d, scale=0.5) for d in right], lag_ratio=0.03), run_time=1.4)
        self.wait(0.8)

        conc = [i for i in range(n) if base0[i] == var0[i]]
        disc = [i for i in range(n) if base0[i] != var0[i]]
        conc_lbl = VGroup(label("same result in both:", 22), label("says nothing about the gap", 22)).arrange(DOWN, buff=0.15).move_to([4.2, 1.3, 0])
        self.play(*[VGroup(left[i], right[i], links[i]).animate.set_opacity(0.12) for i in conc], FadeIn(conc_lbl), run_time=1.6)
        self.wait(0.6)
        rings = VGroup(*[SurroundingRectangle(VGroup(left[i], right[i]), color=INK, buff=0.18, stroke_width=2) for i in disc])
        self.play(Create(rings), *[links[i].animate.set_stroke(INK, 3) for i in disc], FadeOut(conc_lbl), run_time=0.9)

        split = Text(f"{m['baseline_only_passed']} : {m['variant_only_passed']}", font=MONO, weight=BOLD, font_size=88, color=INK).move_to([4.2, 1.5, 0])
        who = label("baseline only : e1 only", 20).next_to(split, DOWN, buff=0.25)
        ptex = MathTex(rf"p={m['p_exact_two_sided']:g}", color=INK).scale(1.5).next_to(who, DOWN, buff=0.55)
        self.play(FadeIn(split, shift=LEFT * 0.4), FadeIn(who), run_time=1.0)
        self.play(Write(ptex), run_time=0.9)
        self.wait(0.7)

        ex = REPORT["power"]["mcnemar_exact_examples"]
        rows = VGroup(*[VGroup(label(k, 22, INK), label(f"p={v:.2g}", 22, MUTED)).arrange(RIGHT, buff=0.4) for k, v in ex.items()]).arrange(DOWN, aligned_edge=LEFT, buff=0.22)
        rows.move_to([4.2, -2.5, 0])
        ref = label("what it would take", 18).next_to(rows, UP, buff=0.3).align_to(rows, LEFT)
        self.play(FadeIn(ref), LaggedStart(*[FadeIn(r, shift=UP * 0.15) for r in rows], lag_ratio=0.3), run_time=1.4)
        self.wait(3.4)


class Clip4(Scene):
    """What about 40 points means at 18 instances."""

    def construct(self):
        self.camera.background_color = BG
        sd = math.sqrt(2) * SE_PP
        Phi = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))
        power = lambda g: Phi(g / sd - 1.96)
        mde = REPORT["power"]["approx_minimum_detectable_gap_pp_two_independent_runs"]

        ax = Axes(x_range=[0, 60, 10], y_range=[0, 1, 0.2], x_length=10.6, y_length=5.2,
                  axis_config={"color": HAIR, "stroke_width": 3, "include_ticks": False, "include_tip": False}).move_to([0.3, -0.1, 0])
        xt = VGroup(*[label(str(g), 18).next_to(ax.c2p(g, 0), DOWN, buff=0.2) for g in range(0, 61, 10)])
        yt = VGroup(*[label(f"{int(v * 100)}", 18).next_to(ax.c2p(0, v), LEFT, buff=0.2) for v in (0, 0.2, 0.4, 0.6, 0.8, 1.0)])
        xl = label("gap in pass rate, points", 18).next_to(ax, DOWN, buff=0.75).align_to(ax, RIGHT)
        yl = label("chance of detecting it, %", 18).next_to(ax, UP, buff=0.3).align_to(ax, LEFT).shift(LEFT * 0.8)
        grid = VGroup(*[Line(ax.c2p(0, v), ax.c2p(60, v), stroke_color=HAIR, stroke_width=1) for v in (0.2, 0.4, 0.6, 0.8, 1.0)])
        self.play(FadeIn(grid), Create(ax), FadeIn(xt), FadeIn(yt), FadeIn(xl), FadeIn(yl), run_time=1.2)

        curve = ax.plot(power, x_range=[0, 60], color=INK, stroke_width=4, use_smoothing=False)
        self.play(Create(curve), run_time=2.0, rate_func=linear)
        self.wait(0.4)

        h80 = DashedLine(ax.c2p(0, 0.8), ax.c2p(mde, 0.8), stroke_color=MUTED, stroke_width=2, dash_length=0.12)
        v39 = DashedLine(ax.c2p(mde, 0.8), ax.c2p(mde, 0), stroke_color=MUTED, stroke_width=2, dash_length=0.12)
        pt = Dot(ax.c2p(mde, 0.8), radius=0.13, color=BLUE)
        t80 = label("80%", 22, INK).next_to(ax.c2p(0, 0.8), UP, buff=0.12).shift(RIGHT * 0.5)
        t39 = label(f"about {mde:.0f} points", 26, BLUE).move_to(ax.c2p(49, 0.12))
        self.play(Create(h80), Create(v39), run_time=1.0)
        self.play(FadeIn(pt, scale=2.5), FadeIn(t80), FadeIn(t39), run_time=0.8)
        self.wait(0.8)

        # where the variants actually landed
        obs = []
        for rid, name, col in (("sweep-e1", "e1", ORANGE), ("sweep-r2", "r2", AQUA), ("sweep-m1", "m1", MAGENTA)):
            c = next(c for c in REPORT["comparisons"] if c["variant"] == rid)
            obs.append((name, col, abs(c["variant_rate"] - c["baseline_rate"]) * 100))
        dots = VGroup()
        for name, col, g in obs:
            d = Dot(ax.c2p(g, power(g)), radius=0.12, color=col)
            t = label(name, 22, col).next_to(d, UP, buff=0.2).shift(LEFT * 0.3 if name == "e1" else RIGHT * 0.25 if name == "r2" else ORIGIN)
            dots.add(VGroup(d, t))
        self.play(LaggedStart(*[FadeIn(x, scale=2) for x in dots], lag_ratio=0.4), run_time=1.6)
        note = label("observed gaps vs baseline", 20).move_to(ax.c2p(24, 0.045))
        self.play(FadeIn(note), run_time=0.8)
        self.wait(3.6)
