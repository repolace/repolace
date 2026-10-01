"""Pin LiteLLM's price map before anything in this directory imports it.

`repolace_gateway.client` sets this itself, but only if it is imported *before*
`litellm`, and `gateway_support` (rightly) imports `litellm` to build real
response objects. A conftest runs before any test module in the directory, so
the pin holds regardless of import order below it.

Without it LiteLLM tries to download the current price map from GitHub when it
is imported: slow or hanging on a machine with no network, and -- worse for the
tests that compare against the map -- a different map from the one the gateway
runs against in production.
"""

import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
