"""The agent's tools. Import from here or from `repolace_agents.tools.base`.

`build_toolbox` is the one exception: import it from here. The copy in `base`
is the frozen Wave 0 stub; the implementation lives in `toolbox.py`, which this
package re-exports.

Nothing in this package imports `litellm`, `repolace_gateway.client` (which
pulls `litellm` and FastAPI), `rag`/`retrieval`, `torch` or
`sentence_transformers`: agent tests must stay cheap, and `rag` would pull torch
into them. The lighter gateway modules (`repolace_gateway.budget`,
`.errors`) are fine. SQLAlchemy and pgvector *are* loaded, through
`verify.scoring`'s import of `repolace_shared.db.models`.
"""

from repolace_agents.tools.base import (
    Tool,
    ToolBox,
    ToolContext,
    ToolError,
    ToolLimits,
    ToolOutcome,
    ToolSpec,
)
from repolace_agents.tools.toolbox import build_toolbox

__all__ = [
    "Tool",
    "ToolBox",
    "ToolContext",
    "ToolError",
    "ToolLimits",
    "ToolOutcome",
    "ToolSpec",
    "build_toolbox",
]
