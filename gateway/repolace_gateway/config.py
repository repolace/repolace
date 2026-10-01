"""Stage -> model routing, prices, and the provider credentials.

Two things live here and are kept apart on purpose:

* `GatewayConfig`, loaded from `gateway/models.toml`: which model serves which
  stage, what each model is called to LiteLLM, and what it costs if LiteLLM does
  not know. Plain data, checked into the repo, no secrets.
* `GatewaySettings`: the provider API keys, read from the environment or `.env`.

The keys are *only* here. They are never added to `sanitized_git_env` or
`sanitized_docker_env`, which are allowlists and so already exclude them, and
they are handed to LiteLLM per call as `api_key=` rather than exported into
`os.environ`, so a key that came from `.env` is never in this process's
environment for a child to inherit.

Every table in `models.toml` rejects unknown keys. A misspelt
`supports_prompt_cacheing` would otherwise be ignored without a word, and a
setting that fails open is the kind this repo has been bitten by before.
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from repolace_gateway.errors import ConfigError, MissingProviderKey, UnpricedModelError

_REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_MODELS_PATH = Path(__file__).resolve().parents[1] / "models.toml"

#: Provider name -> the `GatewaySettings` field holding its key. Adding a
#: provider is one field and one entry here; a model naming a provider that is
#: not in this table is rejected at load rather than failing on its first call.
PROVIDER_KEY_FIELDS: Mapping[str, str] = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
}

#: Stages the pipeline calls by name. A config missing either is wrong at load.
REQUIRED_STAGES = ("agent", "cheap")

_MILLION = Decimal(1_000_000)
#: Anything shorter is not a credential, and replacing it everywhere in a stored
#: prompt would corrupt the record instead of protecting it.
_MIN_SECRET_LENGTH = 8


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT_ENV_FILE, extra="ignore", populate_by_name=True
    )

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    #: Aliased so the environment names carry a `GATEWAY_` prefix: bare
    #: `MODELS_PATH` and `STAGE_MODELS` are too generic to claim in a shared `.env`.
    #: (The provider keys keep their standard names, which other tooling expects.)
    models_path: Path = Field(default=DEFAULT_MODELS_PATH, validation_alias="GATEWAY_MODELS_PATH")
    #: Per-run stage routing overrides, e.g. `GATEWAY_STAGE_MODELS='{"agent": "gpt-x"}'`.
    #: How the eval runner points a one-run comparison at another model without
    #: editing `models.toml`: it is the channel that survives a subprocess boundary.
    stage_models: dict[str, str] = Field(default_factory=dict, validation_alias="GATEWAY_STAGE_MODELS")

    def api_key_for(self, provider: str) -> str:
        field = PROVIDER_KEY_FIELDS.get(provider)
        secret: SecretStr | None = getattr(self, field) if field else None
        if secret is None or not secret.get_secret_value():
            env_name = (field or f"{provider}_api_key").upper()
            raise MissingProviderKey(
                f"no API key for provider {provider!r}: set {env_name} in the gateway's environment"
            )
        return secret.get_secret_value()

    def secret_values(self) -> tuple[str, ...]:
        """Every configured key, for exact-match redaction of stored payloads."""
        values: list[str] = []
        for field in PROVIDER_KEY_FIELDS.values():
            secret: SecretStr | None = getattr(self, field)
            if secret is not None and len(secret.get_secret_value()) >= _MIN_SECRET_LENGTH:
                values.append(secret.get_secret_value())
        return tuple(values)


@dataclass(frozen=True)
class Price:
    """USD per token. Built from per-million-token rates, which is how providers publish them.

    Used only for a model LiteLLM's price map does not cover. It is deliberately
    plain arithmetic: LiteLLM's custom-pricing hook takes an input and an output
    rate and nothing for cache reads or writes, and a cache-heavy agent loop is
    exactly where that omission would put the number wrong.
    """

    input: Decimal
    output: Decimal
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None

    def cost(
        self,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        """Price one call. `input_tokens` is the total prompt, cached tokens included.

        A cache token with no rate to price it is an error, not a fallback to
        the input rate: that would overstate a cache read tenfold and look
        entirely plausible while doing it.
        """
        uncached = input_tokens - cached_input_tokens - cache_write_tokens
        if uncached < 0:
            raise UnpricedModelError(
                f"inconsistent usage: {cached_input_tokens} cached + {cache_write_tokens} cache-write "
                f"tokens exceed the {input_tokens} reported prompt tokens"
            )
        if cached_input_tokens and self.cache_read is None:
            raise UnpricedModelError(
                f"the call reported {cached_input_tokens} cache-read tokens but the price has no "
                "cache_read rate"
            )
        if cache_write_tokens and self.cache_write is None:
            raise UnpricedModelError(
                f"the call reported {cache_write_tokens} cache-write tokens but the price has no "
                "cache_write rate"
            )
        return (
            uncached * self.input
            + cached_input_tokens * (self.cache_read or Decimal(0))
            + cache_write_tokens * (self.cache_write or Decimal(0))
            + output_tokens * self.output
        )


@dataclass(frozen=True)
class ModelConfig:
    #: The key in `models.toml`; what a stage names.
    key: str
    provider: str
    litellm_model: str
    #: Whether the gateway sends Anthropic-style `cache_control` markers. Distinct
    #: from the provider caching anyway: OpenAI caches prefixes automatically and
    #: reports it in usage, with no marker from us.
    supports_prompt_caching: bool
    #: None means LiteLLM's own price map must cover the model.
    price: Price | None = None


@dataclass(frozen=True)
class StageConfig:
    name: str
    model: str
    fallback: str | None = None
    #: Whether this stage's calls carry cache markers by default.
    cache: bool = False
    max_tokens: int | None = None


@dataclass(frozen=True)
class RetryPolicy:
    #: Total tries per model, the first included.
    max_attempts: int = 5
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 60.0
    request_timeout_seconds: float = 180.0


@dataclass(frozen=True)
class Route:
    stage: StageConfig
    primary: ModelConfig
    fallback: ModelConfig | None


@dataclass(frozen=True)
class GatewayConfig:
    models: Mapping[str, ModelConfig]
    stages: Mapping[str, StageConfig]
    retry: RetryPolicy = RetryPolicy()

    def route(self, stage: str) -> Route:
        stage_config = self.stages.get(stage)
        if stage_config is None:
            raise ConfigError(f"unknown stage {stage!r}; configured: {sorted(self.stages)}")
        return Route(
            stage=stage_config,
            primary=self.models[stage_config.model],
            fallback=self.models[stage_config.fallback] if stage_config.fallback else None,
        )

    def with_stage_models(self, overrides: Mapping[str, str]) -> "GatewayConfig":
        """The same config with some stages pointed at other models."""
        stages = dict(self.stages)
        for stage, model_key in overrides.items():
            if stage not in stages:
                raise ConfigError(f"cannot override unknown stage {stage!r}; configured: {sorted(stages)}")
            if model_key not in self.models:
                raise ConfigError(
                    f"stage {stage!r} override names unknown model {model_key!r}; "
                    f"configured: {sorted(self.models)}"
                )
            stages[stage] = replace(stages[stage], model=model_key)
        return replace(self, stages=stages)


def _reject_unknown(table: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")


def _require(table: Mapping[str, Any], key: str, kind: type | tuple[type, ...], where: str) -> Any:
    if key not in table:
        raise ConfigError(f"{where}: missing required key {key!r}")
    value = table[key]
    # bool is an int subclass; a `max_tokens = true` is a typo, not a number.
    if not isinstance(value, kind) or (isinstance(value, bool) and bool not in _as_tuple(kind)):
        raise ConfigError(f"{where}.{key}: expected {_kind_name(kind)}, got {value!r}")
    return value


def _as_tuple(kind: type | tuple[type, ...]) -> tuple[type, ...]:
    return kind if isinstance(kind, tuple) else (kind,)


def _kind_name(kind: type | tuple[type, ...]) -> str:
    return " or ".join(k.__name__ for k in _as_tuple(kind))


def _per_token(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a USD-per-million-tokens number, got {value!r}")
    if value < 0:
        raise ConfigError(f"{where}: a price cannot be negative, got {value!r}")
    # str() first: Decimal(0.3) is the float's binary expansion, not 0.3.
    return Decimal(str(value)) / _MILLION


def _parse_price(table: Mapping[str, Any], supports_caching: bool, where: str) -> Price:
    _reject_unknown(table, {"input", "output", "cache_read", "cache_write"}, where)
    price = Price(
        input=_per_token(_require(table, "input", (int, float), where), f"{where}.input"),
        output=_per_token(_require(table, "output", (int, float), where), f"{where}.output"),
        cache_read=_per_token(table["cache_read"], f"{where}.cache_read") if "cache_read" in table else None,
        cache_write=(
            _per_token(table["cache_write"], f"{where}.cache_write") if "cache_write" in table else None
        ),
    )
    # A caching model priced without cache rates would be priced wrong on every
    # call that hit the cache -- most of them, in an agent loop.
    if supports_caching and (price.cache_read is None or price.cache_write is None):
        raise ConfigError(
            f"{where}: the model sets supports_prompt_caching, so the price needs both "
            "cache_read and cache_write"
        )
    return price


def _parse_model(key: str, table: Mapping[str, Any]) -> ModelConfig:
    where = f"models.{key}"
    _reject_unknown(table, {"provider", "litellm_model", "supports_prompt_caching", "price"}, where)
    provider = _require(table, "provider", str, where)
    litellm_model = _require(table, "litellm_model", str, where)
    supports_caching = _require(table, "supports_prompt_caching", bool, where)

    if provider not in PROVIDER_KEY_FIELDS:
        raise ConfigError(
            f"{where}.provider: unknown provider {provider!r}; known: {sorted(PROVIDER_KEY_FIELDS)}"
        )
    # The key is chosen by `provider` and the route by LiteLLM's model prefix. If
    # they disagree, the call carries one vendor's key to another vendor's API.
    if not litellm_model.startswith(f"{provider}/"):
        raise ConfigError(
            f"{where}.litellm_model: {litellm_model!r} does not start with {provider + '/'!r}; the "
            "provider's key would be sent to a different vendor"
        )
    price_table = table.get("price")
    if price_table is not None and not isinstance(price_table, dict):
        raise ConfigError(f"{where}.price: expected a table")
    price = _parse_price(price_table, supports_caching, f"{where}.price") if price_table is not None else None
    return ModelConfig(
        key=key,
        provider=provider,
        litellm_model=litellm_model,
        supports_prompt_caching=supports_caching,
        price=price,
    )


def _parse_stage(name: str, table: Mapping[str, Any], models: Mapping[str, ModelConfig]) -> StageConfig:
    where = f"stages.{name}"
    _reject_unknown(table, {"model", "fallback", "cache", "max_tokens"}, where)
    model = _require(table, "model", str, where)
    fallback = table.get("fallback")
    if fallback is not None and not isinstance(fallback, str):
        raise ConfigError(f"{where}.fallback: expected a model key")
    # An empty string is how a TOML author writes "none" when they cannot comment a key out.
    fallback = fallback or None
    for label, ref in (("model", model), ("fallback", fallback)):
        if ref is not None and ref not in models:
            raise ConfigError(f"{where}.{label}: unknown model {ref!r}; configured: {sorted(models)}")
    cache = table.get("cache", False)
    if not isinstance(cache, bool):
        raise ConfigError(f"{where}.cache: expected true or false, got {cache!r}")
    max_tokens = table.get("max_tokens")
    if max_tokens is not None:
        max_tokens = _require(table, "max_tokens", int, where)
        if max_tokens <= 0:
            raise ConfigError(f"{where}.max_tokens: must be positive, got {max_tokens}")
    return StageConfig(name=name, model=model, fallback=fallback, cache=cache, max_tokens=max_tokens)


def _parse_retry(table: Mapping[str, Any]) -> RetryPolicy:
    where = "gateway"
    _reject_unknown(
        table,
        {"max_attempts", "base_delay_seconds", "max_delay_seconds", "request_timeout_seconds"},
        where,
    )
    default = RetryPolicy()
    policy = RetryPolicy(
        max_attempts=table.get("max_attempts", default.max_attempts),
        base_delay_seconds=table.get("base_delay_seconds", default.base_delay_seconds),
        max_delay_seconds=table.get("max_delay_seconds", default.max_delay_seconds),
        request_timeout_seconds=table.get("request_timeout_seconds", default.request_timeout_seconds),
    )
    if isinstance(policy.max_attempts, bool) or not isinstance(policy.max_attempts, int) or policy.max_attempts < 1:
        raise ConfigError(f"{where}.max_attempts: expected an integer >= 1, got {policy.max_attempts!r}")
    for name in ("base_delay_seconds", "max_delay_seconds", "request_timeout_seconds"):
        value = getattr(policy, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"{where}.{name}: expected a positive number, got {value!r}")
    return policy


def parse_config(raw: Mapping[str, Any], stage_models: Mapping[str, str] | None = None) -> GatewayConfig:
    """Validate a decoded `models.toml`. Separate from `load_config` so tests need no file."""
    _reject_unknown(raw, {"gateway", "stages", "models"}, "models.toml")
    model_tables = raw.get("models")
    stage_tables = raw.get("stages")
    if not isinstance(model_tables, dict) or not model_tables:
        raise ConfigError("models.toml: define at least one [models.<key>] table")
    if not isinstance(stage_tables, dict):
        raise ConfigError("models.toml: define the [stages.*] tables")

    models: dict[str, ModelConfig] = {}
    for key, table in model_tables.items():
        if not isinstance(table, dict):
            raise ConfigError(f"models.{key}: expected a table")
        models[key] = _parse_model(key, table)

    stages: dict[str, StageConfig] = {}
    for name, table in stage_tables.items():
        if not isinstance(table, dict):
            raise ConfigError(f"stages.{name}: expected a table")
        stages[name] = _parse_stage(name, table, models)
    missing = [name for name in REQUIRED_STAGES if name not in stages]
    if missing:
        raise ConfigError(f"models.toml: missing required stage(s) {missing}")

    retry_table = raw.get("gateway", {})
    if not isinstance(retry_table, dict):
        raise ConfigError("gateway: expected a table")
    config = GatewayConfig(models=models, stages=stages, retry=_parse_retry(retry_table))
    return config.with_stage_models(stage_models) if stage_models else config


def load_config(path: Path = DEFAULT_MODELS_PATH, stage_models: Mapping[str, str] | None = None) -> GatewayConfig:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"gateway model config not found at {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
    return parse_config(raw, stage_models)
