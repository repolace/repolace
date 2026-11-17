#!/usr/bin/env python3
"""CDK entry point for the two hosts.

The stacks are environment-agnostic on purpose: `cdk synth` needs no AWS credentials. `cdk deploy` uses the
account and region of the credentials in use.

- `RepolaceSiteStack` (the public site host) always exists. Context keys (all optional, `-c key=value` or
  cdk.json): ami_id (required), architecture (arm64|x86_64), instance_type, github_repo, create_oidc_provider.
- `RepolaceAppStack` (the API and the task worker) exists only when `public_url` is given, so the site's
  `cdk synth` and `cdk deploy` are unchanged. Its keys are prefixed so the two never share one: public_url
  (required), app_ami_id (required), app_instance_type, data_volume_gib, docker_volume_gib,
  workspace_volume_gib, app_existing_data_volume_id, app_bootstrap_ref. Do not put
  `public_url` in cdk.json: with it set, a missing `app_ami_id` stops the site's synth too.
"""

import aws_cdk as cdk
from stacks.app_stack import RepolaceAppStack
from stacks.site_stack import RepolaceSiteStack

app = cdk.App()
RepolaceSiteStack(app, "RepolaceSiteStack", description="Public site host for repolace: one VM, Cloudflare Tunnel, SSM-only access")
if app.node.try_get_context("public_url"):
    RepolaceAppStack(
        app,
        "RepolaceAppStack",
        description="App host for repolace: API, task worker and their stores on one VM, rootless Docker, Cloudflare Tunnel, SSM-only access",
    )
app.synth()
