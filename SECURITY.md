# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue.

Use GitHub's private vulnerability reporting: open the **Security** tab of this repository and choose
**Report a vulnerability**. Include what you found, how to reproduce it, and what you think the impact is.

I will acknowledge a report within a few days and keep you updated while it is fixed. This is a solo
project, so there is no bug bounty and no guaranteed response time.

## What is in scope

repolace runs model-written code, so the parts worth attacking are the boundaries around it:

- the verify sandbox (`verify/`): escapes, network access, writes outside the export;
- how repository content and issue or comment text reach a model (`agents/`, `pipeline/`);
- the public site (`api/repolace_api/site/`), the only process meant to be reachable from the internet;
- secrets handling (provider keys and the GitHub App key).

The design of each boundary is described in [docs/security.md](docs/security.md).

## Known limits

The private API and task UI (`/ui`, `/repos`, `/tasks`) are for localhost or a private tunnel only and have
no general authentication yet; do not expose them to the internet. This is documented, not a vulnerability
to report. A bypass of the public/private separation, or a way to reach those routes through the public
site, is in scope.
