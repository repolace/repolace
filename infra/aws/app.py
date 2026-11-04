#!/usr/bin/env python3
"""CDK entry point for the public site host.

The stack is environment-agnostic on purpose: `cdk synth` needs no AWS credentials. `cdk deploy` uses the
account and region of the credentials in use. Context keys (all optional, set with `-c key=value` or in
cdk.json): architecture (arm64|x86_64), instance_type, github_repo, create_oidc_provider (true|false).
"""

import aws_cdk as cdk
from stacks.site_stack import RepolaceSiteStack

app = cdk.App()
RepolaceSiteStack(app, "RepolaceSiteStack", description="Public site host for repolace: one VM, Cloudflare Tunnel, SSM-only access")
app.synth()
