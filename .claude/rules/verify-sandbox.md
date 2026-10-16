---
description: "Verify sandbox: containment, .git as configuration, byte-identical export"
paths:
  - "verify/**"
  - "shared/repolace_shared/paths.py"
  - "shared/repolace_shared/git/**"
---

## Verify sandbox (decided)

Verify runs the target repo's test suite twice per task — once at the base commit for the baseline, once after each patch attempt. That means executing arbitrary code from two sources: the model's patch, and **the repo's own test files**. The second is the sharper one: `pytest` executes `conftest.py` at *collection*, before any test runs and before a patch is even involved. Registering a hostile repo is the whole attack; no agent cooperation is required.

There are **two independent dangers here, and a sandbox only addresses one of them.** Conflating them is the mistake to avoid.

### Danger 1 — untrusted code running (containment)

Handled by the sandbox. The naive shortcut, mounting the host's `/var/run/docker.sock` so the worker can spawn sibling containers, is root-equivalent on the host and defeats the entire point.

**Decided: right-size the isolation to who can register a repo.** Today that is one person hand-picking 15–20 benchmark repos, so the attacker would have to be the operator. A locked-down rootless container is proportionate: no network during the test run, dropped capabilities, `no-new-privileges`, read-only root filesystem, memory/CPU/PID caps, and a hard timeout. Escalate to gVisor (syscall interception) or Kata/Firecracker (real VM per sandbox) **when strangers can register repos, not before** — gVisor has syscall-compatibility gaps, Kata needs nested virtualisation, and Firecracker is a build project rather than a drop-in runtime. Keeping the boundary clean is what makes the runtime swappable later.

Note a container is **not** a VM: it shares the host kernel, so a container escape is usually a kernel bug. That is precisely the gap gVisor and Firecracker exist to fill, and the reason the escalation path above is worth keeping open.

### Danger 2 — the confused deputy (`.git` is executable configuration)

**A better sandbox does not fix this, at any price.** Nothing escapes. The sandbox writes a file, stays perfectly contained, and exits. Then the *host*, later, voluntarily reads that file and acts on it.

Both halves were reproduced against this codebase:

- **Host code execution.** The sandbox writes `.git/hooks/post-commit`. The container is torn down. The host then runs an ordinary `git commit --no-verify` for the next attempt — and the hook executes as the worker user. Verified: `--no-verify` suppresses `pre-commit` but **not** `post-commit`. `post-checkout` fires on `git checkout -b`, `pre-push` on the push, and `core.fsmonitor` on *every* `git status`.
- **Credential redirection.** The sandbox rewrites `remote.origin.url` in `.git/config` (or adds `url.<evil>.insteadOf`, which rewrites explicit URLs too, so passing a URL instead of a remote name is not a fix; or sets `http.proxy`). The host's next `git push` then sends the installation token to a host of the attacker's choosing. Demonstrated against a local listener, which received `x-access-token:<token>` in cleartext over plain HTTP.

Hooks are **not** shippable in a repo — `git clone` never transfers them, verified. Both attacks require the sandbox to *write* into `.git`, which is only possible because `.git` happens to sit inside the working tree the sandbox legitimately needs.

**Correction to an earlier revision of this note (matters, don't undo it):** the sentence above says both attacks "require the sandbox to *write* into `.git`". That is true of those two instances and **false of the class**, and the gap was reproduced against this code. `.gitattributes` is a **tracked file** — an ordinary file in the repository, present in every clone, which the sandbox never has to touch. It supplies the *selector* half; the *command* half comes from the operator's own `~/.gitconfig`. `*.py filter=x` plus a global `filter.x.smudge` runs a command on the host during any checkout; `diff=x` plus a global `diff.x.textconv` runs one during the Reviewer's diff. `_BASE_ARGS` stops neither — it clears the keys it enumerates, which is the enumeration game this section already calls unwinnable — and withholding `.git` from the sandbox stops neither, because neither half came from the sandbox.

**Fixed, structurally:** git's environment now pins `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` to `/dev/null` (`sanitized_git_env`), so git reads **no configuration file it was not handed on the command line**. No key list, so keys nobody has thought of yet are covered. Note `HOME` cannot simply be dropped from the allowlist instead — git needs it for other reasons, and the suite itself had been silently running against whatever the developer had configured until this landed.

This does **not** cover the checkout's own `.git/config`, which the sandbox *can* write. That half is closed separately, by never letting a filter or attribute run during the export at all — see "Byte-identical export" below.

**The lesson worth carrying:** *"the sandbox never receives `.git`" bounds what the sandbox can plant, not what the host will read.* Every host-side git invocation is still a confused deputy for whatever configuration the host itself supplies.

### A third route into the same class: paths the host chooses to follow

`.git` sitting outside the sandbox does not help when the *host* is handed a path and follows it. Two routes were live and are now closed, and neither involves the sandbox or `.git` at all:

- **Arbitrary host file read.** `retrieval.index` walked the checkout with `os.walk` and read any `.py` it found. `os.walk` does not descend a symlinked *directory*, but it does list a symlinked *file*, and `read_text` resolves it. git stores a symlink as an ordinary mode-`120000` entry and clones it back verbatim, so a repo committing `settings.py -> /home/worker/.env` put a host file into `code_chunks.content` — from where it is retrievable and, in Phase 2, goes into an LLM prompt at a third-party API. Reproduced. Note the *incremental* path is the live one and never went through the walk at all: `_incremental_index` builds `repo_path / rel` straight from `git diff --name-only`.
- **Arbitrary host file write.** `apply_stub_edit` did `repo_path / file_path`. Under pathlib semantics that discards `repo_path` entirely for an absolute `file_path` — `Path("/repo") / "/etc/cron.d/x"` *is* `/etc/cron.d/x`, with no `..` and nothing in the string that looks wrong — and follows a symlink for a relative one.

**The rule now: every path that reaches the filesystem from a database row, a diff, or a model goes through `repolace_shared.paths.resolve_within`, and a symlink is refused rather than followed.** Refusing rather than following matters even when the target happens to land inside the tree: following it would still be letting the repository choose where a later read or write goes.

The Editor additionally refuses anything under `.git`. That is protection the *stub* editor does not need — its path came from a row it wrote itself — and the real Editor will: `.git/hooks/post-commit` is one model-chosen string away from host code execution on the next `record_attempt`. Deliberate, and worth keeping when the stub is replaced.

**Decided: the sandbox never receives `.git`.**

- **In:** source files only, copied out of the checkout without `.git`. No credentials, no remote, no network during the test run.
- **Out:** *data only* — the pass/fail test-ID sets and stdout. Never files that get executed, never anything written back into `.git`. Returning, say, a patch file that the host blindly applied would reopen a smaller version of the same hole.

This deletes the whole class rather than blocking instances, which matters because the instance list (`diff.<d>.textconv`, `filter.<d>.smudge`, `core.pager`, `core.sshCommand`, `uploadpack.packObjectsHook`, …) grows with every git release. The mitigations already in `_BASE_ARGS` (`core.hooksPath=/dev/null`, `core.fsmonitor=`) and the host-checked credential helper stay as defence in depth — two independent things then have to be wrong — but they are no longer the primary control.

This also settles the older open question of **whether Verify runs in the checkout or a copy: a copy, necessarily**, since the copy is what omits `.git`.

### Byte-identical export (decided)

The sandbox receives the tracked tree, and it now receives it as **the bytes the commit contains**: enumerated with `git ls-files -s -z`, fetched with `git cat-file --batch`, written straight to disk. Not by asking git to materialise a working tree, because every mechanism that does applies some transformation, and each transformation is a way for what the sandbox runs to differ from what the commit holds and the Reviewer reads.

- **Not `git archive`**, for the reason already recorded: it honours `export-ignore` in `.gitattributes`, which would let a repository hide its own test files from the run that establishes ground truth. An attack on the benchmark number, and the kind that would never look like an attack.
- **Not `checkout-index`**, which was the previous implementation. It honours neither `export-ignore` nor `export-subst` — but it *does* run clean/smudge filters, `text`/`eol` conversion and `ident` expansion. Measured on this repository with a one-line `.gitattributes` (`*.py text eol=crlf`): **72 of 110 tracked files came out byte-different from the commit.** A `filter` driver is worse still, since its value is an arbitrary command run on the host. Pinning git's config files (see the correction under "Danger 2") closed the half of that which came from the operator's `~/.gitconfig`; it does **not** close the checkout's own `.git/config`, which is inside the tree the sandbox writes to. Reading blobs consults no attribute or filter machinery at all, so it closes both — structurally, with no key list.

Only index entries are listed, so `.git`, untracked files and ignored build artifacts are excluded by construction, exactly as before.

**Modes `120000` (symlink) and `160000` (gitlink) are refused loudly**, not approximated. A submodule has no blob to write, and `checkout-index` silently left an empty directory — so the suite ran against missing sources and produced a confidently wrong number rather than an error. A symlink is not a file with content, so "the bytes match the commit" stops being a well-formed claim, and materialising one would put a path resolving outside the export into a tree the host later deletes. Paths stay `bytes` end to end for the same fidelity reason: a repository may hold a filename that is not valid UTF-8, and decoding it with `errors="replace"` would write a *different* file.

**The cost is real and accepted:** a repository containing any symlink cannot currently be exported — and this repository is one of them, since `infra/.env` is a tracked symlink. Some real Python projects have a `LICENSE` or docs symlink too. The refusal names every offender at once so a rejected benchmark repo is diagnosable at a glance, and it raises a distinct `GitExportError`, so a future `RepoSpec` opt-in is a contained addition rather than a rewrite. **This is a constraint on picking benchmark repos, and it should be checked before the set is finalised, not after.**

**Directory modes go all the way down, not just onto the root.** `mkdir`'s mode argument is masked by the process umask, and the old code chmodded only the export root — so `checkout-index` created `0755` subdirectories under a `0777` root, and a sandbox running as an unprivileged uid got `EACCES` writing a `__pycache__` beside its own code. That surfaces as an unscoreable run, which silently drops the instance from the benchmark rather than failing visibly. Files keep `0644`/`0755` from their index mode: what the sandbox needs is permission to create entries *in a directory*, and the executable bit has to survive for suites that shell out to a tracked script.

**`tree_matches_head` gates all of this, and is a precondition rather than a warning.** It compares the index *and* the working tree against HEAD. It previously passed `diff-index --cached`, which compares only the index and **ignores the working tree** — and the agent edits the working tree directly, with staging happening later in `record_attempt`. So an unstaged edit passed the guard, the export handed over HEAD's content, the sandbox tested code the agent had not written, and the pass/fail sets were attributed to the attempt regardless. A confidently wrong measurement, which is the worst shape this stage can fail in. It now uses `status --porcelain --untracked-files=no`: that refreshes the index itself, so stat noise cannot read as a modification, and it needs no exit-code interpretation — `diff-index --quiet` exits 1 for "differs" and 128 for a real failure, and the old code swallowed both alike, so a corrupt repository reported "index differs".

### Where the boundary falls

| Inside the sandbox | On the host |
|---|---|
| The repo's test suite | Clone, branch, commit, squash, push |
| Dependency installation | Indexing, retrieval, every LLM call |
| | Applying the patch **as text** |
| | Reviewer reading `git diff`, opening the PR |

The rule: **executing the repo's code goes inside; everything else stays outside.** Applying a patch is writing text, not executing it, so it does not need containing. No agent ever runs inside the sandbox — Verify is a stage, not an agent.

### The part that will actually cost the time

**Getting each repo's dependencies installed so its suite runs at all.** Not optional, fiddly per repo, and the reason SWE-bench ships a prepared image per repo. It also creates the one genuine tension with the security design: installing needs network, and network is the exfiltration path. Resolution: **network on during a separate build/install step, off during the test run.**

Related decision: **install dependencies once into an image, then start a fresh container per run from that image.** Reusing one live container across retry attempts is faster but lets state leak between attempts — a stale `.pyc`, a mutated fixture database — which quietly corrupts the benchmark. A fresh container from a prepared image pays the install cost once and still gives each attempt a clean filesystem.

**Implemented in `verify/`:** `spec.py` (per-repo `RepoSpec` from `verify/specs.toml`, plus the install heuristic for an uncurated repo), `dockerfile.py` (rendering the image, and the cache key that decides when it is rebuilt), `backends/docker.py` (`build_run_argv` and `DockerBackend`), `stage.py` (`Verifier`, the seam `run.py` calls). Four things in there are load-bearing and easy to undo by accident:

- **The image cache key covers the Dockerfile, the plugin's bytes and the dependency *manifests* — never the rest of the tree.** Keying on source would rebuild per attempt, and the baseline and the attempt would then have run in different environments. That is exactly the ambiguity the baseline exists to remove: `score` could no longer tell a suite the patch broke from one a differently-resolved dependency broke.
- **`build_run_argv` is pure and its test asserts each flag separately.** Every one of these fails *open*: a misspelled `--secutiry-opt` is rejected by the CLI and surfaces at once, while a dropped `--network=none` is simply a container with network and nothing about the run looks different. A golden-argv comparison would go stale and get regenerated rather than read.
- **The timeout path removes the container by name.** `run_process` kills the `docker run` client; the container is a child of the daemon and survives it, holding the memory cgroup and a bind mount of a directory the workspace is about to delete. `--rm` does not help — it fires on exit, which is what is not happening. This is why `container_name` is a required keyword on the Protocol and why it includes the task id.
- **The `docker` pytest marker is enforced by a `pytest_runtest_setup` hook in the root `conftest.py`,** skipping when no daemon answers and *failing* when `REPOLACE_TEST_DOCKER_REQUIRED=1`. The marker was declared in `pyproject.toml` before anything implemented it, so a daemon-backed test would have passed on a machine with no daemon — and the containment assertions are the ones that must never pass vacuously.

**What running it for the first time found, and neither was catchable by unit tests.** Both sides of each boundary were behaving exactly as written; only an actual container revealed the interaction.

- **The fingerprint was not stable, and it would have failed every task on every repository.** pytest names a plugin `getattr(plugin, "__name__", None) or str(id(plugin))`, and `PytestPluginManager` registers *itself* that way — so `fingerprint["plugins"]` carried a fresh memory address on every run. `fingerprint_changed` compares that list between baseline and attempt and `score` returns FAILED on a difference **before it looks at a single test result**. The benchmark would have been a uniform wall of confident, spurious failures. `_plugin_names` now counts unnameable registrations (`<anonymous:N>`) instead of naming them — counted rather than dropped, so a conftest registering an object of its own still shows as a difference. Pinned by `TestFingerprintStability` in `verify/tests/test_plugin.py`, which runs the real plugin twice in one directory and needs no daemon.
- **The sandbox left a `.pytest_cache` the host could not delete.** Written into the bind-mounted export as uid 65534 with mode 0755; deleting inside it needs write permission on a directory the host does not own, so `task_workspace`'s cleanup failed and every task leaked its temp tree. Fixed with `-o cache_dir=/tmp/.pytest_cache` — into the tmpfs, which dies with the container. `-o cache_dir` rather than `-p no:cacheprovider` so the `cache` fixture and `--lf` keep working, and `cache_dir` is not a fingerprinted ini option so it cannot read as configuration drift. **The general hazard remains** for any suite that writes into its own tree, and the remedy when it bites is the same one used to clear the stale dirs: delete them from inside a container, since the host cannot chmod files owned by a subuid.

**Rootless is a deployment precondition, not a preference.** Without cgroup v2 delegation, rootless Docker silently ignores `--memory`, `--cpus` and `--pids-limit` — the containment controls, failing open again. `docker info` reports the missing delegation as a warning; check it before trusting a run. Verified on the development host: `cpu memory pids` are delegated and the caps bind (`memory.max`, `pids.max` and `cpu.max` read back inside the container exactly as set). `cpuset` and `io.*` are **not** delegated, so `DockerLimits.cpuset_cpus` is silently ignored there — do not reach for it as a containment control without re-checking.

