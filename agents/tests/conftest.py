"""Pin LiteLLM's price map before anything in this directory imports it.

Same rationale as `gateway/tests/conftest.py`. `repolace_gateway.client` sets
this itself, but only if it is imported *before* `litellm`; a conftest runs
before any test module in the directory, so the pin holds whatever order the
tests below it import in.

Without it LiteLLM tries to download the current price map from GitHub at import
time: slow or hanging on a machine with no network, and a different map from the
one the gateway runs against in production.

The `repolace_agents` contract modules themselves never import `litellm` (a test
asserts it in a subprocess); this pin is for the graph and pipeline tests that
build real gateway responses later.
"""

import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
