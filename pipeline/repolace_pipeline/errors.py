"""Pipeline failures, tagged with the stage that produced them.

Stage-prefixing every message is what makes `select error_message from tasks`
answer "where did it die" without reading logs. It is the "a failure should
have exactly one plausible cause" rule made structural rather than aspirational.
"""


class PipelineError(RuntimeError):
    """Base class, so callers can catch the family."""


class TaskNotFound(PipelineError):
    def __init__(self, task_id) -> None:
        self.task_id = task_id
        super().__init__(f"no task with id {task_id}")


class TaskNotClaimable(PipelineError):
    """The row exists but is neither `queued` nor `paused`, so this runner must not touch it.

    Not an error in the task itself -- it is what a second concurrent runner,
    or a re-run of a finished task, is supposed to hit.
    """

    def __init__(self, task_id, status: str) -> None:
        self.task_id = task_id
        self.status = status
        super().__init__(f"task {task_id} is {status}, not queued or paused")


class StageFailed(PipelineError):
    """A stage ended the task. `stop_class` says so when the failure has no exception to read it from.

    Most StageFaileds wrap an exception (`__cause__`), and the class of the stop is the cause's. A stage that
    raises this one itself -- a baseline run whose sandbox never answered, an index lock that stayed taken --
    names the class here, because there is nothing else to classify.
    """

    def __init__(self, stage: str, message: str, *, stop_class: str | None = None) -> None:
        self.stage = stage
        self.message = message
        self.stop_class = stop_class
        super().__init__(f"{stage}: {message}")


class ResumeRefused(StageFailed):
    """A resume that must not go ahead: the saved work no longer matches the world it would continue in.

    Raised before anything of the paused run is touched. The row fails cleanly with `RESUME_REFUSED_PREFIX`
    and its checkpoint kept, and the resume it cost is not counted (the claim bumped `resume_count`; the failure
    hands it back), so an operator who undoes the cause can `reopen_refused` it.
    """

    def __init__(self, message: str) -> None:
        super().__init__("resume", message, stop_class="design")
