"""`build_toolbox` is still a stub. Stream B deletes this file when it lands the tools.

Its own file so stream B owns it outright, for the reason `test_graph_stub.py`
gives. It goes through `expect_stub`, so an implementation turns it into a skip
rather than a red test even if nobody deletes it.
"""

from pathlib import Path

from repolace_agents.tools.base import ToolContext, build_toolbox

from agents_support import expect_stub


def test_build_toolbox_lands_in_stream_b():
    async def noop(*args):
        return None

    with expect_stub("stream B: toolbox"):
        build_toolbox(ToolContext(Path("."), noop, noop, None, None))
