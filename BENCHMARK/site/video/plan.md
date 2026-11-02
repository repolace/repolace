# Manim explainers for the benchmark report (Stream C)

Palette = the site's dataviz tokens: stage #1a1a19, ink #f0efec, muted #898781, hairline #2c2f33,
baseline blue #3987e5, e1 orange #d95926, r2 aqua #199e70, m1 magenta #d55181.
Text inside clips is tiny and mono; captions live in the page. All numbers are read from ../data/report.json.
Every clip ends on a held "idea frame" that doubles as the reduced-motion poster.

1. clip1, why 54 runs is really 18 instances (about 19 s)
   Misconception: 54 rows are 54 independent trials. Aha: the unit is the instance, so we resample instances.
   54 loose dots group into 18 boxes of 3 (real outcomes of the baseline), a few whole-instance resamples
   land as points on a rate axis, then the stored bootstrap rates build the histogram and the 44-83% interval
   closes around the 35/54 point.
2. clip2, runs of one instance move together (about 18 s)
   Misconception: three runs of a bug are three coin flips. Aha: they clump at 0 and 3.
   Outlined bars = how many instances would pass 0,1,2,3 times if runs were independent at 35/54;
   filled bars = the real counts. Then 1 + 2 x 0.689 = 2.38 and 54 rows count as about 22.7.
3. clip3, only disagreements count (about 18 s)
   Misconception: compare two pass rates. Aha: pair the instances and look at the discordant ones.
   18 matched dots (run 0), concordant pairs fade, the real e1 split (2:0, p = 0.5) is what is left,
   with the reference p-values for 8:0, 7:1, 6:2.
4. clip4, what 40 points means (about 18 s)
   Misconception: a lower number is a worse agent. Aha: at 18 instances only a gap near 39 points is detectable.
   Power curve from the instance-bootstrap SE (9.9 points), the 80% / 39-point mark, then the observed
   gaps of e1, r2 and m1 sitting far to the left, where power is low.
