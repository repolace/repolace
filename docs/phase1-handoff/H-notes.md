# H notes (running)

- env: no sudo; Postgres 16 + pgvector via the pgserver pip wheel (scratchpad), TCP :54329, db hdev, suffix _h. Wrapper: scratchpad/t.sh
- branch: local `p1/h-integration-wt` pushed to origin `p1/h-integration` (the local name `p1/h-integration` is checked out in the repolace-wt/h-integration worktree)
- baseline: pipeline/tests all pass before my changes (426)
- DONE commit 2f9f493: deps + uv.lock (unsigned: 1Password "failed to fill whole buffer" twice). uv.lock is in the same commit as the pyproject edits, not its own.
- DONE: agent_runner.py (LLMAgent -> run_agent), cli default llm + build_llm_client/preflight, run.py `_retrieve` uses build_query + stored index_strategy, GraphRecursionError -> STEP_CAP in `_agent_stop_from`
- TODO tests: retrieval spy, recursion, e2e real graph (happy / retry / budget / hostile / benchmark / agent-failure), run-record completeness, report UNCURATED
- AMBIGUITY: GraphRecursionError -> step_cap (graph docstring says harness error; H brief item 9 says COMPLETED)
- REQUEST gateway: public `LLMClient.preflight(stage)` (the CLI uses `_verify_usable` / `_config`)
