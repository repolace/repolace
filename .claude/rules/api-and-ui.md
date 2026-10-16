---
description: "API and the mocked UI"
paths:
  - "api/**"
  - "ui/**"
---

## UI (mocked, not yet built)

- Task list per repo: rows with git-flavored status badges (`queued`/`running`/`pr_opened`/`completed`/`conflicting`/`failed`) — keep this vocabulary equal to the DB `TaskStatus` enum, which has exactly these six. `completed` means the pipeline ran to the end and opened no PR (read `outcome` and `agent_stop_reason` for why); `failed` is reserved for "repolace itself broke" and is the only status that sets `error_message`. `conflicting` is in the enum and nothing sets it: the conflict-resolution loop is Phase 2. **`merged` was replaced by `pr_opened`**: a task's work ends when the PR exists, and whether it later merges is decided by branch protection and human reviewers — explicitly not repolace's job. Nothing subscribes to PR webhooks, so `merged` was a state the pipeline could never reach. Add it back only alongside the webhook that could actually set it.
- Status is not outcome. A row can show `pr_opened` and still have scored `failed` — status is where the pipeline got to, outcome is whether the issue was fixed. The task list should show both, or it will overstate success.
- Enqueue flow: pick issue → pick target branch → confirm screen (shows PR target branch + a real computed cost estimate, not a static placeholder) → start
- Pipeline trace view: horizontal stage strip (Retrieve/Plan/Edit/Test/Review) with per-stage status color and duration — in Phase 2 this is backed by logs; in Phase 3 it's backed by real OTel spans

## API (decided)

Every route has a Pydantic response model (`api/repolace_api/schemas.py`); status and outcome are the DB enums, so the JSON vocabulary is the database's. Routes: `GET /repos`; `GET /repos/{id}/issues` (number, title, html_url, plus labels as names, author login and created_at); `GET /repos/{id}/branches` (name, `default`); `POST /repos/{id}/tasks` (422 when an explicit `target_branch` is not a branch on GitHub; empty is 400; GitHub failures are 502); `GET /repos/{id}/tasks?status=&outcome=&limit=&before=&include_benchmark=` returns `{items, next_cursor}` (keyset on `(created_at, id)` through an opaque base64url `cursor`, limit 1-100 default 50, 404 unknown repo, 422 bad enum or malformed cursor); `GET /repos/{id}/tasks/estimate`; `GET /tasks/{id}` (the full row, `attempts`, `task_stages` ordered by start, and per-attempt test-run counts, never node ids).

Decisions: stage status is `running|ok|failed` and the UI derives skipped/pending from absent rows; benchmark tasks (`eval_run_id IS NOT NULL`) are hidden from the list unless `include_benchmark=true`; there is no approve action, only the stored approval fields; diff highlighting is the UI's job. The estimate returns p50/p90 (`percentile_cont`) with `basis`: `repo` (last 30 priced non-benchmark tasks of the repo, at least 5), else `model` (all such tasks whose latest `llm_calls` row has the model of the repo's latest task that has one), else `none` with `upper_bound_usd`, the $2.00 per-task cap, kept as `TASK_COST_CAP_USD` in the API because the API image must not import the gateway (change both together). `GET /tasks/{id}` nulls `score_reason`, `patch_diff`, `changed_files`, `error_message`, `test_edit_justification` and stage errors for benchmark tasks (they can name hidden tests) and never returns `claimed_by`. The branch check falls back to `GET .../branches/{name}` when the branch listing hit its page cap.

