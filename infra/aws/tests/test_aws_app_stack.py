"""The app host stack (API, task worker and their stores), checked against its synthesized template.
No AWS credentials, nothing deployed."""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("aws_cdk", reason="aws-cdk-lib is installed from infra/aws/requirements.txt")

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stacks.app_stack import RepolaceAppStack

AMI = "ami-0fedcba987654321a"
URL = "https://app.example.com"
OIDC = "token.actions.githubusercontent.com"


def synth(**context) -> Template:
    app = cdk.App(context={"app_ami_id": AMI, "public_url": URL, **context})
    return Template.from_stack(RepolaceAppStack(app, "TestAppStack"))


@pytest.fixture(scope="module")
def template() -> Template:
    return synth()


def statements_of(template: Template, role_name: str | None = None) -> list[dict]:
    """The IAM policy statements attached to the role `role_name`, or all of them."""
    role = None
    if role_name:
        role = next(k for k, v in template.find_resources("AWS::IAM::Role").items() if v["Properties"].get("RoleName") == role_name)
    out = []
    for res in template.find_resources("AWS::IAM::Policy").values():
        if role is None or any(r["Ref"] == role for r in res["Properties"]["Roles"]):
            body = res["Properties"]["PolicyDocument"]["Statement"]
            out += body if isinstance(body, list) else [body]
    return out


def actions(statements: list[dict]) -> set[str]:
    return {a for s in statements for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}


def instance_zone(template: Template):
    """The availability zone the instance's subnet is in, as the template expresses it."""
    instance = next(iter(template.find_resources("AWS::EC2::Instance").values()))
    subnet = instance["Properties"]["NetworkInterfaces"][0]["SubnetId"]["Ref"]
    return template.to_json()["Resources"][subnet]["Properties"]["AvailabilityZone"]


def test_security_group_has_no_inbound_rules(template):
    groups = template.find_resources("AWS::EC2::SecurityGroup")
    assert len(groups) == 1
    for group in groups.values():
        assert "SecurityGroupIngress" not in group["Properties"]
    assert not template.find_resources("AWS::EC2::SecurityGroupIngress")


def test_instance_requires_imdsv2_with_one_hop_and_has_no_key_pair(template):
    """The host runs model-written code in containers: they must not reach the instance credentials."""
    template.has_resource_properties(
        "AWS::EC2::Instance",
        {"MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1, "HttpEndpoint": "enabled"}},
    )
    for instance in template.find_resources("AWS::EC2::Instance").values():
        assert "KeyName" not in instance["Properties"]


def test_the_host_is_a_pinned_x86_64_ami_with_an_8_gib_default_size(template):
    template.has_resource_properties("AWS::EC2::Instance", {"ImageId": AMI, "InstanceType": "m6i.large"})
    assert "aws/service/ami" not in json.dumps(template.to_json())
    for name, param in template.to_json().get("Parameters", {}).items():
        assert name == "BootstrapVersion" or "ssm" not in param["Type"].lower(), name


def test_instance_type_is_one_context_key():
    synth(app_instance_type="m6i.xlarge").has_resource_properties("AWS::EC2::Instance", {"InstanceType": "m6i.xlarge"})


@pytest.mark.parametrize(
    "context",
    [{"app_ami_id": ""}, {"app_ami_id": "not-an-ami"}, {"public_url": ""}, {"public_url": "http://app.example.com"}, {"public_url": "https://app.example.com/path"}],
)
def test_missing_or_malformed_required_context_is_refused(context):
    with pytest.raises(ValueError):
        synth(**context)


def test_root_device_is_ubuntus_and_encrypted(template):
    instance = next(iter(template.find_resources("AWS::EC2::Instance").values()))
    assert instance["Properties"]["InstanceType"].startswith(("m6i.", "t3.", "c6i.", "m5."))  # x86_64 families, never Graviton
    (root,) = instance["Properties"]["BlockDeviceMappings"]
    assert root["DeviceName"] == "/dev/sda1"
    assert root["Ebs"]["Encrypted"] is True and root["Ebs"]["VolumeType"] == "gp3" and root["Ebs"]["DeleteOnTermination"] is True


def test_three_extra_volumes_are_attached_each_to_its_own_device(template):
    attachments = {v["Properties"]["Device"] for v in template.find_resources("AWS::EC2::VolumeAttachment").values()}
    assert attachments == {"/dev/sdf", "/dev/sdg", "/dev/sdh"}
    volumes = template.find_resources("AWS::EC2::Volume")
    assert len(volumes) == 3
    for volume in volumes.values():
        assert volume["Properties"]["Encrypted"] is True and volume["Properties"]["VolumeType"] == "gp3"
        # The zone is the subnet's, never a reference to the instance (a replaced instance must not replace a volume),
        # and it is the zone the instance really lands in (the first public subnet's).
        assert "Host" not in json.dumps(volume["Properties"]["AvailabilityZone"])
        assert volume["Properties"]["AvailabilityZone"] == instance_zone(template)


def test_only_the_data_volume_is_retained_and_snapshotted(template):
    volumes = template.find_resources("AWS::EC2::Volume")
    retained = [v for v in volumes.values() if v["DeletionPolicy"] == "Retain"]
    assert len(retained) == 1
    tags = {t["Key"] for t in retained[0]["Properties"]["Tags"]}
    assert "repolace-app-snapshot" in tags
    # The docker data root and the workspace are rebuildable: deleted with the stack, never snapshotted.
    scratch = [v for v in volumes.values() if v["DeletionPolicy"] == "Delete"]
    assert len(scratch) == 2
    assert all("repolace-app-snapshot" not in json.dumps(v["Properties"].get("Tags", [])) for v in scratch)
    template.has_resource_properties(
        "AWS::DLM::LifecyclePolicy",
        {"PolicyDetails": Match.object_like({"TargetTags": [{"Key": "repolace-app-snapshot", "Value": "daily"}]})},
    )


def test_default_sizes_and_the_context_that_changes_them(template):
    sizes = sorted(v["Properties"]["Size"] for v in template.find_resources("AWS::EC2::Volume").values())
    assert sizes == [30, 30, 80]
    bigger = synth(data_volume_gib="50", docker_volume_gib="120", workspace_volume_gib="40")
    assert sorted(v["Properties"]["Size"] for v in bigger.find_resources("AWS::EC2::Volume").values()) == [40, 50, 120]


def test_an_existing_data_volume_can_be_reused():
    reused = synth(app_existing_data_volume_id="vol-0123456789abcdef0")
    assert len(reused.find_resources("AWS::EC2::Volume")) == 2  # only the two scratch volumes are created
    reused.has_resource_properties("AWS::EC2::VolumeAttachment", {"Device": "/dev/sdf", "VolumeId": "vol-0123456789abcdef0"})


def test_stack_has_termination_protection():
    app = cdk.App(context={"app_ami_id": AMI, "public_url": URL})
    assert RepolaceAppStack(app, "T").termination_protection is True


def test_two_ecr_repos_immutable_and_scanned(template):
    repos = template.find_resources("AWS::ECR::Repository")
    assert sorted(v["Properties"]["RepositoryName"] for v in repos.values()) == ["repolace-api", "repolace-worker"]
    for repo in repos.values():
        assert repo["Properties"]["ImageTagMutability"] == "IMMUTABLE"
        assert repo["Properties"]["ImageScanningConfiguration"] == {"ScanOnPush": True}
        assert repo["DeletionPolicy"] == "Retain"


def test_the_oidc_provider_is_imported_never_created(template):
    """The site stack owns it, and an account can have only one."""
    assert not template.find_resources("Custom::AWSCDKOpenIdConnectProvider")
    assert not template.find_resources("AWS::IAM::OIDCProvider")


def test_two_oidc_roles_with_distinct_subjects_and_their_own_environment(template):
    subjects, workflow_refs = {}, {}
    for res in template.find_resources("AWS::IAM::Role").values():
        for stmt in res["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            if stmt["Action"] == "sts:AssumeRoleWithWebIdentity":
                cond = stmt["Condition"]["StringEquals"]
                assert cond[f"{OIDC}:aud"] == "sts.amazonaws.com"
                subjects[res["Properties"]["RoleName"]] = cond[f"{OIDC}:sub"]
                workflow_refs[res["Properties"]["RoleName"]] = cond.get(f"{OIDC}:job_workflow_ref")
    assert subjects == {
        "repolace-app-build": "repo:repolace/repolace:ref:refs/heads/main",
        "repolace-app-deploy": "repo:repolace/repolace:environment:production-app",
    }
    # Both are pinned to the app workflow: the build job has no environment, so its `sub` alone is shared with
    # every job on main (the site's build job included).
    workflow = "repolace/repolace/.github/workflows/deploy-app.yml@refs/heads/main"
    assert workflow_refs == {"repolace-app-build": workflow, "repolace-app-deploy": workflow}


def oidc_conditions(template: Template) -> list[dict]:
    out = []
    for res in template.find_resources("AWS::IAM::Role").values():
        for stmt in res["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            if stmt["Action"] == "sts:AssumeRoleWithWebIdentity":
                out.append(stmt["Condition"]["StringEquals"])
    return out


def test_no_app_role_can_be_assumed_by_a_job_the_site_roles_trust():
    """A site deploy approval (environment `production`) must not be able to deploy the app, and the site's
    build job must not be able to push app images."""
    from stacks.site_stack import RepolaceSiteStack

    site = Template.from_stack(RepolaceSiteStack(cdk.App(context={"ami_id": "ami-0123456789abcdef0"}), "Site"))
    site_subs = {c[f"{OIDC}:sub"] for c in oidc_conditions(site)}
    app_subs = {c[f"{OIDC}:sub"] for c in oidc_conditions(synth())}
    assert "repo:repolace/repolace:environment:production" in site_subs
    assert not site_subs & {"repo:repolace/repolace:environment:production-app"}
    # The one subject they share (a job on main with no environment) is told apart by the workflow file.
    shared = site_subs & app_subs
    assert shared == {"repo:repolace/repolace:ref:refs/heads/main"}
    assert all(c.get(f"{OIDC}:job_workflow_ref") for c in oidc_conditions(synth()))
    site_refs = {c.get(f"{OIDC}:job_workflow_ref") for c in oidc_conditions(site)}
    assert not site_refs & {c[f"{OIDC}:job_workflow_ref"] for c in oidc_conditions(synth())}


def test_the_imported_oidc_provider_is_the_account_wide_github_one(template):
    rendered = json.dumps(list(template.find_resources("AWS::IAM::Role").values()))
    assert ":oidc-provider/token.actions.githubusercontent.com" in rendered


def test_the_immutable_subject_prefix_is_used_when_configured():
    prefix = "repo:repolace@319966794/repolace@1343131202"
    subjects = set()
    for res in synth(github_sub_prefix=prefix).find_resources("AWS::IAM::Role").values():
        for stmt in res["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            if stmt["Action"] == "sts:AssumeRoleWithWebIdentity":
                subjects.add(stmt["Condition"]["StringEquals"][f"{OIDC}:sub"])
    assert subjects == {f"{prefix}:ref:refs/heads/main", f"{prefix}:environment:production-app"}


def test_build_role_can_only_push_to_the_two_repos(template):
    statements = statements_of(template, "repolace-app-build")
    assert actions(statements) and all(a.startswith("ecr:") for a in actions(statements))
    push = next(s for s in statements if "ecr:PutImage" in json.dumps(s["Action"]))
    assert len(push["Resource"]) == 2


def test_deploy_role_is_scoped_to_the_instance_and_document(template):
    statements = statements_of(template, "repolace-app-deploy")
    send = next(s for s in statements if s["Action"] == "ssm:SendCommand")
    rendered = json.dumps(send["Resource"])
    assert len(send["Resource"]) == 2 and ":instance/" in rendered and "document/AWS-RunShellScript" in rendered and "*" not in rendered
    assert actions([s for s in statements if s is not send]) == {"ssm:GetCommandInvocation"}


def test_secret_reads_are_exactly_the_three_named_secrets(template):
    """Provider keys live only in the worker's secret; each secret is its own resource."""
    reads = [s for s in statements_of(template) if "secretsmanager:GetSecretValue" in json.dumps(s["Action"])]
    assert len(reads) == 1
    rendered = [json.dumps(r) for r in reads[0]["Resource"]]
    assert len(rendered) == 3
    for name in ("repolace-app-api-env", "repolace-app-worker-env", "repolace-app-tunnel-credentials"):
        assert any(f"secret:{name}-??????" in r for r in rendered), name
    assert not any(r.strip('"') == "*" or r.endswith('*"') for r in rendered)


def test_no_site_secret_is_readable_from_the_app_host(template):
    assert "repolace-site-env" not in json.dumps(template.to_json())
    assert "repolace-tunnel-credentials" not in json.dumps(template.to_json())


def test_instance_role_cannot_read_backups_or_wildcard_s3(template):
    s3 = {a for a in actions(statements_of(template)) if a.startswith("s3:")}
    assert s3 == {"s3:PutObject", "s3:AbortMultipartUpload"}


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


def test_wildcard_resources_only_where_the_service_allows_nothing_else(template):
    allowed = {"ecr:GetAuthorizationToken", "ssm:GetCommandInvocation"}
    for s in statements_of(template):
        resources = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
        if "*" in resources:
            assert set(s["Action"] if isinstance(s["Action"], list) else [s["Action"]]) <= allowed, s


def test_resource_names_and_descriptions_have_no_em_dash(template):
    assert "—" not in json.dumps(template.to_json())


def test_user_data_names_the_bootstrap_and_the_public_url_but_holds_no_secret_value(template):
    instance = next(iter(template.find_resources("AWS::EC2::Instance").values()))
    user_data = json.dumps(instance["Properties"]["UserData"])
    assert "bootstrap-app.sh" in user_data and URL in user_data
    # The disks are told apart by volume id (they are NVMe devices with no stable name), so all three are given.
    for key in ("DATA_VOLUME_ID", "DOCKER_VOLUME_ID", "WORKSPACE_VOLUME_ID"):
        assert key in user_data, key
    for name in ("repolace-app-api-env", "repolace-app-worker-env", "repolace-app-tunnel-credentials"):
        assert name in user_data
    assert "TUNNEL_TOKEN" not in user_data and "sk_" not in user_data
    # Minimal: the scripts and compose file are fetched or refreshed on the host, not embedded.
    assert "deploy-app.sh" not in user_data and "compose.app.yml" not in user_data


def test_the_app_stack_exists_only_when_public_url_is_given(tmp_path):
    """The site's `cdk synth` and `cdk deploy` must not change: `app.py` adds the app stack only for `public_url`."""
    import subprocess

    def stacks(*context: str) -> set[str]:
        out = tmp_path / ("cdk.out." + str(len(context)))
        cmd = ["npx", "--yes", "aws-cdk@2", "synth", "--quiet", "--output", str(out), "-c", "ami_id=ami-0123456789abcdef0", *context]
        subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, timeout=300)
        return {p.name.removesuffix(".template.json") for p in out.glob("*.template.json")}

    assert stacks() == {"RepolaceSiteStack"}
    both = stacks("-c", "app_ami_id=ami-0fedcba987654321a", "-c", "public_url=https://app.example.com")
    assert both == {"RepolaceSiteStack", "RepolaceAppStack"}
