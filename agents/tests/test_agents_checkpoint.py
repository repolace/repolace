"""Checkpoints out of the graph, and a run continued from one.

The writer is called at the points the design names (the top of every agent step, and after a verify that
sends the agent back), carries nothing the model was not shown, and never fails the run. A resumed run
keeps the snapshot's transcript and counters and reaches the same result as one that was never interrupted.
"""

import copy
import json
from dataclasses import fields

import pytest
from agents_support import ScriptedLLM, ScriptedVerifier, attempt_record, submit_reply, suite
from repolace_agents.checkpoint import (
    FIELDS,
    VERSION,
    CheckpointError,
    snapshot_from_json,
    snapshot_to_json,
)
from repolace_agents.contracts import AgentSnapshot, ScoredMark, StopReason
from repolace_agents.graph import run_graph
from repolace_agents.prompts import elided
from repolace_agents.run import run_agent
from test_graph import LIMITS, NONCE, A, B, clean, deps_for, edit, read, red

pytestmark = pytest.mark.anyio


class Collector:
    def __init__(self) -> None:
        self.snapshots: list[AgentSnapshot] = []

    async def __call__(self, snapshot: AgentSnapshot) -> None:
        self.snapshots.append(snapshot)


async def run_with(collector, llm, verifier, **overrides):
    return await run_graph(deps_for(llm, verifier, checkpoint=collector, **overrides), nonce=NONCE)


class TestWhatIsWritten:
    async def test_a_snapshot_is_taken_before_every_agent_step(self):
        collector = Collector()
        llm = ScriptedLLM([read(), edit(), submit_reply()])

        await run_with(collector, llm, ScriptedVerifier([clean(1)]))

        assert [(s.steps, s.attempt_steps, s.attempt) for s in collector.snapshots] == [(0, 0, 0), (1, 1, 0), (2, 2, 0)]
        assert {s.nonce for s in collector.snapshots} == {NONCE}
        assert all(s.last_scored is None for s in collector.snapshots)

    async def test_the_snapshot_holds_a_valid_transcript_up_to_the_last_complete_step(self):
        collector = Collector()
        llm = ScriptedLLM([read(), edit(), submit_reply()])

        await run_with(collector, llm, ScriptedVerifier([clean(1)]))

        roles = [m["role"] for m in collector.snapshots[2].messages]
        assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]

    async def test_a_retry_is_snapshotted_with_its_feedback_and_the_attempt_that_was_just_scored(self):
        collector = Collector()
        llm = ScriptedLLM([submit_reply("first"), submit_reply("second")])

        await run_with(collector, llm, ScriptedVerifier([red(1), clean(2)]))

        after_verify = [s for s in collector.snapshots if s.last_scored is not None and s.attempt == 1]
        assert after_verify, "no snapshot was taken after the first attempt was scored"
        snap = after_verify[0]
        assert snap.last_scored == ScoredMark(1, red(1).commit_sha, False)
        assert snap.feedback_clean is False
        assert snap.attempt_steps == 0
        assert snap.messages[-1]["role"] == "user" and "<feedback" in snap.messages[-1]["content"]

    async def test_nothing_the_oracle_holds_reaches_a_snapshot(self):
        secret = "tests/hidden_test.py::test_the_secret_one"
        hidden = frozenset({"tests/hidden_test.py"})
        baseline = suite(passed=[A, B], failed=[secret], collected_files=["tests/hidden_test.py"])
        attempt_one = attempt_record(1, suite(passed=[A], failed=[secret], collect_failures=["tests/hidden_test.py"]))
        collector = Collector()
        llm = ScriptedLLM([submit_reply("first"), submit_reply("second")])

        await run_with(
            collector, llm, ScriptedVerifier([attempt_one, clean(2)]), baseline=baseline, hidden_paths=hidden
        )

        assert collector.snapshots
        dumped = json.dumps([snapshot_to_json(s) for s in collector.snapshots])
        assert "test_the_secret_one" not in dumped
        assert "hidden_test" not in dumped

    def test_the_json_form_lists_every_field_of_the_snapshot_and_no_other(self):
        assert set(FIELDS) == {f.name for f in fields(AgentSnapshot)}
        assert set(snapshot_to_json(_example())) == {"version", *FIELDS}

    async def test_a_writer_that_raises_costs_the_checkpoint_and_not_the_run(self):
        async def broken(snapshot):
            raise RuntimeError("database went away")

        llm = ScriptedLLM([submit_reply()])

        result = await run_graph(deps_for(llm, ScriptedVerifier([clean(1)]), checkpoint=broken), nonce=NONCE)

        assert result.stop_reason is StopReason.SUBMITTED


class TestResuming:
    async def test_a_resumed_run_reaches_the_result_of_one_that_was_never_interrupted(self):
        whole = await run_graph(
            deps_for(ScriptedLLM([read(), edit(), submit_reply()]), ScriptedVerifier([clean(1)])), nonce=NONCE
        )
        collector = Collector()
        await run_with(collector, ScriptedLLM([read(), edit(), submit_reply()]), ScriptedVerifier([clean(1)]))
        snapshot = collector.snapshots[2]  # two steps done, the third not begun

        llm = ScriptedLLM([submit_reply()])
        verifier = ScriptedVerifier([clean(1)])
        # `run_agent`, the production entry: it takes no nonce, and for a resume it must reuse the snapshot's.
        resumed = await run_agent(deps_for(llm, verifier, resume_from=snapshot))

        assert (resumed.stop_reason, resumed.attempts, resumed.steps) == (whole.stop_reason, whole.attempts, whole.steps)
        assert verifier.calls == [1]

    async def test_the_first_request_after_a_resume_carries_the_snapshots_transcript_and_no_new_opening(self):
        collector = Collector()
        await run_with(collector, ScriptedLLM([read(), submit_reply()]), ScriptedVerifier([clean(1)]))
        snapshot = collector.snapshots[1]
        llm = ScriptedLLM([submit_reply()])

        await run_agent(deps_for(llm, ScriptedVerifier([clean(1)]), resume_from=snapshot))

        sent = llm.calls[0].messages
        assert sent == [dict(m) for m in snapshot.messages]
        assert NONCE in sent[0]["content"]  # the nonce the transcript was written with is the one still in force
        assert [m["role"] for m in sent].count("system") == 1  # localize did not run again

    async def test_a_resume_in_a_later_attempt_continues_it_and_does_not_elide_its_own_results(self):
        collector = Collector()
        llm = ScriptedLLM([submit_reply("first"), read(), submit_reply("second")])
        await run_with(collector, llm, ScriptedVerifier([red(1), clean(2)]))
        mid_second = next(s for s in collector.snapshots if s.attempt == 1 and s.attempt_steps == 1)
        own_result = next(m for m in mid_second.messages if m["role"] == "tool" and "read_file" in m["content"])
        assert own_result["content"] != elided(1)

        llm = ScriptedLLM([submit_reply("second")])
        verifier = ScriptedVerifier([clean(2)])
        resumed = await run_graph(
            deps_for(llm, verifier, resume_from=mid_second, resume_last_attempt=red(1)), nonce=NONCE
        )

        sent = llm.calls[0].messages
        assert any(m["role"] == "tool" and m["content"] == own_result["content"] for m in sent)
        assert verifier.calls == [2]
        assert resumed.attempts == 2 and resumed.last_attempt.attempt == 2

    async def test_the_step_cap_counts_the_steps_taken_before_the_stop(self):
        collector = Collector()
        steps = LIMITS.max_steps_per_attempt
        await run_with(collector, ScriptedLLM([read()] * steps), ScriptedVerifier([clean(1)]))
        near_the_cap = collector.snapshots[steps - 1]
        assert near_the_cap.attempt_steps == steps - 1
        llm = ScriptedLLM([read()])

        resumed = await run_graph(deps_for(llm, ScriptedVerifier([clean(1)]), resume_from=near_the_cap), nonce=NONCE)

        assert len(llm.calls) == 1
        assert resumed.stop_reason is StopReason.STEP_CAP

    async def test_a_snapshot_with_a_scored_attempt_needs_the_matching_record(self):
        collector = Collector()
        await run_with(collector, ScriptedLLM([submit_reply(), submit_reply()]), ScriptedVerifier([red(1), clean(2)]))
        snapshot = next(s for s in collector.snapshots if s.last_scored is not None)

        with pytest.raises(ValueError, match="wanted 1, got None"):
            await run_graph(deps_for(ScriptedLLM([]), ScriptedVerifier([]), resume_from=snapshot), nonce=NONCE)
        with pytest.raises(ValueError, match="wanted 1, got 2"):
            await run_graph(
                deps_for(ScriptedLLM([]), ScriptedVerifier([]), resume_from=snapshot, resume_last_attempt=clean(2)),
                nonce=NONCE,
            )

    async def test_a_snapshot_survives_the_json_round_trip_used_by_the_database(self):
        collector = Collector()
        await run_with(collector, ScriptedLLM([submit_reply(), submit_reply()]), ScriptedVerifier([red(1), clean(2)]))

        for snapshot in collector.snapshots:
            stored = json.loads(json.dumps(snapshot_to_json(snapshot)))
            assert snapshot_from_json(stored) == snapshot


def _example() -> AgentSnapshot:
    return AgentSnapshot(
        nonce="abc",
        messages=({"role": "system", "content": "s"}, {"role": "user", "content": "u"}),
        attempt=1,
        steps=4,
        attempt_steps=2,
        summary=None,
        scored_summary="did a thing",
        feedback_clean=False,
        last_scored=ScoredMark(1, "a" * 40, False),
    )


class TestReadingOneBack:
    def test_the_example_round_trips(self):
        assert snapshot_from_json(json.loads(json.dumps(snapshot_to_json(_example())))) == _example()

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(version=VERSION + 1),
            lambda d: d.update(attempt="1"),
            lambda d: d.update(attempt=True),
            lambda d: d.update(nonce=None),
            lambda d: d.update(messages=[{"content": "no role"}]),
            lambda d: d.update(messages="not a list"),
            lambda d: d["last_scored"].update(infrastructure_error="no"),
            lambda d: d.pop("steps"),
        ],
    )
    def test_corrupt_content_is_refused(self, mutate):
        data = copy.deepcopy(snapshot_to_json(_example()))
        mutate(data)
        with pytest.raises(CheckpointError):
            snapshot_from_json(data)

    def test_something_that_is_not_an_object_is_refused(self):
        with pytest.raises(CheckpointError):
            snapshot_from_json([1, 2])
