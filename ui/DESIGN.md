# repolace web UI: implementation spec

Source: six Claude Design prototype pages (task list, new task, task detail finished, task detail failed, component sheet, token sheet). They are visual references only. This file is the contract for the build: server-rendered Jinja2 plus HTMX plus plain CSS, same origin as the FastAPI app, no node build, no CORS (Phase 2 plan, section 5). Where the prototype disagrees with `CLAUDE.md` or the schema, section 4 says so and the prototype loses.

Prototype facts, read from the exported markup (not guessed): fonts are IBM Plex Sans and IBM Plex Mono, themes are `.theme-dark` / `.theme-light` classes on the page root, controls are 32 px, content max width 1376 px, base text 13/20. No page shows the running-task detail; the running states are only on the component sheet and in list rows.

---

## 1. Design tokens

### 1.1 Colour (CSS custom properties, exact prototype values)

Implement on `:root` (light), `:root[data-theme="dark"]`, and `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }`. The prototype only has two classes and no toggle; the system default and toggle are an addition (section 5).

| token | dark | light | use |
|---|---|---|---|
| `--bg` | `#0e1116` | `#f5f6f8` | page ground |
| `--surface` | `#151a21` | `#ffffff` | panels, table, cards |
| `--surface-2` | `#1b2129` | `#f3f5f8` | table head, file headers, hover, secondary button |
| `--surface-3` | `#232a34` | `#e8ecf1` | branch pill, tags, bar track, pressed segment |
| `--border` | `#2a323d` | `#d8dde4` | dividers, panel borders |
| `--border-strong` | `#3b4552` | `#b8c0cb` | inputs, buttons, pending node |
| `--fg` | `#e6e9ef` | `#181d25` | primary text |
| `--fg-muted` | `#a6afbc` | `#47505d` | labels, secondary text |
| `--fg-subtle` | `#8d97a5` | `#5a6370` | meta, issue numbers, unscored |
| `--accent` | `#7aa7ff` | `#1d5bd0` | links, estimate band, step done |
| `--accent-strong` | `#9dbdff` | `#1549ad` | link hover |
| `--btn-bg` | `#3b6fe0` | `#1d5bd0` | primary button (text `--btn-fg: #ffffff`) |
| `--focus` | `#9dbdff` | `#1d5bd0` | focus ring |
| `--ok` / `--ok-bg` | `#56d17a` / `#16301f` | `#16723a` / `#dcf5e3` | pr_opened, passed, stage passed |
| `--run` / `--run-bg` | `#7aa7ff` / `#172540` | `#1d5bd0` / `#e2ecff` | running |
| `--warn` / `--warn-bg` | `#e8bc4e` / `#33290f` | `#7f5600` / `#fdf1c8` | passed_with_test_edit, low history |
| `--orange` / `--orange-bg` | `#f39a5a` / `#3a2414` | `#a4430a` / `#ffe9d9` | conflicting |
| `--danger` / `--danger-bg` | `#ff8a80` / `#3c1a1b` | `#b8212c` / `#ffe7e6` | failed (status, outcome, stage, error block) |
| `--danger-line` | `#7a3532` | `#f1a7a3` | error block border (failed page only) |
| `--info` / `--info-bg` | `#b9a8ff` / `#2a2245` | `#5a43c9` / `#ece8ff` | completed |
| `--neutral` / `--neutral-bg` | `#a6afbc` / `#222932` | `#47505d` / `#eceff3` | queued |
| `--row-live` | `#131c2b` | `#f2f6ff` | running row tint (list only) |
| `--sel-bg` | `#172540` | `#eef4ff` | picked issue row (new task only) |
| `--log-bg` | `#0b0d11` | `#fbf3f2` | error message block (failed page only) |
| `--hatch` | `#1f252e` | `#eef0f3` | skipped-stage stripes |
| `--add-bg` / `--add-fg` / `--add-gutter` | `#14301d` / `#d3f8d9` / `#1c4027` | `#e4f8ea` / `#0f3a1d` / `#c9efd4` | diff added |
| `--del-bg` / `--del-fg` / `--del-gutter` | `#3a1a1c` / `#ffdcd8` / `#4d2125` | `#ffecea` / `#5a1016` / `#ffd3cf` | diff removed |
| `--hunk-bg` / `--hunk-fg` | `#172133` / `#9dbdff` | `#e6efff` / `#1549ad` | hunk header |
| `--syn-kw` | `#ff9fbe` | `#b8212c` | syntax keyword |
| `--syn-str` | `#a5d6ff` | `#0a3a73` | syntax string |
| `--syn-fn` | `#d2b6ff` | `#6b3fc4` | syntax function/builtin |
| `--syn-num` | `#7fc8ff` | `#0b4fa8` | syntax number |
| `--syn-com` | `#9aa4b2` | `#5a6370` | syntax comment (italic) |

The token sheet documents WCAG AA pairs (dark/light ratio): ok 7.3/5.2, run 6.4/5.1, info 7.2/5.7, orange 6.6/5.3, danger 6.8/5.4, neutral 6.6/7.1, warn 8.0/5.7, diff add 12.4/11.5, diff del 12.3/12.1. These were not re-measured here. Dark values of `--run` and `--accent` are identical, as are `--neutral` and `--fg-muted`.

### 1.2 Type

Families: `'IBM Plex Sans', ui-sans-serif, system-ui, sans-serif` for UI; `'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace` for every machine value (status and outcome words, branches, costs, SHAs, ids, diffs, durations). Confirmed against the exported CSS; no other family appears.

| token | spec | use |
|---|---|---|
| `--t-title` | Sans 20/28, 600 | page `h1` |
| `--t-heading` | Sans 16/24, 600 | error block title |
| `--t-section` | Sans 14/20, 600 | panel `h2`, step heads, brand |
| `--t-body` | Sans 13/20, 400/500 | default |
| `--t-meta` | Sans 12/16, 400 | hints, notes, th |
| `--t-figure` | Mono 22/28, 600 | estimate range |
| `--t-code` | Mono 12/20, 400/500 | `.mono`, diffs; 11 px for tags, `.sofar`, scale |

Self-host (no CDN; the prototype preconnects to Google Fonts and must not ship that). Six files, `font-display: swap`, normal style only: **IBM Plex Sans 400, 500, 600** and **IBM Plex Mono 400, 500, 600**, `latin` subset woff2 (about 6 files). Add `latin-ext` only if non-Latin issue titles matter; otherwise the system fallback covers them. Check that the glyphs the templates use (`·`, `—`, `−` U+2212, `–`, `→`) are in the subset you ship; U+2192 is not in Google's `latin` range, so either ship `latin-ext` or use the inline SVG arrow. Ship the SIL OFL licence text beside the fonts. Serve from `/static/fonts/` with `Cache-Control: immutable`, and `<link rel="preload" as="font" type="font/woff2" crossorigin>` for Sans 400/500 and Mono 500 only.

### 1.3 Space, radius, border, shadow, motion

- Space scale (px): 4, 8, 12, 16, 20, 32 -> `--space-1 .. --space-6` (4,8,12,16,20,32). Gaps of 6, 10, 14 appear ad hoc; round to the scale or keep as literals inside components.
- Radius: `--radius-chip: 4px` (chip, tag, branch pill), `--radius-ctl: 6px` (button, stage node, input, segment group), `--radius-panel: 8px` (panel, table, estimate), `--radius-pill: 999px` (badge, label, flag).
- Borders: 1 px `--border` on panels and tables, 1 px `--border-strong` on controls. Chips use `1px solid currentColor`; dashed for test-edit and unscored. Skipped stage: dashed.
- Shadows: **none**. The only elevation cue is the skipped-stage tooltip (no shadow, inverted colours: `background: var(--fg); color: var(--bg)`). Selected segment: `box-shadow: inset 0 -2px 0 var(--accent)`. Running dot: `0 0 0 3px var(--run-bg)`.
- Layout: `.wrap { max-width: 1376px; margin: 0 auto; padding: 0 32px }`, 16 px under 900 px. New-task grid `minmax(0,1.35fr) minmax(360px,1fr)` stacks under 1000 px. Detail layout is flex-wrap: main `flex: 999 1 640px`, sidebar `flex: 1 1 300px`.
- Controls 32 px tall (select and search 34 px); badge and chip 22 px; label pill 18 px; tag 18 px; flag 20 px.
- Focus: `:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 4px }` globally.
- Motion: only the running spinner (1.4 s linear rotate) and a 0.12 s chevron rotation and tooltip fade. `prefers-reduced-motion` disables the spinner.
- Icons: 16x16 inline SVG, `stroke: currentColor; stroke-width: 1.6; fill: none; round caps`, rendered at 14 px (18 px for brand). Ship a Jinja macro file `_icons.html`; icon paths are in the prototype markup (section 2 names each).

---

## 2. Component inventory

Naming: BEM-lite, prefix-free, matching the prototype so templates can be diffed against it. Every status or outcome is **icon + exact value text**; colour only reinforces. All status/outcome values are rendered from the enum value, never retyped, and a route test asserts the rendered vocabulary equals `TaskStatus` / `TaskOutcome` (plan section 5).

### 2.1 Status badge (`macros/badge.html`)

```html
<span class="badge b-{{ key }}"><svg class="i" viewBox="0 0 16 16" aria-hidden="true">...</svg>{{ status.value }}</span>
```

Pill, 22 px, mono 12/500, tinted fill. Class map: `queued -> b-queued`, `running -> b-running`, `pr_opened -> b-pr`, `completed -> b-completed`, `conflicting -> b-conflicting`, `failed -> b-failed`. Icons: queued dashed circle (`circle r=5.5 stroke-dasharray="2.4 2.2"`); running spinner (`<g class="spin">` faint full circle + quarter arc); pr_opened git-pull-request glyph; completed circle with inner square (stop); conflicting triangle with bang; failed circle with x. Only running animates.

### 2.2 Outcome chip (`macros/chip.html`)

Square-ish (4 px radius), outlined, 22 px, mono 12, deliberately a different shape from the pill so status and outcome never read as one thing.

| value | class | look | icon |
|---|---|---|---|
| `passed` | `chip c-passed` | `--ok` outline | check |
| `failed` | `chip c-failed` | `--danger` outline | x |
| `passed_with_test_edit` | `chip c-edit` | `--warn`, dashed, `--warn-bg` fill | pencil |
| `NULL` rendered `unscored` | `chip c-unscored` | `--fg-subtle`, dashed | dash |

`passed_with_test_edit` carries an approval segment: `<span class="sub" title="Approved by {{ approver }}">check approved</span>` or `<span class="sub pending">clock not approved</span>` (`.sub`: left dashed divider, `margin-left 3px; padding-left 7px`; `.pending` text is `--fg`). Approval state comes from `test_edit_approved_by` / `test_edit_approved_at` (null = not approved). `unscored` is a UI label for `outcome IS NULL`, not an enum member (section 4).

### 2.3 Stage strip (`macros/stage_strip.html`)

```html
<section aria-labelledby="pl"><h2 id="pl" class="sr">Pipeline</h2>
<ol class="strip">
  <li class="stage st-passed"><span class="nm">{icon}Retrieve</span><span class="sm"><b>passed</b> · 4.2 s</span></li>
  ... Plan, Edit, Test, Review ...
  <li class="tailgrp" aria-label="Delivery"><ol class="strip" style="flex-wrap:nowrap">Push, PR</ol></li>
</ol></section>
```

Node: `flex: 1 1 150px; min-width: 132px`, 6 px radius, 1 px border, 3 px top rule in `--sc`, icon + name (600), then mono `<b>state</b> · duration`. Delivery group (Push, PR) sits after a dashed vertical divider, nodes 112 px. State classes:

| state | class | rule colour | icon | extras |
|---|---|---|---|---|
| pending | `st-pending` | `--border-strong` | dashed circle | text "pending", "not reached" when a prior stage failed |
| running | `st-running` | `--run` | spinner | `running · 0:41` live elapsed |
| passed | `st-passed` | `--ok` | circle-check | duration |
| failed | `st-failed` | `--danger` | circle-x | `--danger-bg` fill, `--danger` border, duration |
| skipped | `st-skipped` | transparent | circle-slash | hatched fill, dashed, tooltip |

Skipped node is a `<button type="button" aria-describedby="tip-plan">` containing `<span class="tip" role="tooltip" id="tip-plan">not used yet</span>`; the tooltip shows on `:hover` and `:focus-visible`. Durations: under 10 s one decimal (`4.2 s`), under 1 min `45 s`, else `1 m 48 s`; running shows `m:ss`. The word for the ok state is "passed" in the prototype; see section 4 item 9 (recommend `ok`).

### 2.4 Table row (`macros/task_row.html`)

`<tr id="task-{{ task.id }}" class="{{ 'is-live' if live }}" hx-get=".../row" hx-trigger="every 3s" hx-swap="outerHTML">` (the hx attributes exist only on queued/running rows, so a row that finishes stops polling when the server returns it without them). Columns, in order: Issue (`#n` mono subtle + title link, width 34%), Target branch (`.branch` pill with branch glyph), Status (`.stcell`: badge + optional `.note`), Outcome (chip), Cost USD (right aligned mono, `.sofar` "so far" under it when running), Started (`<time datetime title>` muted, relative), PR (`#503` link with PR glyph, or `—` with `aria-label="No PR"`). Table: `min-width: 1080px` inside `.tablewrap { overflow-x:auto; border; radius 8 }`, `<caption class="sr">`, `th scope="col"`, hover `--surface-2`, live row `--row-live`.

`.note` under the badge (12/16, max 280 px): `<span class="k">key</span>value`. Keys: `stage` (running: current stage, `Test · attempt 1`), `conflict` (conflicting: file list), `stop` (completed: `agent_stop_reason`), `error` (failed: `Stage: message`, class `.note.err`, `--danger`).

### 2.5 Estimate panel (`macros/estimate.html`)

```html
<div class="est" role="group" aria-labelledby="estlab">
  <div class="lab"><span id="estlab">Estimated cost (p50 – p90)</span> {low-history flag}</div>
  <div class="range">$0.74 – $1.46<small>p50 – p90</small></div>
  <div><div class="bar" aria-hidden="true"><div class="band" style="left:37%;width:36%"></div><div class="p50" style="left:calc(37% - 1px)"></div></div>
       <div class="scale" aria-hidden="true"><span>$0</span><span>cap $2.00</span></div></div>
  <div class="basis">Median of the last 12 tasks on this repo.</div>
</div>
```

`.range` mono 22/28 600. Bar is 8 px, runs $0 to the $2.00 cap; band = p50..p90 (`--accent` at .55 opacity), tick = p50 (2 px x 14 px, `--fg`). Band left = `p50/cap*100%`, width = `(p90-p50)/cap*100%`, both clamped to 0..100. The bar and scale are `aria-hidden`, so the range text and basis line carry all the information. States:
- **Normal** (12 tasks): range, band, basis "Median of the last N tasks on this repo."
- **Low history** (3 tasks): a `.flag` ("triangle low history", `--warn` on `--warn-bg`, pill, 20 px) next to the label; p90 clipped to the cap (`$0.90 – $2.00`); basis "Median of the last 3 tasks on this repo. Too few to be reliable; p90 is clipped to the $2.00 cap."
- **Empty**: `.none` "No estimate yet" (16/24 600 muted), empty bar, basis "No history yet, capped at $2.00." The page may still be submitted; the hard stop is $2.00 (`budget_usd`).

### 2.6 Diff block (`macros/diff.html`)

Whole-diff `<details class="diff" open>` with summary: chevron, `<h2>Diff</h2>`, optional `.tag` ("not pushed" on a task with no PR), "N files changed", `.stat` (`+7` in `--ok`, `−4` in `--danger`, U+2212). Per file `<details class="file" open>` with a sticky summary (`--surface-2`): chevron, path (mono 600), `.tag` modified/added/deleted/renamed, `.stat` right aligned. Body: `<div class="code" role="region" aria-label="Diff of {path}" tabindex="0">` with rows `<div class="dl add|del|hunk|ctx">` as a 3-column grid `44px 44px 1fr` (old gutter, new gutter, text), `min-width: 620px`, `overflow-x: auto`. Text cell is `white-space: pre`; the sign is real text in `<span class="sg">+</span>` (`-`, space). Gutter cells get `--add-gutter` / `--del-gutter`. Syntax spans `k f s nu cm` use the `--syn-*` tokens. Chevron is a native `<details>` marker replaced by an SVG rotating 90 degrees when open.

Build note: `tasks.patch_diff` is raw unified diff text, so the server must parse it (hunk headers, line numbers) and escape every line (the diff is attacker-influenced). Syntax highlighting needs a new dependency (Pygments) or must be dropped; recommended: ship without highlighting first (`k f s nu cm` classes unused), add later. Cap rendering (suggest 2,000 lines or 300 KB) with a "diff truncated, N lines" note.

### 2.7 Test-runs table (`macros/test_runs.html`)

`<section class="panel"><header><h2>Test runs</h2><span class="muted">attempt 0 is the baseline, before any edit</span></header>`; table `.runs` (`min-width 620px` in an `overflow-x:auto` wrapper): Attempt (mono number, `.tag` "baseline" for 0), Passed, Failed, Errors, Duration (right-aligned mono), Result. Number cells: `.zero` (`--fg-subtle`) when 0, `.nz-bad` (`--danger`, 600) when a Failed/Errors count is nonzero. Result cell: `.res.ok` "check all passing" or `.res.bad` "x N failing" followed by the first failing test name (`mono subtle`); an aborted run shows "triangle run aborted" and its `error` text, with Passed/Failed rendered `—` and Errors 1.

### 2.8 Buttons, inputs, filters

- `.btn`: 32 px, 6 px radius, 1 px `--border-strong`, `--surface-2` fill, 500 weight, hover `--surface-3`. `.btn-primary`: `--btn-bg`, white text, hover `filter: brightness(1.08)`. `.btn-ghost`: transparent, accent text. `.btn[disabled]`: opacity .5, `cursor: not-allowed`. Link-styled buttons use `<a class="btn">`.
- Segmented filter `.segs[role=group][aria-label="Filter by status"]`: seven buttons (`all` plus the six statuses), each `class="seg" aria-pressed` with a `.count`. Pressed: `--surface-3`, 600, inset 2 px accent underline. In the server-rendered build make each segment an `<a class="seg" href="?status=x&outcome=y" aria-current="true">` or a `hx-get` button; keep `aria-pressed` only if they stay buttons.
- Outcome filter: `<label class="sel"><span>Outcome</span><select>` with `all outcomes`, `passed`, `passed_with_test_edit`, `failed`, `unscored`. `unscored` maps to `outcome IS NULL` server-side.
- Live indicator `.live[role=status]`: dot plus "N running · rows refresh every 3 s".
- Search `.search` (34 px, focus-within outline), issue radio list `fieldset.issues` of `label.opt` (grid `18px 1fr`, picked state `--sel-bg` plus inset 1 px accent), `.lbl` GitHub label pills, `select` for branch, `.field` (label, control, `.hint`).
- Breadcrumbs `nav[aria-label=Breadcrumb] ol.crumbs`, last item `aria-current="page"`; `.branch` pill; `.tag`; `.back` link with arrow icon; `.costbar` (4 px track, `--accent` fill, width = cost/cap).

### 2.9 Empty states and error blocks

- Empty list: one `<td colspan="7" class="empty">` "No tasks match `status=...` and `outcome=...`." plus a "Clear filters" button. A repo with no tasks at all needs its own copy (not in the prototype): "No tasks yet" and a primary "New task" button.
- No issue matches: `.empty` "No open issues match `"q"`."; step 3 with no pick: "Pick an issue in step 1 to see where the PR will go and what it should cost."
- Error block (failed task, above the pipeline): `section.err[aria-labelledby]` with `--danger-bg` fill and `--danger-line` border: head row (icon, `h2` "Failed in {Stage}: {short message}", right-aligned `.when` "attempt 1 · 05:43:52"), a plain paragraph explaining that repolace broke, not a verdict on the issue, and the outcome stays `unscored`, then `<pre class="msg">` (mono 12/20, `--log-bg`, `white-space: pre-wrap`, bold exception name in `--danger`).

---

## 3. Pages

Shared shell (`base.html`): `<header class="topbar">` (brand mark + "repolace", breadcrumbs, spacer, primary action), `<main class="wrap">`. One `<html data-theme>`; the theme class lives on the root so every fragment inherits it with no client state. All times: `<time datetime="ISO" title="ISO">` with a relative label; the title attribute is the absolute time.

Route scheme (UI router, HTML; JSON API from plan section 4 stays UUID-keyed): the prototype uses `acme/ledger` slugs, so UI pages resolve `owner/name` to the repo and use the task UUID for tasks.

| page | route |
|---|---|
| task list | `GET /ui/{owner}/{name}/tasks?status=&outcome=&before=` |
| new task | `GET /ui/{owner}/{name}/tasks/new`, `POST /ui/{owner}/{name}/tasks` (303 to the detail page) |
| task detail | `GET /ui/tasks/{task_id}` |

### 3.1 Task list

Regions: topbar (breadcrumb `owner / name / Tasks`, primary "New task"); `.repohead` ("Tasks", `owner/name` mono, "newest first"); toolbar (status segments with counts, outcome select, live indicator); table; load-more bar.

Data per row: issue number and title (link to detail), target branch, status badge, optional note (stage, conflict files, stop reason or error), outcome chip, cost (with "so far" while running), started, PR link.

Interactions: status segment and outcome select refresh the table (full-page GET with query string; add `hx-boost` and `hx-push-url` for partial swaps); "Load more" is `hx-get="...?before={cursor}" hx-target="closest .loadmore" hx-swap="outerHTML"` appending rows (keyset on `created_at`); row title links to detail; PR links to `pr_url`, `target="_blank" rel="noopener noreferrer"`.

Polling:

| fragment | endpoint | trigger | notes |
|---|---|---|---|
| one live row | `GET /ui/tasks/{id}/row` | `every 3s` on queued/running rows only | `outerHTML` swap; terminal render omits `hx-trigger`, and sends `HX-Trigger: tasks-changed` |
| toolbar counts + running count | `GET /ui/{owner}/{name}/tasks/summary` | `every 10s, tasks-changed from:body` | counts per status, running count |

A row that stops being live must stop polling by being re-rendered without `hx-trigger`; a 286 response is the alternative. Rows that newly become queued after the page loaded do not appear until the next full load (documented limitation; add a `hx-trigger="every 10s"` list refresh only if needed).

### 3.2 New task

Single `<form method="post" class="flow">`, two columns.

Left column, step 1 "Pick an open issue" (`stepnum` goes `done` when an issue is picked; right side "N open"): search input, then `fieldset#issue-list` of radios, each row `#number title`, GitHub label pills, "opened 2 h ago by @mira". Right column: step 2 "Target branch" (a `select` of branches, default branch labelled "(default)" and preselected, hint text), step 3 "Confirm" (`aria-live="polite"` region): summary `dl` (issue), `.target` strip "The pull request will target [branch]", the estimate panel (2.5), actions row ("Start task" primary disabled until an issue is picked, "Cancel", right-aligned hint "Hard stop at $2.00 (`budget_usd`)").

Form fields posted: `issue_number`, `target_branch` (the prototype names them `issue` and `branch`; use the API's names). `open_pr_on_failure` exists on `CreateTaskRequest` and is not in the prototype; leave it out of the form (default false).

| fragment | endpoint | trigger |
|---|---|---|
| issue list | `GET /ui/{owner}/{name}/issues?q=` | `input changed delay:250ms` on the search box, target `#issue-list` |
| confirm panel | `GET /ui/{owner}/{name}/tasks/new/confirm?issue_number=&target_branch=` | `change` on the radios and branch select, target `#confirm` |

The estimate is repo-level (it does not depend on the issue), so the confirm fragment can be a cached render. The server must filter issues locally from a short-lived (suggest 60 s) cached GitHub list; one GitHub call per keystroke is not acceptable. Branch list from `GET /repos/{id}/branches`; invalid branch returns 422 (plan section 4), shown as an inline `.hint` error under the select.

### 3.3 Task detail, finished (`pr_opened`, `completed`, `conflicting`)

Order: `.head` (back link "All tasks"; `h1` with mono `#issue_number` and issue title; `.metaline`: status badge, outcome chip, "targets [branch]", "started ...", "ran 4 m 12 s", PR link), stage strip, then `.layout` with main column (Result panel, Diff panel, Test runs panel) and sidebar (`dl.panel.facts`).

Result panel `dl.result`: Stop reason (mono value plus one-sentence gloss, section 4), Outcome (chip plus `score_reason`, see item 11), Pull request (`owner/name#503` link, arrow, branch pill, "no merge conflicts" or the conflict note). For `conflicting`, add the conflicting files from `conflict_reason`. For `passed_with_test_edit`, show `test_edit_justification` and the approval line (the prototype has no panel for this; section 4).

Sidebar facts, in order: Status, Outcome, Stop reason, Issue (link to `issue_url`), Target branch, Pull request, Cost ("$1.12 of $2.00 cap" with `.costbar`), Started (absolute mono), Duration, Task (short id).

No polling once terminal: the page is rendered once, with no `hx-trigger`.

### 3.4 Task detail, running (not designed in the prototype; derived from the component sheet)

Same page, status `running` or `queued`. Pipeline strip shows `st-running` with live elapsed time, later nodes `st-pending`, earlier `st-passed`; Result panel replaced by a single line "Running: {stage}" (no stop reason, outcome `unscored`, no PR); Diff panel hidden until `patch_diff` exists; Test runs fills as attempts land; sidebar cost shows "so far".

| fragment | endpoint | trigger |
|---|---|---|
| pipeline strip | `GET /ui/tasks/{id}/pipeline` | `every 3s` |
| facts sidebar (status, cost so far, duration) | `GET /ui/tasks/{id}/facts` | `every 3s` |
| test runs | `GET /ui/tasks/{id}/test-runs` | `every 3s`, `outerHTML` |
| head metaline | `GET /ui/tasks/{id}/head` | `every 3s` |

Each fragment returns HTTP 286 once the task is terminal (HTMX stops polling after applying it); the final render includes the Result and Diff panels. Elapsed time ticks client-side from `datetime` on a `<time>` element only if wanted; otherwise it updates per poll. A running task whose `heartbeat_at` is stale (more than 90 s, tune to the 30 s beat) should show a "no heartbeat" flag: not in the prototype, see section 4.

### 3.5 Task detail, failed (`failed`)

Same shell, with the error block (2.9) between `.head` and the strip. Differences from 3.3: metaline shows `no PR`, outcome chip `unscored`; sidebar gains a "Failed stage" row (mono 600 `--danger`) before Outcome; Result panel states stop reason `—` ("The agent never stopped on its own; the pipeline failed first."), Outcome `unscored` ("Not scored because no attempt finished."), Pull request "None. Nothing was pushed."; Diff panel closed by default with a "not pushed" tag; strip shows the failing node `st-failed` and later nodes `st-pending` ("not reached"); test-runs table shows the aborted attempt (`—` counts, Errors 1, "run aborted").

---

## 4. Data contract

Existing columns are on `shared/repolace_shared/db/models.py` (`main`). `task_stages`, `tasks.heartbeat_at`, `tasks.claimed_by` are from the unmerged `feat/task-stages-heartbeat` (migration 0014); `tasks.conflict_reason` is planned (0015).

### 4.1 Field to source

| shown | where | source |
|---|---|---|
| issue number | list, detail h1, sidebar | `tasks.issue_number` |
| issue title | list, detail h1 | `tasks.issue_title` |
| issue link | sidebar | `tasks.issue_url`; label `owner/name#n` from `registered_repos.full_name` |
| target branch | list, detail | `tasks.target_branch` |
| status badge | everywhere | `tasks.status` (enum, 6 values) |
| outcome chip | everywhere | `tasks.outcome` (`passed`, `passed_with_test_edit`, `failed`), NULL shown as `unscored` |
| test-edit approval | chip, result | `tasks.test_edit_approved_by`, `test_edit_approved_at` (null = pending); justification `tasks.test_edit_justification` |
| stop reason | list note, result, sidebar | `tasks.agent_stop_reason` (8 values) |
| cost | list, sidebar | `tasks.cost_usd` when terminal; while running `SUM(llm_calls.cost_usd)` for the task |
| "of $2.00 cap" | sidebar, hint | constant `DEFAULT_MAX_USD` (gateway), not stored per task |
| cost bar width | sidebar | cost / cap |
| started | list, detail | `tasks.started_at` (NULL = "not started"); `created_at` for ordering |
| duration "ran 4 m 12 s" | detail | `completed_at - started_at`; running: now - `started_at` |
| PR number, link | list, detail | `tasks.pr_number`, `tasks.pr_url` |
| diff | detail | `tasks.patch_diff` (parse unified diff); file list also `tasks.changed_files` |
| diff stats +/- | detail | counted from `patch_diff` |
| file status tag (modified/added/...) | detail | parsed from the diff headers |
| test-runs rows | detail | `task_test_runs`: attempt, `passed`/`failed`/`xfailed`/`skipped`/`did_not_run`/`collect_failures` (use array lengths), `duration_seconds`, `error`, `exit_code` |
| Passed / Failed counts | test runs | `array_length(passed)`, `array_length(failed)` |
| Errors | test runs | `array_length(collect_failures)`, plus 1 when `error` is set |
| failing test name | test runs | first element of `failed` |
| result score sentence | result panel | `tasks.score_reason` |
| error text | error block, row note | `tasks.error_message` (status `failed` only) |
| failed stage name | error block, sidebar | the `task_stages` row with `status='failed'`, mapped to a UI stage |
| failed stage message, time | error block | `task_stages.error` (redacted, capped at 1,000 chars), `ended_at` |
| stage status and duration | strip | `task_stages.status` (`running`, `ok`, `failed`), `ended_at - started_at` |
| current stage (list note) | running rows | latest `task_stages` row with `status='running'` |
| attempt number in note ("Test, attempt 1") | running rows | `MAX(task_test_runs.attempt)` plus one for the next run |
| conflict files | list note, result | `tasks.conflict_reason` (planned 0015) |
| branch list, default | new task | `GET /repos/{id}/branches` (planned), `registered_repos.default_branch` |
| open issues | new task | `GET /repos/{id}/issues` today returns only `number`, `title`, `html_url` |
| estimate p50/p90, basis, n | new task | planned `GET /repos/{id}/tasks/estimate` |
| list total ("of 148"), status counts | list | `COUNT(*)` and `GROUP BY status` over the repo's non-benchmark tasks (new) |

### 4.2 Stage mapping (UI node to `task_stages.stage`)

The strip has five nodes plus two delivery nodes; the pipeline records sixteen real stage names. Mapping per plan section 5:

| UI node | `task_stages.stage` rows | duration |
|---|---|---|
| Retrieve | `clone`, `index`, `retrieve` | sum of the three |
| Plan | none | always `skipped`, tooltip "not used yet" (until Phase 2 roles) |
| Edit | `agent` | `agent` duration minus the Test time that ran inside it (see gap below) |
| Test | `verify_baseline`, `record_baseline`, plus scored runs | `verify_baseline` plus `SUM(task_test_runs.duration_seconds)` for attempts >= 1 |
| Review | `review` (and the gate) | `review` |
| Push | `squash`, `push` | sum |
| PR | `pr` | `pr` |

`instance`, `preflight`, `embedder`, `baseline_files`, `branch`, `finalize` have no node: fold them into the neighbouring node or ignore. Node state: failed if any of its rows failed; running if any running; passed if all present rows `ok`; pending otherwise ("not reached" when an earlier node failed).

### 4.3 Fields shown that have NO source today

Add to the API or drop from the UI:

1. **Short task id `t_9c41e2`**: `tasks.id` is a UUID. Derive the first 8 hex characters of the UUID and drop the invented `t_` prefix, or drop the row.
2. **Issue labels, author, "opened 2 h ago"** in the issue picker: `GET /repos/{id}/issues` returns only number, title, url. Extend `GithubClient.list_repo_open_issues` and the response (GitHub returns labels, `user.login`, `created_at`) or drop the meta line.
3. **Container id, memory limit, peak memory, worker name** in the failed error block: `tasks.claimed_by` (worker id) is planned; container id, limit and peak have no column. `task_stages.error` is free text. Show only what the stage error text contains.
4. **Exception class and structured fields** ("SandboxError: ...", "stage:", "command:") in the error block: only whatever text is in `task_stages.error` / `tasks.error_message`. Render as a plain preformatted block; do not parse.
5. **Per-stage Edit vs Test duration**: scored suites run inside the `agent` stage (`verify_attempt` is not a `_stage`), so the strip cannot show separate Edit and Test durations from `task_stages` alone. Derive Test from `task_test_runs.duration_seconds` as above; otherwise add `agent` sub-spans.
6. **A "Test" failure inside the agent stage**: a sandbox failure during a scored attempt fails stage `agent`, not a stage called Test. The prototype's "Failed in Test" needs either extra stage rows for scored runs or a mapping from `task_test_runs.error` on the last attempt.
7. **"No merge conflicts"** line: only inferable as `status != conflicting` and `pr_url` present.
8. **Total task count and per-status counts** on the list: new aggregate queries.
9. **Estimate**: the endpoint is planned, not built. Note the basis difference in 4.4 item 10.
10. **Live elapsed time for a running stage** (`running · 0:41`): `now - started_at` of the running `task_stages` row; no stored value, computed on render.
11. **"agent declared the fix done on attempt 2"**: the attempt number is derivable (`MAX(attempt)`), the stop-reason gloss needs a lookup table of all 8 values: `submitted`, `step_cap`, `budget_usd`, `budget_calls`, `budget_wall`, `llm_error`, `no_change`, `max_attempts`. The prototype only shows `submitted`, `step_cap`, `no_change`, `budget_usd`.
12. **Approver `@rounak` and the approve action**: `test_edit_approved_by` exists, but there is no auth until Phase 4, and no approve endpoint. The prototype shows approval state only.
13. **Staleness / "no heartbeat"**: needs `tasks.heartbeat_at` (migration 0014, unmerged). Not in the prototype.

### 4.4 Contradictions with CLAUDE.md and the schema

1. **`completed` plus `unscored` for agent-caused stops.** List rows #468 (`budget_usd`) and #466 (`no_change`) show `completed` with outcome `unscored`. `CLAUDE.md` says agent-caused stops are `completed` with the scorer's outcome set, and "a task with no scored attempt scores `failed`, not inadmissible". `completed` plus NULL outcome is only the inadmissible case (unusable baseline, curated test not red, product task with no pytest tests), where the agent never ran and `agent_stop_reason` is NULL. Fix the sample data and the gloss: those two rows should read `failed`. A `completed` row with NULL outcome needs its own note (for example "inadmissible: baseline not usable"), with no stop reason.
2. **`unscored` is not an outcome value.** `TaskOutcome` has three members; `unscored` is the UI label for NULL (queued, running, `failed` status, inadmissible). Keep it as a rendered label and never write or filter it as a value except via `outcome IS NULL`. Status and outcome stay in separate columns and separate shapes (pill versus square chip), as the prototype does correctly.
3. **`pr_opened` plus `failed` (#474)** is consistent with the model only when `open_pr_on_failure` was true (the gate otherwise refuses a PR). The row should say so (a note "PR opened despite failing score"), and the new-task form has no way to set it.
4. **`pr_opened` plus `passed_with_test_edit` plus approval pending (#477)**: consistent, but CLAUDE.md requires human sign-off before this counts; the UI must never present it as a pass and must not render the passed-green colour for it (the prototype uses amber dashed, correct).
5. **`conflicting` plus `passed` (#471)**: allowed (outcome is from the pre-merge scoring, `conflicting` set when the resolver gives up and a PR is opened anyway). Show the PR link and conflict files. `tasks.conflict_reason` is not yet in the schema.
6. **"Task #479" is the issue number**, not a task identifier: `tasks` has a UUID `id` and `issue_number`, and several tasks can exist for one issue. The prototype uses `#479` as the row id (`id="task-479"`), as the URL segment (`/tasks/479/row`) and as the page title. Use the task UUID for ids, URLs and fragments, and show `#issue_number` as the issue.
7. **Repo addressing**: prototype uses `/repos/acme/ledger/...`; the API addresses repos by UUID (`/repos/{repo_id}/...`). Resolve the slug to `registered_repos.full_name` in the UI router.
8. **The stage strip vocabulary overlaps the outcome vocabulary**: nodes say `passed`/`failed`, while `task_stages.status` is `ok`/`failed` and `outcome=passed` means the issue was fixed. A stage reading "Review passed" next to an outcome of `failed` is easy to misread. Recommend rendering stage state as `ok` (matching the table) and reserving `passed` for outcomes. Needs a decision from the user.
9. **Benchmark tasks are not excluded.** Plan section 4 excludes `eval_run_id IS NOT NULL` tasks from the estimate; the prototype list does not say. List and counts should hide benchmark tasks by default (they belong to the report). The detail page for one must not offer actions, and it shows test ids that are the answer key, which is operator-side only and fine as long as no agent-visible path renders it.
10. **Estimate basis wording.** Prototype: "Median of the last 3 tasks on this repo" with a low-history flag. Plan: below 5 repo tasks, fall back to all tasks on the same model, then `basis: "none"`. The template must render the basis from the endpoint's `basis` and `n` (`repo`, `model`, `none`), flag low history when `basis != "repo"` or `n < 5`, and word the line accordingly ("Median of the last 8 tasks on this model, across repos.").
11. **`score_reason` is operator text**, sometimes naming up to three test ids (CLAUDE.md, "Task bookkeeping"). It may be shown in the Result panel next to the outcome, escaped, and must never be fed back to an agent. The prototype's sentences ("The failing baseline test now passes; no test files were edited.") are hand-written glosses; render `score_reason` verbatim instead, or generate glosses per outcome class only.
12. **`patch_diff` on failed tasks.** The failed page shows a diff ("not pushed"). `patch_diff` is NULL for a task that never reached the agent and is only set by the pipeline once the agent has run and the review stage produced it; whether a `failed` task keeps it depends on where it died. Treat the diff as optional and hide the panel when NULL (and show "No changes" for `''`).
13. **Cost is stored at terminal write** (`write_terminal`); the list's "so far" figure for running rows must come from `llm_calls`, which is a per-row subquery. Batch it for the visible running rows.
14. **No auth** (Phase 4): the prototype shows approver names and a logged-in feel; there is no user concept. Do not add login chrome.

---

## 5. Accessibility

Found in the prototype (keep):
- Status and outcome are never colour-only: every badge and chip pairs an icon with the exact value text; diff lines carry a visible `+` / `-` sign; stage nodes carry icon plus state word plus duration; counts `0` are dimmed, not hidden.
- Global `:focus-visible` ring (2 px, offset 2 px) in a dedicated `--focus` token; the search wrapper uses `:focus-within`.
- Semantics: one `h1` per page, `h2` per panel (`aria-labelledby`), `<nav aria-label="Breadcrumb">` with `aria-current="page"`, table `<caption class="sr">` and `th scope="col"`, `<fieldset><legend class="sr">` for the issue radios, labelled search input, `role="status"` on the live count, `aria-live="polite"` on the confirm panel, `role="group"` on the estimate, decorative SVG `aria-hidden`, diff regions are `role="region"` with `aria-label` and `tabindex="0"` so wide code scrolls from the keyboard, native `<details>` for collapse.
- Documented contrast: all text pairs at or above 4.5:1 in both themes (token sheet); `prefers-reduced-motion` stops the spinner; `.sr` utility for hidden labels; `<time datetime>` on dates; "No PR" has an `aria-label` on the em dash.

Gaps to fix in the build:
1. **No system theme or toggle.** Prototype has two classes set by a design-tool prop. Default to `prefers-color-scheme`, add a light/dark/system toggle in the topbar (persisted in `localStorage` only as a convenience, with the choice mirrored in a cookie so the server can render the right class and avoid a flash).
2. **Skipped stage is a `<button>` that does nothing**, announced as an interactive control. Use a non-interactive `<li>` with the text "skipped, not used yet" visible or in `aria-describedby`; keep the tooltip as an enhancement.
3. **Tooltip only on hover/focus** and positioned above, may clip at the viewport top; make the "not used yet" text part of the node for touch and screen reader users.
4. **Polling and screen readers.** `role="status"` on the "N running" text will announce on every 3 s refresh if the node is replaced. Update the live region only when the count changes, and never put `aria-live` on polled rows or the strip; announce terminal transitions once.
5. **Sticky `th` inside an `overflow-x: auto` wrapper** does not stick (the scroll container is the wrapper); either drop `position: sticky` or accept it. Wrapper with `overflow-x` and no `tabindex` is not keyboard scrollable on narrow screens; add `tabindex="0"`, `role="region"`, `aria-label`.
6. **Segmented filter** are `aria-pressed` buttons that change page state; as links they need `aria-current="true"` instead, and the count should be inside the accessible name ("failed, 1 task").
7. **Chevron `<details>` summaries** contain an `h2` inside `summary`, which strips the heading role in some screen readers; keep the heading outside the summary.
8. **Colour contrast not measured** for `--fg-subtle` on `--surface-2`/`--surface-3` (tags, hatch, 11 px text), `--syn-com` on diff rows, skipped-stage text on the hatch, `--border-strong` pending top rule (decorative, text carries state), and `.stepnum.done` (`--surface` text on `--accent`). The 11 px mono tags and `.sofar` are below the 12 px floor of the body scale; raise to 12 px or verify.
9. **Touch targets**: controls are 32 px and segments 30 px; WCAG 2.2 AA minimum is 24 px so it passes, but 44 px is advisable for the row links and "Load more" on touch.
10. **Selection and error feedback in the form**: the estimate and confirm update by HTMX swap into an `aria-live="polite"` region; a failed submit (422 invalid branch, 404) needs an inline `role="alert"` message with focus moved to it.
11. **Colour-only elements that remain**: the cost bar (decorative, value is in text), the estimate bar (`aria-hidden`, text carries it), `.stage` top rule (text carries state). All acceptable; do not add new colour-only cues.
12. **Responsive**: the list table has `min-width: 1080px` and scrolls horizontally on phones; consider hiding Branch and Started columns under 700 px. Not designed in the prototype.

---

## 6. Build order

Each step is shippable and testable on its own; later steps reuse the earlier macros.

1. **Foundations** (`ui/static/`, `base.html`): `tokens.css` (section 1), `base.css` (reset, `.rp` root, `.btn`, `.mono`, `.sr`, focus, topbar, breadcrumbs, `.wrap`), self-hosted fonts, `_icons.html`, theme toggle and cookie, static mount and Jinja2 environment in the FastAPI app, HTMX vendored (no CDN). Test: template renders, tokens present, no external URL in any rendered page.
2. **Status badge and outcome chip macros**, with the vocabulary test (rendered values equal the enums, including the NULL-to-`unscored` mapping). These are the highest-risk components for drifting from `CLAUDE.md`.
3. **API surface the pages need** (plan section 4): response models, paginated `GET /repos/{id}/tasks` with `status`, `outcome`, `before`, counts; `GET /tasks/{id}`; the repo slug resolver. Everything below renders from these.
4. **Task list page**: table, row macro, filters, empty states, load more, then the 3 s row fragment and summary fragment.
5. **Task detail, finished**: head, Result and sidebar, test-runs table, then the diff (parser, escaping, size cap). Fixtures for all six statuses.
6. **Stage strip** and the stage mapping (4.2), driven by `task_stages` (needs migration 0014 merged); then the running view with its polling fragments and 286 stop; then the failed view and error block.
7. **New task flow**: branches endpoint, issue search fragment with cached list, estimate endpoint and panel (all three states), confirm fragment, POST with 422 handling.
8. **Hardening**: accessibility gaps (section 5), responsive pass, heartbeat flag once `heartbeat_at` lands, `passed_with_test_edit` approval display, benchmark-task filtering, HTML-injection tests (issue titles, diff lines, error text, `score_reason` all escaped; no `|safe` on any DB value).

Security notes for every step: Jinja2 autoescape on (never `|safe` on database text; the diff renderer builds its own markup from escaped pieces); external links `rel="noopener noreferrer"`; no inline script, so a CSP of `default-src 'self'` works (HTMX uses no inline handlers; the prototype's inline `style="..."` for bar widths needs `style-src 'self' 'unsafe-inline'` or CSS custom properties set through classes); bind to 127.0.0.1 and give the `api` service its own env file (plan section 5, there is no auth until Phase 4).
