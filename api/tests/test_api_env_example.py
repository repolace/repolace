"""`infra/api.env.example` is the whole environment the `api` container is meant to see.

The API is the internet-facing process, so its env file lists only what `ApiSettings` and
`UiSettings` read. The test holds the example equal to those settings in both directions: a new
setting without an example line fails here, and so does a stray line (a provider key, say) that
nothing in the API reads. It also checks the examples under `infra/` carry no secret values.
"""

import re
from pathlib import Path

from repolace_api.config import ApiSettings, UiSettings

INFRA = Path(__file__).resolve().parents[2] / "infra"
API_ENV_EXAMPLE = INFRA / "api.env.example"

_SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD", re.IGNORECASE)
#: Value shapes of real credentials, so a pasted one is caught even under an innocent-looking name.
_SECRET_VALUE = re.compile(r"-----BEGIN|\bsk-[A-Za-z0-9]|\bghp_|\bgithub_pat_|\bAIza[0-9A-Za-z_-]{20}")


def read_env(path: Path) -> dict[str, str]:
    """`KEY=value` lines, with comments and blank lines dropped."""
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


def test_the_api_example_lists_exactly_what_the_api_reads():
    expected = {name.upper() for name in (*ApiSettings.model_fields, *UiSettings.model_fields)}

    assert set(read_env(API_ENV_EXAMPLE)) == expected


def test_no_example_under_infra_carries_a_secret_value():
    examples = sorted(INFRA.glob("*.example"))
    assert API_ENV_EXAMPLE in examples

    for example in examples:
        for name, value in read_env(example).items():
            if _SECRET_NAME.search(name):
                assert value == "", f"{example.name}: {name} must be an empty placeholder"
            assert not _SECRET_VALUE.search(value), f"{example.name}: {name} holds a credential-shaped value"
