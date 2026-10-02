"""The two tools that execute code: `run_python` and `run_tests`.

Neither runs anything itself. Each hands the tree to the sandbox through a
callable the pipeline built, and the sandbox is where the repository's code and
the model's script actually execute -- no network, read-only source, nothing it
writes comes back. What *these* tools own is the order of operations and the
arguments:

* **Checkpoint first, always.** The sandbox export refuses a tree that differs
  from HEAD, so the working tree is committed before it is handed over. That
  commit lives in the tool, not the verifier, so `verify` stays git-free; and it
  is why `AgentResult.last_attempt`, not `HEAD`, is the scored state.
* **Arguments are checked before anything runs.** A `run_tests` target ends up in
  pytest's argument list, so one that starts with `-` would be an *option*
  (`-p evil` loads a plugin, `--rootdir=/` moves the tree); it must be a plain
  node id naming a file the model could have read.
* **A sandbox failure is a tool result, not a crash.** The callables return
  errors (`SuiteResult.error`, `ScriptResult.error`) and raise only
  `VerifierNotReady`; all of them reach the model as "the sandbox is unavailable".

Output budgeting: `ToolBox` cuts an over-long result from the *end*, and for
these tools the end is the traceback. So each one fits its own tails inside
`ToolLimits.max_output_chars` instead of leaving the box to chop them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from repolace_shared.git import GitError
from verify.stage import VerifierNotReady

from repolace_agents.tools.base import ToolContext, ToolError, ToolOutcome, ToolSpec
from repolace_agents.tools.wording import TESTS_NOT_SHOWN
from repolace_agents.tools.paths import confine, relative_posix, require_text, shown

#: Per-stream cap for `run_python`, and the cap for a suite's output tail. Both are
#: ceilings: a small `max_output_chars` shrinks them so the whole result still fits.
SCRIPT_TAIL_CHARS = 4000
TEST_TAIL_CHARS = 6000

#: How many failing ids and collection errors a `run_tests` result lists.
MAX_LISTED_IDS = 50
MAX_ID_CHARS = 200

#: What the model may see of a sandbox error message.
MAX_ERROR_CHARS = 200

#: The file part of a target -- everything before `::`. The first character is a letter,
#: digit, `.` or `_`: never `-` (an option) and never `@` (pytest expands `@file` into
#: arguments read from a file the agent can write, which would smuggle in `-o`, `-W`,
#: `--junitxml=`...). The rest is the characters a real path has; a space or `#` is fine
#: because the sandbox receives argv as a list, so each target is one argument whatever it
#: holds. `fullmatch`, because `$` would accept a trailing newline.
_SAFE_PATH = re.compile(r"[A-Za-z0-9_.][\w./\[\]#,=@+ -]*")

#: The node-id part, after `::`: any text without control characters. Parametrised ids are
#: arbitrary (`test_x[(1, 2)]`, `test_x[<lambda>]`, `test_x[a|b]`, `test_x[50%]`), and since
#: this sits behind a file path inside one argv element it cannot become an option.
_NODE_ID = re.compile(r"[^\x00-\x1f\x7f]*")

_FIRST_CHAR = re.compile(r"[A-Za-z0-9_.]")

_UNAVAILABLE = "the sandbox is unavailable: {why}; carry on by reading the code (read_file, grep)"


def _unavailable(why: str) -> ToolError:
    return ToolError(_UNAVAILABLE.format(why=why))


async def _checkpoint(ctx: ToolContext, message: str) -> None:
    """Commit the working tree, turning a git failure into a message the model can read.

    A tree git cannot snapshot (it refuses a path, a lock is stuck) is not something
    the model can fix, but ending the run on it hides why; as a `ToolError` the model
    is told, and every later call fails the same visible way instead of crashing the
    run on the first. Only git's own errors are mapped: anything else is a bug.
    """
    try:
        await ctx.checkpoint(message)
    except GitError as exc:
        raise ToolError(f"could not snapshot the working tree: {shown(str(exc), MAX_ERROR_CHARS)}") from None


def _tail(text: str, limit: int) -> str:
    """The last `limit` characters, marked when something was cut, never longer than `limit`."""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[len(text) - limit :] if limit > 0 else ""
    return "..." + text[len(text) - (limit - 3) :]


class RunPython:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self.spec = ToolSpec(
            name="run_python",
            description=(
                "Run a throwaway Python script to see how the code behaves. It runs in a sandbox with "
                "NO network. The script is /scratch/main.py; the repository is mounted read-only at "
                "/repo and is on sys.path, so `import your_package` works. Nothing the script writes "
                "persists, and nothing it does changes the repository: to change code use edit_file. "
                "Returns the exit code and the end of stdout and stderr. Use it to reproduce the bug "
                "before you fix it and to check the fix; it does not run the test suite (use "
                "run_tests) and it is not scored."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "minLength": 1, "maxLength": 20_000, "description": "The Python source to run."},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": int(ctx.limits.max_script_timeout),
                        "description": f"Kill the script after this long (default {ctx.limits.default_script_timeout:g}).",
                    },
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        ctx = self._ctx
        if ctx.run_script is None:
            raise _unavailable("this run has no sandbox, so scripts cannot be executed")
        require_text("code", args["code"])  # it is written to a script file
        timeout = min(args.get("timeout_seconds", ctx.limits.default_script_timeout), ctx.limits.max_script_timeout)

        # Before the sandbox, not after: the export refuses a tree that differs from HEAD.
        await _checkpoint(ctx, "checkpoint: before script")
        try:
            result = await ctx.run_script(args["code"], timeout)
        except VerifierNotReady:
            raise _unavailable("the test environment has not been prepared") from None
        if result.error:
            raise _unavailable(shown(result.error, MAX_ERROR_CHARS))

        header = [f"exit code: {result.exit_code if result.exit_code is not None else 'none'}"]
        if result.timed_out:
            header.append(f"the script was killed after the {timeout:g}s timeout")
        if result.truncated:
            header.append("the sandbox cut its output at a capture limit, so the tails below may be incomplete")
        labels = ("--- stdout ---\n", "\n--- stderr ---\n")
        fixed = len("\n".join(header)) + 1 + sum(len(label) for label in labels)
        per_stream = max(0, min(SCRIPT_TAIL_CHARS, (ctx.limits.max_output_chars - fixed) // 2))
        stdout = _tail(result.stdout, per_stream) or "(empty)"
        stderr = _tail(result.stderr, per_stream) or "(empty)"
        return ToolOutcome("\n".join(header) + "\n" + labels[0] + stdout + labels[1] + stderr)


class RunTests:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self.spec = ToolSpec(
            name="run_tests",
            description=(
                "Run some of the repository's tests in the sandbox, to check your change as you work. "
                f"{TESTS_NOT_SHOWN} Targets are pytest paths or node ids such as 'tests/test_x.py' or "
                "'tests/test_x.py::test_name'; each must name a file that exists, and none may start "
                "with '-'. A pass here is not a verdict on your change and is not scored. Returns the "
                "counts, the failing test ids and the end of pytest's output."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": ctx.limits.max_targets,
                        "items": {"type": "string", "minLength": 1, "maxLength": 300},
                        "description": "Test files, directories or node ids to run.",
                    },
                },
                "required": ["targets"],
                "additionalProperties": False,
            },
        )

    def _normalise_target(self, target: str) -> str:
        """The repo-relative form of a target the sandbox may be given, or a `ToolError`.

        What reaches pytest is this normalised string, never the raw one: `./-x`
        passes the character check as typed and would be the option `-x` once
        normalised, so the first-character rule is applied again to the result.
        """
        require_text("target", target)
        if target.startswith("-"):
            raise ToolError(f"target {shown(target)!r} starts with '-', which pytest would read as an option; name a test file or node id")
        path_part, separator, node = target.partition("::")
        if not _SAFE_PATH.fullmatch(path_part):
            raise ToolError(
                f"target {shown(target)!r} is not a plain test path or node id: the file part must start with a "
                f"letter, digit, '.' or '_' and use only letters, digits, spaces and . / _ - [ ] # , = @ +"
            )
        if not _NODE_ID.fullmatch(node):
            raise ToolError(
                f"target {shown(target)!r} has a control character after '::'; if a parametrised id is the "
                f"problem, run the test without its [params]"
            )
        # The read guard, so a target can no more name `../x`, a symlink or `.git/...`
        # than `read_file` can; and it must exist, so the name is a file we have seen.
        resolved = confine(self._ctx.checkout, path_part, write=False)
        if not os.path.lexists(resolved):
            raise ToolError(f"target {shown(target)!r}: {shown(path_part)!r} does not exist in the repository")
        normalised = relative_posix(self._ctx.checkout, resolved) + separator + node
        if not _FIRST_CHAR.match(normalised):
            raise ToolError(f"target {shown(target)!r} names {shown(normalised)!r}, which pytest would not read as a plain path")
        return normalised

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        """Run the probe and render what the sandbox returned.

        **The invariant that makes this rendering safe: the failed ids, the
        collection errors and the output tail are printed verbatim, and that is
        only acceptable because a probe never includes the hidden benchmark
        overlay.** `Verifier.run_subset` runs the targets against the exported tree
        without the overlay, so nothing in its `SuiteResult` can name a hidden test.
        Everything shown here is built from that one result and from nothing else:
        not the baseline, not a scored attempt's `AttemptRecord.result`, both of
        which are oracle-bearing and are only ever shown through the feedback filter.
        Do not add them to this output, and do not add filtering here -- filtering
        lives in `feedback.py`, and a second copy would drift from it.

        The pipeline must also pass a baseline-aware `ToolContext.is_protected`; the
        context's default knows only the path heuristic.
        """
        ctx = self._ctx
        targets: Sequence[str] = [self._normalise_target(target) for target in args["targets"]]
        if ctx.run_subset is None:
            raise _unavailable("this run has no sandbox, so tests cannot be executed")

        await _checkpoint(ctx, "checkpoint: before test probe")
        try:
            result = await ctx.run_subset(targets)
        except VerifierNotReady:
            raise _unavailable("the test environment has not been prepared") from None
        if result.error:
            raise ToolError(
                _UNAVAILABLE.format(why=shown(result.error, MAX_ERROR_CHARS))
                + " (if the run timed out, run fewer or narrower targets)"
            )

        cap = ctx.limits.max_output_chars
        summary = (
            f"{len(result.passed)} passed, {len(result.failed)} failed, {len(result.skipped)} skipped, "
            f"{len(result.xfailed)} xfailed, {len(result.did_not_run)} did not run, "
            f"{len(result.collect_failures)} collection error(s)"
        )
        if result.exit_code is not None:
            summary += f" (pytest exit code {result.exit_code})"
        head = [summary]
        head += _id_section("failed", result.failed, cap // 6)
        head += _id_section("collection errors", result.collect_failures, cap // 6)
        head.append("--- end of pytest output ---")
        text = "\n".join(head) + "\n"
        tail = _tail(result.stdout_tail, max(0, min(TEST_TAIL_CHARS, cap - len(text))))
        return ToolOutcome(text + tail)


def _id_section(label: str, ids: Sequence[str], budget: int) -> list[str]:
    """Up to `MAX_LISTED_IDS` ids, each clipped, stopping early at `budget` characters."""
    if not ids:
        return []
    lines = [f"{label} ({len(ids)}):"]
    used = len(lines[0])
    listed = 0
    for test_id in ids[:MAX_LISTED_IDS]:
        line = f"  {_tail(test_id, MAX_ID_CHARS)}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line) + 1
        listed += 1
    if listed < len(ids):
        lines.append(f"  ... and {len(ids) - listed} more")
    return lines
