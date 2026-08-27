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
    """The row exists but is not `queued`, so this runner must not touch it.

    Not an error in the task itself -- it is what a second concurrent runner,
    or a re-run of a finished task, is supposed to hit.
    """

    def __init__(self, task_id, status: str) -> None:
        self.task_id = task_id
        self.status = status
        super().__init__(f"task {task_id} is {status}, not queued")


class StageFailed(PipelineError):
    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        self.message = message
        super().__init__(f"{stage}: {message}")
