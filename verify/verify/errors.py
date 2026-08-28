"""Sandbox failures.

Every message is redacted and capped at construction rather than at the point
it reaches a log line. `tasks.error_message` ends up in a UI and, in Phase 3, in
Loki, and a build log is exactly the kind of output that quotes environment
contents back at you.
"""

from collections.abc import Sequence

from repolace_shared.git import redact

#: How much of a build or run log survives into the message. A container log can
#: be megabytes; the useful part is almost always the tail.
LOG_TAIL_CHARS = 500


def _tail(text: str, limit: int = LOG_TAIL_CHARS) -> str:
    stripped = text.strip()
    return stripped if len(stripped) <= limit else f"...{stripped[-limit:]}"


class SandboxError(RuntimeError):
    """Base class, so `_describe` in the pipeline can catch the family with one arm."""

    def __init__(self, message: str) -> None:
        super().__init__(redact(message))


class SandboxUnavailable(SandboxError):
    """The container runtime is missing or not answering. Not the repo's fault."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"container runtime unavailable: {_tail(detail)}")


class EnvironmentBuildFailed(SandboxError):
    """Installing the repo's dependencies failed.

    Expected to be the most common failure on a new repo, and the reason
    CLAUDE.md says dependency installation is the part that will cost the time.
    """

    def __init__(self, repo: str, exit_code: int, log: str) -> None:
        self.repo = repo
        self.exit_code = exit_code
        super().__init__(f"environment build failed for {repo} (exit {exit_code}): {_tail(log)}")


class SandboxTimeout(SandboxError):
    def __init__(self, seconds: float, command: Sequence[str]) -> None:
        self.seconds = seconds
        super().__init__(f"sandbox exceeded {seconds}s running {' '.join(command[:3])}...")


class SpecError(SandboxError):
    """A per-repo spec file is malformed.

    Raised rather than ignored: a spec whose key was silently dropped stops
    applying, and the symptom is an unexplained unscoreable run weeks later.
    """
