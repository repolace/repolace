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


#: Why a provider call failed for good. `auth` and `quota` are the provider refusing *us* (a key it
#: rejects, a billing or rate limit that is used up), which says nothing about how well the agent did.
KIND_AUTH = "auth"
KIND_QUOTA = "quota"
KIND_OTHER = "other"

#: Fixed text a message starts with for those two kinds, so the eval runner can recognise a run that is
#: failing for the provider's reasons from a task's `error_message` without a new column.
AUTH_FAILURE_PREFIX = "provider auth failure: "
QUOTA_FAILURE_PREFIX = "provider quota exhausted: "
PROVIDER_FAILURE_MARKERS = (AUTH_FAILURE_PREFIX.strip(), QUOTA_FAILURE_PREFIX.strip())


class LLMCallError(GatewayError):
    """The provider call failed for good: retries exhausted, or not retryable.

    The message is redacted before it is built -- provider exceptions routinely
    echo parts of the request.

    `kind` separates the provider refusing us (`auth`, `quota`) from every other failure. The agent
    loop must not turn the first two into a stop reason: a task that could not talk to the provider
    is a harness failure, not a task the agent failed.
    """

    def __init__(self, message: str, *, stage: str, model: str, retries: int, kind: str = KIND_OTHER) -> None:
        super().__init__(message)
        self.stage = stage
        self.model = model
        self.retries = retries
        self.kind = kind

    @property
    def provider_refused(self) -> bool:
        return self.kind != KIND_OTHER
