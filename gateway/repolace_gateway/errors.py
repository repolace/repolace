"""The gateway's failure vocabulary.

One base class so a caller can catch the family, and a distinct type per failure
the agent loop must tell apart: a call that ran out of budget is "finalize with
what you have", a call that could not be priced is "stop, the measurement is
broken", and a call that failed at the provider is neither.
"""


class GatewayError(RuntimeError):
    """Base class for gateway failures."""


class ConfigError(GatewayError, ValueError):
    """`models.toml` or the settings are wrong. Raised at load time, never mid-run."""


class NoTaskScope(GatewayError):
    """A model call was attempted outside `task_scope`.

    A hard error rather than a silent unattributed call: spend that belongs to
    no task is spend `tasks.cost_usd` can never include, which is the
    measurement hole the gateway exists to close.
    """


class MissingProviderKey(GatewayError):
    """The routed model's provider has no API key configured."""


class UnpricedModelError(GatewayError):
    """A call could not be given a cost. Never silently priced at zero.

    An unpriced call that counted as free would under-report every task that
    used it, and the benchmark's headline includes cost.
    """


class LLMCallError(GatewayError):
    """The provider call failed for good: retries exhausted, or not retryable.

    The message is redacted before it is built -- provider exceptions routinely
    echo parts of the request.
    """

    def __init__(self, message: str, *, stage: str, model: str, retries: int) -> None:
        super().__init__(message)
        self.stage = stage
        self.model = model
        self.retries = retries
