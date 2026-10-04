# H notes (final state)

Stream H, integration: the real agent inside `run_task`.

## Where it lives
- Branch: local `p1/h-integration-wt`, pushed to origin as `p1/h-integration`. (The local name `p1/h-integration`
  is checked out in the `repolace-wt/h-integration` worktree, so it could not be reused.)
- Code: `pipeline/repolace_pipeline/agent_runner.py` (new, `LLMAgent` -> `run_agent`), `cli.py` (default `llm`,
  `build_llm_client`, `preflight_agent_models`), `run.py` (`_retrieve` through `build_query`, `_stored_strategy`,
  `GraphRecursionError` -> `STEP_CAP` in `_agent_stop_from`).
- Tests: `test_pipeline_integration_db.py`, `test_pipeline_integration_scenarios_db.py`, `test_pipeline_wiring_db.py`,
  `test_pipeline_cli.py`, support in `pipeline_llm_support.py`.

## Environment used (cloud session)
- No sudo, no Docker daemon. Postgres 16.2 + pgvector 0.6.2 from the `pgserver` pip wheel, TCP :54329,
  database `hdev`, `REPOLACE_TEST_DB_SUFFIX=_h`, throwaway data dir in the scratchpad.
- A throwaway `.env` with only fake `REDIS_URL` / `CELERY_BROKER_URL` was needed for two eval runner subprocess tests
  (their child gets an allowlisted environment); deleted afterwards.

## Decisions to review
- `GraphRecursionError` is an agent-caused stop (`step_cap`), per the H brief; the graph module's docstring says
  it means repolace broke. Charging a runaway loop to the harness would take the instance out of the denominator.
- `UnpricedModelError`, `MissingProviderKey`, `NoTaskScope` and any other exception stay harness errors.
- Retrieval queries with the strategy read back from `registered_repos.index_strategy` after indexing, not the seam.
- The CLI preflight calls the gateway's private `_verify_usable` / `_config` (no public preflight exists).
