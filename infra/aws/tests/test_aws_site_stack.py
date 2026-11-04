"""The site host stack, checked against its synthesized template (no AWS credentials, nothing deployed)."""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("aws_cdk", reason="aws-cdk-lib is installed from infra/aws/requirements.txt")

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stacks.site_stack import RepolaceSiteStack


def synth(**context) -> Template:
    app = cdk.App(context=context)
    return Template.from_stack(RepolaceSiteStack(app, "TestStack"))


@pytest.fixture(scope="module")
def template() -> Template:
    return synth()


def policy_statements(template: Template) -> list[dict]:
    out = []
    for res in template.find_resources("AWS::IAM::Policy").values():
        statements = res["Properties"]["PolicyDocument"]["Statement"]
        out += statements if isinstance(statements, list) else [statements]
    return out


def test_security_group_has_no_inbound_rules(template):
    groups = template.find_resources("AWS::EC2::SecurityGroup")
    assert len(groups) == 1
    for group in groups.values():
        assert "SecurityGroupIngress" not in group["Properties"]
    assert not template.find_resources("AWS::EC2::SecurityGroupIngress")


def test_instance_requires_imdsv2_and_has_no_key_pair(template):
    template.has_resource_properties(
        "AWS::EC2::LaunchTemplate",
        {"LaunchTemplateData": {"MetadataOptions": {"HttpTokens": "required"}}},
    )
    for instance in template.find_resources("AWS::EC2::Instance").values():
        assert "KeyName" not in instance["Properties"]


def test_root_volume_is_encrypted_gp3_20gb(template):
    template.has_resource_properties(
        "AWS::EC2::Instance",
        {"BlockDeviceMappings": [Match.object_like({"DeviceName": "/dev/xvda", "Ebs": Match.object_like({"VolumeSize": 20, "VolumeType": "gp3", "Encrypted": True})})]},
    )


def test_default_host_is_arm64_and_x86_is_one_context_flag():
    template = synth()
    template.has_resource_properties("AWS::EC2::Instance", {"InstanceType": "t4g.small"})
    x86 = synth(architecture="x86_64")
    x86.has_resource_properties("AWS::EC2::Instance", {"InstanceType": "t3a.small"})
    ami_params = json.dumps(x86.to_json()["Parameters"])
    assert "x86_64" in ami_params


def test_two_oidc_roles_with_distinct_subjects(template):
    subjects = {}
    for res in template.find_resources("AWS::IAM::Role").values():
        for stmt in res["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            if stmt["Action"] == "sts:AssumeRoleWithWebIdentity":
                cond = stmt["Condition"]["StringEquals"]
                assert cond["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
                subjects[res["Properties"]["RoleName"]] = cond["token.actions.githubusercontent.com:sub"]
    assert subjects == {
        "repolace-site-build": "repo:repolace/repolace:ref:refs/heads/main",
        "repolace-site-deploy": "repo:repolace/repolace:environment:production",
    }


def test_oidc_provider_is_optional():
    assert synth().find_resources("Custom::AWSCDKOpenIdConnectProvider")
    assert not synth(create_oidc_provider="false").find_resources("Custom::AWSCDKOpenIdConnectProvider")


def test_build_role_can_only_push_to_ecr(template):
    role = next(k for k, v in template.find_resources("AWS::IAM::Role").items() if v["Properties"].get("RoleName") == "repolace-site-build")
    statements = []
    for res in template.find_resources("AWS::IAM::Policy").values():
        if any(r["Ref"] == role for r in res["Properties"]["Roles"]):
            statements += res["Properties"]["PolicyDocument"]["Statement"]
    actions = {a for s in statements for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert actions and all(a.startswith("ecr:") for a in actions)
    assert not any("ssm:" in a for a in actions)


def test_deploy_role_is_scoped_to_the_instance_and_document(template):
    role = next(k for k, v in template.find_resources("AWS::IAM::Role").items() if v["Properties"].get("RoleName") == "repolace-site-deploy")
    statements = []
    for res in template.find_resources("AWS::IAM::Policy").values():
        if any(r["Ref"] == role for r in res["Properties"]["Roles"]):
            statements += res["Properties"]["PolicyDocument"]["Statement"]
    send = next(s for s in statements if s["Action"] == "ssm:SendCommand")
    rendered = json.dumps(send["Resource"])
    assert len(send["Resource"]) == 2
    assert ":instance/" in rendered and "document/AWS-RunShellScript" in rendered
    assert "*" not in rendered
    other = {a for s in statements if s is not send for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert other == {"ssm:GetCommandInvocation", "ssm:ListCommandInvocations"}


def test_backup_bucket_is_private_versioned_encrypted_and_expiring(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "VersioningConfiguration": {"Status": "Enabled"},
            "BucketEncryption": Match.any_value(),
            "LifecycleConfiguration": {"Rules": Match.array_with([Match.object_like({"ExpirationInDays": 30, "Status": "Enabled"})])},
        },
    )


def test_ecr_repository_keeps_the_last_ten_images(template):
    template.has_resource_properties("AWS::ECR::Repository", {"RepositoryName": "repolace-site", "LifecyclePolicy": Match.any_value()})
    policy = next(iter(template.find_resources("AWS::ECR::Repository").values()))["Properties"]["LifecyclePolicy"]["LifecyclePolicyText"]
    assert '"countNumber":10' in policy.replace(" ", "")


def test_secret_reads_are_exactly_the_two_named_secrets(template):
    reads = [s for s in policy_statements(template) if "secretsmanager:GetSecretValue" in json.dumps(s["Action"])]
    assert len(reads) == 1
    resources = reads[0]["Resource"]
    assert len(resources) == 2
    rendered = [json.dumps(r) for r in resources]
    assert any("secret:repolace-site-env-??????" in r for r in rendered)
    assert any("secret:repolace-tunnel-credentials-??????" in r for r in rendered)
    assert not any(r.strip('"') == "*" or r.endswith('*"') for r in rendered)


def test_no_statement_other_than_the_documented_ones_uses_a_wildcard_resource(template):
    wildcard_actions = {
        "ecr:GetAuthorizationToken",  # does not support resource-level permissions
        "ssm:GetCommandInvocation",  # idem
        "ssm:ListCommandInvocations",  # idem
    }
    for s in policy_statements(template):
        if s["Resource"] == "*":
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            assert set(actions) <= wildcard_actions, s


def test_resource_names_and_descriptions_have_no_em_dash(template):
    assert "—" not in json.dumps(template.to_json())


def test_user_data_installs_the_host_files_without_secret_values(template):
    instance = next(iter(template.find_resources("AWS::EC2::Instance").values()))
    user_data = json.dumps(instance["Properties"]["UserData"])
    assert "bootstrap.sh" in user_data and "repolace-site-env" in user_data
    assert "TUNNEL_TOKEN" not in user_data
