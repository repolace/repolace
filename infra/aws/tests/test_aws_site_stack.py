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

AMI = "ami-0123456789abcdef0"


def synth(**context) -> Template:
    app = cdk.App(context={"ami_id": AMI, **context})
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


def test_instance_requires_imdsv2_with_hop_limit_one_and_has_no_key_pair(template):
    template.has_resource_properties(
        "AWS::EC2::Instance",
        {"MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1, "HttpEndpoint": "enabled"}},
    )
    for instance in template.find_resources("AWS::EC2::Instance").values():
        assert "KeyName" not in instance["Properties"]


def test_ami_is_pinned_never_an_ssm_parameter(template):
    template.has_resource_properties("AWS::EC2::Instance", {"ImageId": AMI})
    # The only SSM parameter allowed is CDK's own bootstrap-version check.
    for name, param in template.to_json().get("Parameters", {}).items():
        assert name == "BootstrapVersion" or "ssm" not in param["Type"].lower(), name
    assert "aws/service/ami" not in json.dumps(template.to_json())


def test_missing_or_malformed_ami_is_refused():
    for context in ({"ami_id": ""}, {"ami_id": "not-an-ami"}):
        with pytest.raises(ValueError):
            Template.from_stack(RepolaceSiteStack(cdk.App(context=context), "Bad"))


def test_data_volume_is_separate_encrypted_retained_and_attached(template):
    volumes = template.find_resources("AWS::EC2::Volume")
    assert len(volumes) == 1
    (volume,) = volumes.values()
    assert volume["DeletionPolicy"] == "Retain" and volume["UpdateReplacePolicy"] == "Retain"
    assert volume["Properties"]["Encrypted"] is True and volume["Properties"]["VolumeType"] == "gp3"
    # The root device is the only one in the instance's own mappings, so the data volume is never deleted with it.
    template.has_resource_properties("AWS::EC2::VolumeAttachment", {"Device": "/dev/sdf"})
    instance = next(iter(template.find_resources("AWS::EC2::Instance").values()))
    assert len(instance["Properties"]["BlockDeviceMappings"]) == 1
    # The volume's zone is the subnet's, not a reference to the instance (a replaced instance must not replace it).
    assert "Host" not in json.dumps(volume["Properties"]["AvailabilityZone"])


def test_data_volume_has_a_daily_snapshot_policy(template):
    template.has_resource_properties(
        "AWS::DLM::LifecyclePolicy",
        {"State": "ENABLED", "PolicyDetails": Match.object_like({"ResourceTypes": ["VOLUME"], "TargetTags": [{"Key": "repolace-snapshot", "Value": "daily"}]})},
    )


def test_existing_data_volume_can_be_reused():
    reused = synth(existing_data_volume_id="vol-0123456789abcdef0")
    assert not reused.find_resources("AWS::EC2::Volume")
    reused.has_resource_properties("AWS::EC2::VolumeAttachment", {"VolumeId": "vol-0123456789abcdef0"})


def test_stack_has_termination_protection_and_user_data_never_replaces_the_instance():
    app = cdk.App(context={"ami_id": AMI})
    stack = RepolaceSiteStack(app, "Protected")
    assert stack.termination_protection is True
    instance = next(iter(Template.from_stack(stack).find_resources("AWS::EC2::Instance").values()))
    assert "UserData" in instance["Properties"]


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


def test_two_oidc_roles_with_distinct_subjects(template):
    subjects = {}
    workflow_refs = {}
    for res in template.find_resources("AWS::IAM::Role").values():
        for stmt in res["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            if stmt["Action"] == "sts:AssumeRoleWithWebIdentity":
                cond = stmt["Condition"]["StringEquals"]
                assert cond["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
                subjects[res["Properties"]["RoleName"]] = cond["token.actions.githubusercontent.com:sub"]
                workflow_refs[res["Properties"]["RoleName"]] = cond.get("token.actions.githubusercontent.com:job_workflow_ref")
    assert subjects == {
        "repolace-site-build": "repo:repolace/repolace:ref:refs/heads/main",
        "repolace-site-deploy": "repo:repolace/repolace:environment:production",
    }
    # The deploy role is also tied to the workflow file and ref; the build role has no such claim.
    assert workflow_refs == {
        "repolace-site-build": None,
        "repolace-site-deploy": "repolace/repolace/.github/workflows/deploy-site.yml@refs/heads/main",
    }


def test_immutable_subject_prefix_is_used_when_configured():
    prefix = "repo:repolace@319966794/repolace@1343131202"
    subjects = set()
    for res in synth(github_sub_prefix=prefix).find_resources("AWS::IAM::Role").values():
        for stmt in res["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            if stmt["Action"] == "sts:AssumeRoleWithWebIdentity":
                subjects.add(stmt["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"])
    assert subjects == {f"{prefix}:ref:refs/heads/main", f"{prefix}:environment:production"}


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
    assert other == {"ssm:GetCommandInvocation"}


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


def test_ecr_tags_are_immutable(template):
    template.has_resource_properties("AWS::ECR::Repository", {"ImageTagMutability": "IMMUTABLE"})


def test_instance_role_cannot_read_backups_or_wildcard_s3(template):
    s3_actions = {
        a
        for s in policy_statements(template)
        for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        if a.startswith("s3:")
    }
    assert s3_actions == {"s3:PutObject", "s3:AbortMultipartUpload"}


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
    # Minimal: the scripts and compose file are fetched or refreshed, not embedded.
    assert "deploy.sh" not in user_data and "compose.site.yml" not in user_data
