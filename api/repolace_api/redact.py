"""Host paths out of task text before it is shown.

A task's `error_message` and its stages' errors are stored as the pipeline wrote them (after secret
scrubbing, not path scrubbing), so they can name the worker's filesystem: the task workspace
(`/tmp/repolace-task-…/repo`), the home directory, the virtualenv. The stored rows stay as they are,
for the operator; what the API and the UI show is passed through `scrub_host_paths`.

The rule is mechanical and errs toward hiding: every absolute path of two or more segments is
replaced. A path inside a task checkout keeps its part below `/repo/`, which is the repository's own
layout and the useful part of the message; any other path keeps only its last segment. URLs are left
alone: a `/` that follows `:` or `/` is not the start of a path.
"""

import re

#: An absolute path of at least two segments, not inside a URL or a relative path: the `/` must not
#: follow a word character, `:`, `/`, `.`, `~` or `-`.
_ABSOLUTE_PATH = re.compile(r"(?<![\w:/.~-])/(?:[\w.@+~-]+/)+[\w.@+~-]*")
#: Where a task checkout's own files start.
_CHECKOUT = "/repo/"


def _replace(match: re.Match[str]) -> str:
    path = match.group(0)
    if _CHECKOUT in path:
        return "<checkout>/" + path.split(_CHECKOUT, 1)[1]
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return f"<path>/{last}" if last else "<path>"


def scrub_host_paths(text: str | None) -> str | None:
    if not text:
        return text
    return _ABSOLUTE_PATH.sub(_replace, text)
