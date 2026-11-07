# repolace documentation

Pick the group that matches why you are here. The [project README](../README.md) is the overview.

## The project at a glance

| | |
|---|---|
| What it is | An autonomous agent that turns a GitHub issue into a tested pull request, written in Python (3.12 or newer) |
| Source | [https://github.com/repolace/repolace](https://github.com/repolace/repolace) |
| Licence | MIT, see [LICENSE](../LICENSE) |
| Status | A first benchmark exists (29/54 on 18 public SWE-bench Verified issues, with caveats); the public site is deployed; billing exists only in Stripe test mode. See the [README](../README.md) status table |
| Stack | FastAPI, Celery with RabbitMQ and Redis, PostgreSQL with pgvector, LangGraph, LiteLLM, rootless Docker |
| Author | Rounak Kumar Singh (solo project) |
| Report a vulnerability | [SECURITY.md](../SECURITY.md) |

## Visitors

| Page | Read it to learn |
|---|---|
| [Architecture](architecture.md) | How the nine packages fit together and what happens to a task from enqueue to pull request |
| [The demo run](demo.md) | A real pull request the agent opened on a small public repository, how it was made, and the two problems found in it |
| [Benchmark](benchmark.md) | What the benchmark measures, the headline result with its caveats, and how to reproduce it |
| [Security](security.md) | How model-written code is contained, and what is not protected |
| [BENCHMARK/REPORT.md](../BENCHMARK/REPORT.md) | The full statistical write-up of the recorded run |

## Contributors

| Page | Read it to learn |
|---|---|
| [Getting started](getting-started.md) | Prerequisites, install, env files, running the API, a worker and a task, running the tests |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Checks to run before a pull request, test naming rules, commit conventions |
| [CLAUDE.md](../CLAUDE.md) | The persistent project brief: decisions, build phases, current status |
| [.claude/rules/](../.claude/rules/) | One file per area with the reasoning behind each decided choice. Read the matching file before changing that area |

## Operators

| Page | Read it to learn |
|---|---|
| [Operations](operations.md) | Compose services and ports, env file per service, worker notes, recovery behavior, public site deployment |
| [Billing setup](BILLING_SETUP.md) | Stripe sandbox, restricted key, webhook forwarding, GitHub sign-in, and what is not yet live |
| [infra/aws/README.md](../infra/aws/README.md) | Step-by-step operator procedure for the public site on AWS |
| [Phase 1 testing](PHASE1_TESTING.md) | The order of operations that produced the benchmark number, with a preflight script |
| [SECURITY.md](../SECURITY.md) | How to report a vulnerability, and the known limits |

## Reference

| Page | Contents |
|---|---|
| [Routes](reference/routes.md) | HTTP routes of the API and the public site |
| [Environment variables](reference/env.md) | Every setting, by service |
| [Tables](reference/tables.md) | The database schema |
| [Migrations](reference/migrations.md) | Alembic revisions, in order |
| [Commands](reference/commands.md) | `repolace-run-task`, `repolace-eval` and the test commands |

## Archive

These pages are kept for history. They may not match the current code.

| Page | Note |
|---|---|
| [Interview guide](INTERVIEW_GUIDE.md) | Written on 2026-10-19 and partly updated on 2026-10-25. Sections 1 to 12 and 14 describe the code as read on the first date |
| [Brand notes](brand/README.md) | The wordmark decision and how to regenerate the logo files |
