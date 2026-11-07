# Security

repolace runs code it did not write: the model's patch and the target repository's own test suite. This page explains how that is contained, what the agent can and cannot reach, and, in [What is not protected](#what-is-not-protected), where the limits are. To report a vulnerability, follow [SECURITY.md](../SECURITY.md).

The design reasoning, including the attacks that were reproduced against this code, is in [.claude/rules/verify-sandbox.md](../.claude/rules/verify-sandbox.md).

## Two separate dangers

A sandbox answers only one of them.

1. **Untrusted code running.** `pytest` executes `conftest.py` at collection, before any test runs and before any patch exists. Registering a hostile repository is the whole attack. A container contains this.
2. **The confused deputy.** Code inside the sandbox writes a file, stays perfectly contained, and exits. Later the host reads that file and acts on it: a `.git/hooks/post-commit` hook that runs on the next commit, or a rewritten `remote.origin.url` that sends the host's token to another server. A better sandbox does not fix this, because nothing escaped. The fix is to never let the host read anything the sandbox could have written.

## The sandbox

`verify/verify/backends/docker.py` builds the `docker run` argument list in one pure function, `build_run_argv`, and the scratch-script path shares the same containment list. A test asserts each flag separately, because every one of them fails open: a dropped `--network=none` is simply a container with a network, and nothing about the run looks different.

| Control | Setting |
|---|---|
| Network | `--network=none`, unconditional; no spec field can weaken it |
| Root filesystem | `--read-only`, with `/tmp` as a tmpfs (`nosuid,nodev`, 256 MB) |
| Capabilities | `--cap-drop=ALL` |
| Privilege | `--security-opt=no-new-privileges` |
| User | non-root `65534:65534` |
| Memory | `--memory 2g`, with `--memory-swap` equal, so a container cannot exceed the cap by swapping |
| CPU | `--cpus 2.0` |
| Processes | `--pids-limit 512` |
| Files | `--ulimit nofile=4096:4096` |
| Logs | `--log-opt max-size=10m`, one file |
| Image | `--pull=never`: only the image that was built beforehand can start, so there is no registry access at run time |
| Init | `--init`, so background processes do not leave zombies that keep the pid cgroup populated |
| Entrypoint | explicit, so an image's own `ENTRYPOINT` cannot take over the pytest invocation |
| Timeout | a hard timeout; on expiry the backend removes the container **by name**, because killing the `docker run` client does not stop a container that belongs to the daemon |

Dependencies are installed once into a per-repo image, with the network on, in a separate build step. Each test run then starts a fresh container from that image with the network off, so no state leaks between attempts.

**Rootless daemon.** The whole stack must run on the rootless Docker daemon. Without cgroup v2 delegation of `cpu`, `memory` and `pids`, rootless Docker silently ignores `--memory`, `--cpus` and `--pids-limit`. Check `docker info` for a delegation warning before trusting a run. The compose worker mounts only the rootless socket. Never mount the rootful `/var/run/docker.sock`: it is root-equivalent on the host and would defeat the sandbox.

**A container is not a VM.** It shares the host kernel. A container escape is usually a kernel bug. The backend has a runtime setting so gVisor (`runsc`) can be adopted later; the design records stronger isolation (gVisor, Kata, Firecracker) as the step to take when strangers can register repositories.

## The source never carries `.git`

The sandbox receives the tracked tree and nothing else.

- **Byte-identical export.** The tree is enumerated with `git ls-files -s -z` and each blob is read with `git cat-file --batch` and written straight to disk. The export does not use `git archive` (it honours `export-ignore`, which would let a repository hide its own tests from the run that sets ground truth) or `checkout-index` (it runs clean/smudge filters and line-ending conversion; in one measurement a one-line `.gitattributes` made 72 of 110 files differ from the commit). Reading blobs consults no attribute or filter machinery at all.
- **Symlinks and submodules are refused.** Modes `120000` (symlink) and `160000` (gitlink) raise an error that lists every offender. Repositories that contain either cannot currently be exported.
- **Only data comes back.** The pass/fail test-id sets and a bounded output tail. Never a file the host would execute, and never anything written back into `.git`.
- **`tree_matches_head` is a precondition.** The export refuses a tree that differs from `HEAD`, so the sandbox cannot test code the agent did not write and attribute the result to the attempt.

## Host-side git

Every git command on the host runs in a sanitized environment (`shared/repolace_shared/git/repo.py`):

- `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` point at `/dev/null`, so git reads no configuration file it was not handed on the command line. This covers keys nobody has thought of yet, such as a `filter.<name>.smudge` or `diff.<name>.textconv` command that a tracked `.gitattributes` could select.
- Hooks and `core.fsmonitor` are disabled on every invocation.
- The environment is an allowlist, not the process environment.

**Credentials never appear in a URL.** The remote is plain HTTPS. A scoped credential helper answers git's request only when the protocol is `https` and the host is the expected one, and reads the token from an environment variable whose *name* (not value) is on the command line. A rewritten remote therefore produces a failed push, not a leaked token.

**Every path goes through one guard.** Any path that reaches the filesystem from a database row, a diff or a model goes through `repolace_shared.paths.resolve_within`, which refuses `..`, absolute paths and any symlink, even one that lands inside the tree.

## The agent's authority comes from its tools

The agent can do what its nine tools do and nothing else.

- **One confinement function** for every path. It refuses empty, over-long, control-character and backslash paths, `..`, absolute paths, any symlink in the path, and any `.git*` component for reads. For writes it also refuses every dotfile and root-level CI definitions.
- **`grep`** runs `git grep` under `prlimit`, with model strings never placed before `--`.
- **Scratch scripts and probes** run in the sandbox with no network.
- **Bounds**: at most 8 tool calls executed per model reply, results capped at 8,000 characters, and the cost, call and wall-clock limits enforced in the gateway rather than trusted to the agent.

## The no-oracle protocol

In benchmark mode the hidden fail-to-pass tests are the answer key. If the agent could see which are red, it would iterate against the key and the benchmark would measure how well it reads the key. So hidden tests never reach it ([.claude/rules/no-oracle.md](../.claude/rules/no-oracle.md)):

- The hidden test files are overlaid on the sandbox export, never on the checkout, so no tool can read them. Probes carry no overlay, so `run_tests` cannot run them.
- `agents/repolace_agents/feedback.py` is the only place a scored result becomes text a model can read. It drops test ids and collection failures under an overlay path, computes counts and verdicts from the filtered result so a count cannot encode a hidden test, and drops the output tail, the exit code and the duration.
- The protection is keyed on the task having an instance id, not on the overlay being non-empty, so an empty overlay cannot switch it off.
- A test id is shown only if the baseline run produced it, because the scored run executes the agent's own code and an id on a visible path could be text the agent chose.
- Operator-side text for benchmark tasks (logs, gate reasons) names counts, not ids, and a benchmark pull request or commit message carries no outcome-bearing text.

This is a filter, not a physical barrier. See the limits below.

## Prompt injection

Issue text, retrieved code, test ids and test output can all be written by someone who is not the repository owner. The prompt asks the model to refuse injected instructions, but **the real bound is code**: a test pins that a hostile issue changes neither the tool list nor the system prompt apart from a nonce.

- Untrusted text is wrapped in blocks tagged with a per-task random nonce. Before wrapping, it is length-cut, stripped of invisible and control characters, and has closing tags neutralized in one linear pass (so the defense does not rest on the nonce staying secret).
- The agent's own `submit` summary is sanitized, quoted in a fenced block, and has `@mentions` and bare `#123` references broken, so a pull request body cannot ping a person or notify another project.
- Product pull requests say `Refs #N`, never `Fixes #N`.
- A transcript budget elides old tool results, and malformed tool-call arguments are replayed as `{}`.

Full list: [.claude/rules/prompt-injection.md](../.claude/rules/prompt-injection.md).

## Public and private are separate processes

| | Private API (`repolace_api.main`) | Public site (`repolace_api.site.main`) |
|---|---|---|
| Serves | `/repos`, `/tasks`, `/ui`, `/github`, `/healthz` | Landing, pricing, `/benchmark/`, `/retrieval-benchmark/`, waitlist, `/healthz` |
| Authentication | **None** | Not needed: no task routes |
| Reachable from | localhost or a private tunnel | A public hostname or tunnel |
| Env file | `infra/api.env` | `infra/site.env` |

The site mounts no router from `repolace_api.routes` and not `repolace_api.ui`. A test imports it in a clean interpreter to prove neither is loaded, and `/ui`, `/repos`, `/tasks`, `/github/webhook`, `/docs` and `/openapi.json` answer 404 there. Its database role can INSERT into `waitlist_signups` and nothing else. The waitlist form has a same-origin check, an 8 KB body cap, a honeypot field and an in-memory rate limit.

The private API and UI add three defenses that are **not** authentication:

- **Host allowlist.** Every request needs a `Host` on `UI_ALLOWED_HOSTS` (default `localhost,127.0.0.1,[::1]`) or gets 400. This stops DNS rebinding. Exempt by exact path: `/github/webhook` (authenticated by its HMAC signature) and `/healthz`. Never add a public hostname to this list.
- **Same-origin check** on the one state-changing HTML request, the enqueue POST: `Origin`, else `Referer`, must match the allowlist, and `Sec-Fetch-Site: cross-site` is refused.
- **Response headers** on `/ui`: `Content-Security-Policy: default-src 'self'`, `nosniff`, `X-Frame-Options: DENY`.

## Secrets

| Secret | Where it lives |
|---|---|
| Provider keys | In the `worker` container's env file (`infra/worker.env`) and in the root `.env` for processes you run on the host. Not in `infra/api.env`, `infra/site.env`, or the `worker-maintenance` container, which has no env file |
| GitHub App private key and webhook secret | In both `infra/api.env` (the API lists issues and branches and verifies webhooks) and `infra/worker.env` (clone, push, PR). Not in the site or the maintenance worker |
| Installation tokens | Minted per use; passed to git through an environment variable, never in argv or a URL |

- Provider keys are handed to LiteLLM per call and are never exported into `os.environ`, so git, Docker and the sandbox cannot inherit them. The Docker CLI's environment is an allowlist, not a subtraction.
- `llm_calls` redacts credential-shaped strings and the exact keys the gateway holds before storing a request or response.
- Env templates (`*.env.example`) hold names only, and a test keeps `infra/api.env.example` equal to the API's settings in both directions, so a provider key pasted into it fails the build.
- Run a host process from the repository root and it reads the root `.env`, which holds provider keys. The statement "the API has no provider keys" is true of the compose `api` container, not of an API you start by hand in a checkout whose `.env` contains them.

## What is not protected

Read this section before pointing repolace at anything that matters.

- **The private API and task UI have no authentication.** Anyone who can reach the port can enqueue a task, which spends provider money and runs code on the host. The Host allowlist and same-origin check stop DNS rebinding and cross-site forgery, not a person with network access. Keep them on localhost or a private tunnel.
- **A container shares the host kernel.** A kernel bug could allow an escape. There is no gVisor or VM layer today.
- **Resource caps depend on your host.** Without cgroup v2 delegation they are silently ignored. A suite can also write as much as it likes into its export directory, which no container limit covers, so the workspace parent directory should sit on a size- and inode-limited filesystem.
- **Almost any file is writable by the agent, including files CI executes** (`Makefile`, `setup.py`, `noxfile.py`, scripts). repolace pushes the agent branch to the same repository the pull request targets. If that repository has push-triggered CI with secrets, that CI would run agent-written code before a human looks. Do not point product mode at such a repository without deciding that deliberately, and disable Actions on test repositories.
- **A hostile issue can still mislead the agent.** The tools bound what it can do, not what it decides to do. A convincing injected instruction can produce a wrong edit that passes its checks.
- **The no-oracle filter is not a physical barrier.** A deliberately adversarial agent could still try to signal through it.
- **`llm_calls` stores repository source verbatim**, and that source was sent to the model provider. Redaction strips credential-shaped strings, not code. Treat the database accordingly.
- **Error text can contain host paths.** Secrets are scrubbed from stored errors, paths are not, and the failed-task page shows them.
- **The public-site deployment on AWS and the worker deployment have each been exercised only partly.** The AWS path has deployed once; reboot recovery and a backup restore have not been tested.
- **Indexing is per repository, not per commit**, so concurrent product tasks on one repository can see each other's index.
