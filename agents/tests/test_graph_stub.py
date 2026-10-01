"""`run_agent` is still a stub. Stream F deletes this file when it lands the graph.

Its own file so stream F owns it outright: this assertion used to sit beside
stream B's in one class, and two parallel streams editing adjacent lines of one
file is a merge conflict by construction. It goes through `expect_stub`, so an
implementation turns it into a skip rather than a red test even if nobody deletes it.
"""

import pytest

from repolace_agents.run import run_agent

from agents_support import expect_stub, make_deps

pytestmark = pytest.mark.anyio


async def test_run_agent_lands_in_stream_f():
    with expect_stub("stream F: graph"):
        await run_agent(make_deps())
