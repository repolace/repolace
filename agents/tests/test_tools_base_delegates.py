"""`base.build_toolbox` was the Wave 0 stub that raised; it now delegates to the real one.

Both import paths exist (`repolace_agents.tools.base` and the package), and a
consumer that picked the first would have crashed at runtime with
`NotImplementedError` rather than failing at import. Pinned so the two cannot
drift apart again.
"""

from repolace_agents.tools import build_toolbox as package_build_toolbox
from repolace_agents.tools.base import ToolBox, build_toolbox as base_build_toolbox

from tools_support import make_harness


def test_the_base_build_toolbox_builds_the_same_nine_tools_as_the_package_one(tmp_path):
    h = make_harness(tmp_path)

    from_base = base_build_toolbox(h.ctx)
    from_package = package_build_toolbox(h.ctx)

    assert isinstance(from_base, ToolBox)
    assert from_base.schemas() == from_package.schemas()
    assert len(from_base.schemas()) == 9
