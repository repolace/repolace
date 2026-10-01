"""The LLM gateway: the one door every model call goes through.

Import the submodules directly (`repolace_gateway.client`, `.budget`, ...). This
file re-exports nothing on purpose: `client` imports LiteLLM, which is slow and
pins its price map at import, and a caller that only wants `budget` should not
pay for either.
"""
