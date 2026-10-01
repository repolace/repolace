"""Whether a task actually fixed the issue.

This module is the benchmark. A rule that is subtly wrong produces a number that
is confidently wrong, which is worse than no number at all, so two things are
deliberate throughout:

**It errs toward FAILED.** A false negative costs one point. A false positive
costs the claim -- anyone can open a PASSED task's pull request and read the
diff, and one indefensible pass discredits the whole figure.

**It reports `None` (inadmissible) rather than a verdict when the instrument
failed.** "We never found out" is not "the agent lost", and folding the two
together lets infrastructure flakiness masquerade as capability.

What this module cannot do, stated plainly so nobody assumes otherwise: it
cannot tell a real fix from a patch that satisfies the test from the source side
-- special-casing the input, returning the literal the assertion wants. No
path-based rule can. That needs curated per-instance ground truth
(`expected_fail_to_pass`) and a human reading the diff. `collection_fixed` adds
one more instance of the same limit: an agent can make a module import by
swallowing the failing import rather than fixing it, and collect credit for the
now-trivially-passing tests. That is a source-side edit, so no path rule sees it.
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from repolace_shared.db.models import TaskOutcome
from verify.protocol import SuiteResult

#: Directory names that mean "this is test infrastructure".
_TEST_DIR_PARTS = frozenset({"test", "tests", "testing", "unit_tests", "regression_tests", "spec", "qa"})

#: Directories whose *contents* are test fixtures whatever the extension.
#: Editing a golden file or a recorded cassette is the cheapest possible fake
#: fix, and none of these match a `test_*.py` heuristic.
_FIXTURE_DIR_PARTS = frozenset({"__snapshots__", "cassettes", "testdata", "snapshots", "fixtures"})

#: Files that change what runs, or whether it passes, without being tests.
#: `-o addopts=` clears only `addopts`; everything else in a pytest config still
#: applies, so relaxing `filterwarnings` here turns a real failure into a real
#: pass with a diff that touches no test path.
_CONFIG_FILES = frozenset({
    "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", ".coveragerc",
    "conftest.py",
    # Imported by the interpreter before pytest exists, whenever the repo root
    # is on sys.path -- which a legacy-mode editable install arranges.
    "sitecustomize.py", "usercustomize.py",
    # Can change the bytes checked out without changing the blob, so what runs
    # stops matching what the diff shows.
    ".gitattributes",
})


def is_test_path(path: str) -> bool:
    """Heuristic: does this path look like test infrastructure?

    Used as one input to `disqualifying_paths`, never alone. It necessarily both
    over- and under-matches: `django/test/client.py` is a shipped module, not a
    test, and `myapp/tests.py` is a test that matches no `test_*.py` pattern.
    The collected-file set from the baseline run resolves both.
    """
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    name = parts[-1]
    if name in _CONFIG_FILES:
        return True
    if any(part in _TEST_DIR_PARTS or part in _FIXTURE_DIR_PARTS for part in parts[:-1]):
        return True
    if not name.endswith(".py"):
        return False
    stem = name[:-3]
    return name == "tests.py" or stem.startswith("test_") or stem.endswith("_test")


def is_protected_path(path: str) -> bool:
    """Is the agent's edit tool forbidden from writing this path?

    Exactly the paths `disqualifying_paths` would fail the task for editing, as
    far as a path alone can say: a test path, or a config file that changes what
    runs or whether it passes. The edit tool refuses them up front, as a
    model-visible error, so the agent learns the rule on its first attempt
    instead of finishing a patch the scorer then throws away.

    Built from this module's own constants rather than a copy, so the tool and
    the scorer cannot drift: a path the tool allows and the scorer disqualifies
    is a wasted run, and one the tool refuses and the scorer allows is a fix the
    agent could not make.

    **Stricter than the scorer on purpose, and the one place it differs.**
    `disqualifying_paths` spares a shipped module like `django/test/client.py`
    because it can consult the baseline's collected-file set and
    `baseline_files`. At edit time there is no such evidence to hand, so this
    follows `is_test_path` alone and refuses it. That is the cautious direction
    -- a refused edit costs a model-visible error, a permitted one that the
    scorer later disqualifies costs the task -- but it does mean an issue whose
    real fix lives in such a module cannot be fixed by the agent.

    Takes a canonical repo-relative path; the caller is expected to have already
    confined it with `repolace_shared.paths.resolve_within`. Normalised first,
    because the `./` prefix is exactly the seam `_normalise` was written to
    close: `./.gitattributes` must not escape the set.
    """
    normalised = _normalise(path)
    # `is_test_path` already matches `_CONFIG_FILES` today. The second clause is
    # not redundancy to tidy away: `is_test_path` is documented as a heuristic
    # that gets tuned, and narrowing it must never quietly unprotect
    # `conftest.py` or `pyproject.toml` -- the basename check is the floor.
    return is_test_path(normalised) or PurePosixPath(normalised).name in _CONFIG_FILES


def _normalise(path: str) -> str:
    """Strip a leading `./` without eating the leading dot of a dotfile.

    `lstrip("./")` strips *characters*, not a prefix: it turned
    `.gitattributes` into `gitattributes` and `.coveragerc` into `coveragerc`,
    so neither matched `_CONFIG_FILES` and both were freely editable at the repo
    root -- while the same files one directory down were caught. Those are the
    only two dotted entries in that set, and they are the two an agent most
    wants: one changes the bytes checked out, the other turns off coverage
    gates.

    The existing unit test asserted `is_test_path(".gitattributes") is True` and
    passed throughout, because `is_test_path` does not normalise. The bug lived
    entirely in the seam between the two functions.

    Nothing upstream is known to emit a `./` prefix -- neither
    `git diff --name-only` nor pytest's `item.location[0]` does -- so this is
    defensive. The mangling was not: it applied to every path unconditionally.
    Note it does not resolve `..` either, so `src/../tests/test_x.py` matches
    nothing and falls through to the heuristic; git does not emit that shape.
    """
    return path.removeprefix("./")


def _in_a_collected_test_tree(path: str, collected_dirs: set[PurePosixPath]) -> bool:
    """Is this file under a *test-named* directory pytest collected tests from?

    This is what narrows the existence exemption below. Two boundaries, and both
    are load-bearing in the direction of not failing honest work:

    * **The directory must be test-named.** `myapp/models.py` sits beside a
      collected `myapp/tests.py` in every Django project ever written, and
      disqualifying it would fail every honest fix in such a repo.
    * **pytest must actually have collected from it, or from below it.**
      `django/test` is test-named and pytest collects nothing there -- that is
      precisely the case the exemption exists for.

    What is left is what the exemption was wrongly covering: `tests/helpers.py`,
    `tests/__snapshots__/render.ambr`, `tests/cassettes/api.yaml`. All are test
    infrastructure that defines no test of its own, so pytest never lists them;
    all existed at baseline, because a fixture by definition does; and editing a
    golden file or a recorded cassette is the cheapest possible fake fix.
    """
    for ancestor in PurePosixPath(path).parents:
        if ancestor == PurePosixPath("."):
            continue
        if ancestor.name not in _TEST_DIR_PARTS and ancestor.name not in _FIXTURE_DIR_PARTS:
            continue
        if any(d == ancestor or ancestor in d.parents for d in collected_dirs):
            return True
    return False


def disqualifying_paths(
    changed_files: list[str] | tuple[str, ...],
    baseline: SuiteResult,
    baseline_files: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Which changed files are off-limits.

    Combines what pytest actually collected -- authoritative for this repo -- with
    a path heuristic that is still needed for test files the baseline never saw,
    since a file the agent *adds* cannot appear in the collected set.

    ``baseline_files`` is what resolves the heuristic's false positives. Django
    ships ``django/test/client.py``; the heuristic flags it, and disqualifying a
    fix that legitimately lands there would fail honest work for reasons
    unrelated to the agent. The discriminator is existence: a file that was
    present at the base commit and that pytest did *not* collect from is not a
    test, whatever it is called. A file absent at baseline is new, so the
    heuristic applies.

    Config files are disqualified regardless -- ``pyproject.toml`` exists at
    baseline and is never collected, but relaxing ``filterwarnings`` in it turns
    a real failure into a real pass.
    """
    collected = {_normalise(p) for p in baseline.collected_files}
    conftests = {PurePosixPath(p).name for p in baseline.conftests}
    fixture_dirs = {str(PurePosixPath(p).parent) for p in collected}
    existed = {_normalise(p) for p in baseline_files} if baseline_files is not None else None
    # `.` is dropped defensively. It cannot over-match as the rule is written --
    # the ancestor loop skips `.`, and a non-`.` ancestor never matches it -- but
    # a repository whose tests sit at the top level would put the repo root here,
    # and the root is an ancestor of everything. Any future loosening of the
    # match would then disqualify every source file in the project. The cost of
    # dropping it is that such a repo gets no tree rule at all, only the
    # heuristic and the collected-file set: a real gap, acknowledged not fixed.
    collected_dirs = {
        parent for parent in (PurePosixPath(p).parent for p in collected)
        if parent != PurePosixPath(".")
    }

    disqualified = []
    for path in changed_files:
        normalised = _normalise(path)
        name = PurePosixPath(normalised).name

        if normalised in collected:
            disqualified.append(path)
        elif normalised.endswith("conftest.py") and name in conftests:
            disqualified.append(path)
        elif name in _CONFIG_FILES:
            disqualified.append(path)
        elif str(PurePosixPath(normalised).parent) in fixture_dirs and not normalised.endswith(".py"):
            # A data file beside collected tests: a golden file or a recorded
            # cassette, which is a test in everything but extension.
            disqualified.append(path)
        elif is_test_path(normalised):
            # Only when we cannot prove otherwise. A file that existed at
            # baseline and that pytest ignored is usually not test
            # infrastructure -- unless it sits under a test-named directory
            # pytest *did* collect from, which is what a helper module, a
            # snapshot and a cassette all look like. Those existed at baseline
            # and are never collected, so existence alone exonerated exactly
            # the files this rule most needs to catch.
            if (
                existed is not None
                and normalised in existed
                and not _in_a_collected_test_tree(normalised, collected_dirs)
            ):
                continue
            disqualified.append(path)
    return tuple(sorted(set(disqualified)))


def _candidate_fail_to_pass(baseline: SuiteResult) -> set[str]:
    """The only nodes that could possibly count as fail-to-pass.

    Shared with the admissibility gate deliberately. If the gate ever admitted
    an instance whose baseline holds nothing this set can match, the task would
    be scored FAILED for an instrument limitation -- which is the exact defect
    the gate exists to prevent, reintroduced by the two drifting apart.
    """
    return set(baseline.failed) | set(baseline.xfailed)


def _collect_failure_prefixes(collect_failures: tuple[str, ...]) -> tuple[str, ...]:
    """Node-id prefixes for modules that would not import at baseline.

    `::` is part of the prefix on purpose. Without it `tests/test_api.py` also
    prefix-matches `tests/test_api_v2.py::test_x`, crediting a sibling module
    the agent never touched.

    Only file-shaped ids are handled. pytest also emits a collect failure for a
    directory or a package, whose id contains no `.py` and whose tests are
    `<dir>/<file>.py::<name>` rather than `<dir>::<name>` -- so a prefix rule
    would either miss them or, matched on `<dir>/`, credit every test in the
    subtree for one module starting to import. Missing them is the safer half,
    and it is the direction this module errs in everywhere else.
    """
    return tuple(f"{nodeid}::" for nodeid in collect_failures if nodeid.endswith(".py"))


def collection_fixed(baseline: SuiteResult, attempt: SuiteResult) -> tuple[str, ...]:
    """Tests that now pass in a module that would not import at baseline.

    A module that fails to collect is as red as a failing test and arguably
    redder: every test in it was lost, and none of them appears in
    `baseline.failed`, because none of them ran. "The module raises ImportError
    at import time" is also one of the commonest shapes a real GitHub issue
    takes. Without this the agent can genuinely repair one and score nothing --
    the instance is unscoreable for a reason that has nothing to do with it.

    Known limits, none of which can produce a false pass:

    * A module fixed by *moving* it lands under a different prefix and is
      uncredited. Moving a test file is very likely disqualified anyway.
    * Every test in a recovered module counts, not only the one targeting the
      issue. That is the same weakness the uncurated path already carries, and
      it does not arise when `expected_fail_to_pass` is supplied.
    * A baseline collect failure that *persists* stays invisible: not
      neutralized (it was already silent at baseline) and not a regression.
      Correct, but worth saying.
    """
    prefixes = _collect_failure_prefixes(baseline.collect_failures)
    if not prefixes:
        return ()
    return tuple(sorted(n for n in attempt.passed if n.startswith(prefixes)))


def fail_to_pass(baseline: SuiteResult, attempt: SuiteResult) -> tuple[str, ...]:
    """Tests that were failing and now pass.

    `xfailed`, not `skipped`. A mature repository records a known bug as
    `@pytest.mark.xfail`, and that going green is the likeliest honest form of a
    legitimately-red test, so it has to count. But pytest reports an *ordinary*
    skip the same way, and a real suite is full of them -- `importorskip` for an
    optional dependency, a platform guard, a marker gate. Joining on `skipped`
    meant a baseline skip that started passing for any reason at all (a
    dependency appearing in the image, an install-step change) scored the task
    PASSED with **nothing red at baseline**. That is a false positive, and by
    the reasoning at the top of this module a false positive costs the claim.
    """
    became_passing = _candidate_fail_to_pass(baseline) & set(attempt.passed)
    return tuple(sorted(became_passing | set(collection_fixed(baseline, attempt))))


def regressions(baseline: SuiteResult, attempt: SuiteResult) -> tuple[str, ...]:
    """Tests that were passing and no longer are.

    A set difference against `passed`, not a lookup in `failed`. That is what
    makes a *deleted* test, a test turned into a skip, and a test whose module
    stopped collecting all count -- none of which appear in `attempt.failed`,
    and all of which are ways to make an inconvenient test stop objecting.
    """
    return tuple(sorted(set(baseline.passed) - set(attempt.passed)))


def neutralized(baseline: SuiteResult, attempt: SuiteResult) -> tuple[str, ...]:
    """Baseline-failing tests that were silenced rather than fixed.

    Without this the incentive is plain: turn one red test green by any means,
    and make everything else you broke stop running. A skipped or uncollected
    test is in neither `passed` nor `failed`, so it costs nothing under the
    other two rules. Only previously-*passing* tests were protected.

    `xfailed` counts as silenced alongside `skipped`, and must: marking a
    baseline failure `@pytest.mark.xfail` is the cheapest possible way to make
    it stop objecting, and separating the two buckets for `fail_to_pass` would
    otherwise have opened a wider hole here than it closed there.
    """
    silenced = set(attempt.skipped) | set(attempt.xfailed) | set(attempt.did_not_run)
    observed = set(attempt.passed) | set(attempt.failed) | silenced
    vanished = set(baseline.failed) - observed
    return tuple(sorted((set(baseline.failed) & silenced) | vanished))


def fingerprint_changed(baseline: SuiteResult, attempt: SuiteResult) -> str | None:
    """Did the run's configuration shift underneath the comparison?

    node ids are relative to rootdir, ini options decide whether a warning is an
    error, and a conftest-registered plugin can reorder the suite. If any of
    those differ between baseline and attempt, the two result sets are not
    comparable and the diff is not the only thing that changed.
    """
    for key in ("rootdir", "ini", "plugins"):
        before, after = baseline.fingerprint.get(key), attempt.fingerprint.get(key)
        if before != after:
            return f"{key} changed between baseline and attempt"
    return None


@dataclass(frozen=True)
class Score:
    outcome: TaskOutcome | None
    reason: str
    fail_to_pass: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    neutralized: tuple[str, ...] = ()
    disqualified: tuple[str, ...] = ()
    #: True when the instrument failed rather than the agent. Excluded from the
    #: headline figure, and reported as its own count so exclusions stay visible.
    inadmissible: bool = False


def _inadmissible(reason: str, **fields) -> Score:
    return Score(outcome=None, reason=reason, inadmissible=True, **fields)


def score(
    baseline: SuiteResult,
    attempt: SuiteResult,
    changed_files: list[str] | tuple[str, ...],
    *,
    baseline_files: tuple[str, ...] | None = None,
    expected_fail_to_pass: tuple[str, ...] | None = None,
    attempt_infrastructure_error: bool = False,
) -> Score:
    """Decide one attempt's outcome. First match wins; order is load-bearing.

    `expected_fail_to_pass` is the curated per-instance ground truth -- the tests
    the fixing PR added. When present, all of them must pass; "some baseline
    failure went green" is not evidence about *this* issue. When absent the rule
    degrades to that weaker claim, which is why an uncurated figure overstates.
    """
    disqualified = disqualifying_paths(changed_files, baseline, baseline_files)
    if disqualified:
        # First, deliberately. A disqualifying diff is a property of the diff and
        # needs no run at all -- so crashing the harness cannot launder a test
        # edit into an exclusion, which is what the old ordering allowed.
        return Score(
            outcome=TaskOutcome.FAILED,
            reason=f"diff touches test or config files: {', '.join(disqualified[:3])}",
            disqualified=disqualified,
        )

    if baseline.error:
        return _inadmissible(f"baseline unscoreable: {baseline.error}")

    if attempt_infrastructure_error:
        # The host or the daemon failed. Not a property of the patch, so not a
        # verdict about the agent -- this is a retry signal.
        return _inadmissible(f"infrastructure failure: {attempt.error}")

    if attempt.error:
        # The environment is identical across attempts because the image is
        # built once, so a suite that now fails to collect, times out, or is
        # OOM-killed is the patch's doing. That is a bad patch, not an excuse.
        return Score(outcome=TaskOutcome.FAILED, reason=f"attempt unscoreable: {attempt.error}")

    drift = fingerprint_changed(baseline, attempt)
    if drift:
        return Score(outcome=TaskOutcome.FAILED, reason=drift)

    if (
        expected_fail_to_pass is None
        and not _candidate_fail_to_pass(baseline)
        and not _collect_failure_prefixes(baseline.collect_failures)
    ):
        # Nothing that *could* go green was red at the base commit, so
        # `F0 ∩ Pn` is empty whatever the agent does. Charging that to the agent
        # would attribute an instrument limitation to it; the instance simply
        # cannot be scored this way.
        #
        # The condition used to read `not baseline.failed and not
        # baseline.skipped`, which looks equivalent and is not: once xfails
        # moved out of `skipped`, an *ordinary* skip -- a platform guard, an
        # `importorskip`, which essentially every real suite has at least one of
        # -- made an unscoreable instance look scoreable. It then fell through
        # to "no baseline-failing test now passes" and scored FAILED. Same
        # expression as `fail_to_pass` joins on, via one function, so the two
        # cannot drift apart again.
        return _inadmissible(
            "nothing was failing, xfailed or uncollectable at the base commit and no curated "
            "fail-to-pass list was supplied; this instance cannot demonstrate a fix"
        )

    fixed = fail_to_pass(baseline, attempt)
    broke = regressions(baseline, attempt)
    silenced = neutralized(baseline, attempt)
    sets = {"fail_to_pass": fixed, "regressions": broke, "neutralized": silenced}

    if silenced:
        return Score(
            outcome=TaskOutcome.FAILED,
            reason=f"{len(silenced)} baseline failure(s) silenced rather than fixed: "
                   f"{', '.join(silenced[:3])}",
            **sets,
        )

    if broke:
        return Score(
            outcome=TaskOutcome.FAILED,
            reason=f"{len(broke)} pass-to-pass regression(s): {', '.join(broke[:3])}",
            **sets,
        )

    if expected_fail_to_pass is not None:
        missing = tuple(sorted(set(expected_fail_to_pass) - set(attempt.passed)))
        if missing:
            return Score(
                outcome=TaskOutcome.FAILED,
                reason=f"{len(missing)} expected fail-to-pass test(s) still not passing: "
                       f"{', '.join(missing[:3])}",
                **sets,
            )
        return Score(outcome=TaskOutcome.PASSED,
                     reason=f"all {len(expected_fail_to_pass)} expected tests pass, no regressions",
                     **sets)

    if not fixed:
        return Score(outcome=TaskOutcome.FAILED,
                     reason="no baseline-failing test now passes", **sets)

    return Score(
        outcome=TaskOutcome.PASSED,
        reason=f"{len(fixed)} fail-to-pass, no regressions "
               f"(uncurated: not evidence this issue specifically was fixed)",
        **sets,
    )


@dataclass(frozen=True)
class Verdict:
    """Whether a patch did harm -- the PR gate outside the benchmark.

    Not an outcome and not a score. `ok` means *no evidence of harm was found*,
    which is a weaker claim than `Score.PASSED` and must be worded as one
    wherever it reaches a human (the PR body says what was not verified).
    """

    ok: bool
    reason: str
    #: Passing at baseline, not passing now -- a deleted test, a test turned into
    #: a skip and a module that stopped collecting all count, as in `regressions`.
    regressions: tuple[str, ...] = ()
    #: Baseline failures silenced rather than fixed, as in `neutralized`.
    neutralized: tuple[str, ...] = ()
    #: Modules that collected at baseline and do not now. A module that stops
    #: importing loses every test in it without any of them ever being recorded
    #: as failed, which is why this is its own set rather than a side effect of
    #: `regressions`.
    new_collect_failures: tuple[str, ...] = ()
    disqualified: tuple[str, ...] = ()


def agent_verdict(
    baseline: SuiteResult,
    attempt: SuiteResult,
    changed_files: list[str] | tuple[str, ...],
    *,
    baseline_files: tuple[str, ...] | None = None,
    attempt_infrastructure_error: bool = False,
) -> Verdict:
    """Decide whether to open a PR for a task that has no ground truth.

    `score()` cannot gate this. It is inadmissible whenever nothing was red at
    the base commit -- which is the *normal* case for a live issue, where the
    suite passes and the bug is simply not covered by it -- so under `score()`
    a real task would never open a PR. This asks the question that can be
    answered without a failing test: did the patch break anything, or make
    something stop objecting?

    Built from the same primitives as `score()` -- `disqualifying_paths`,
    `regressions`, `neutralized`, `fingerprint_changed` -- and **its precondition
    checks must be shared with `score()`**, factored out of it rather than
    copied (a refactor that leaves `test_scoring.py` passing untouched). Two
    copies of "was the baseline usable, did the environment drift" are two
    chances for the PR gate and the benchmark to disagree about what a run
    means.

    Errs toward `ok=False`, as `score()` errs toward FAILED, with one difference
    in posture: where `score()` reports an unusable baseline as *inadmissible*
    (excluded from the headline), a PR gate has no such bucket. An unusable
    baseline, an unscoreable attempt, an infrastructure error and a fingerprint
    drift all mean harm was not ruled out, so all are `ok=False` with the reason
    saying which.
    """
    raise NotImplementedError("agent_verdict lands in stream A: sandbox")
