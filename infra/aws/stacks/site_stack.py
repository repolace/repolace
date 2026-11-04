"""The public site's AWS footprint: one VM with no inbound ports, an ECR repo, a backup bucket and two
GitHub OIDC roles (build and deploy). Nothing here is named with an em dash; the secrets are referenced by
name only and are created by hand in the console."""

import re

import jsii
from aws_cdk import (
    ArnFormat,
    CfnOutput,
    CfnTag,
    Duration,
    RemovalPolicy,
    Size,
    Stack,
    Tags,
)
from aws_cdk import (
    aws_dlm as dlm,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecr as ecr,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_s3 as s3,
)
from constructs import Construct

GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
SITE_ENV_SECRET = "repolace-site-env"
TUNNEL_SECRET = "repolace-tunnel-credentials"
PUBLIC_URL = "https://repolace.rounakkumarsingh.com"
DEPLOY_WORKFLOW = ".github/workflows/deploy-site.yml"
SNAPSHOT_TAG = ("repolace-snapshot", "daily")


@jsii.implements(ec2.IMachineImage)
class PinnedImage:
    """An explicit AMI id. Never the live SSM parameter: a new AMI release must not replace the instance."""

    def __init__(self, ami_id: str) -> None:
        self._ami_id = ami_id

    def get_image(self, scope: Construct) -> ec2.MachineImageConfig:
        return ec2.MachineImageConfig(
            image_id=self._ami_id, os_type=ec2.OperatingSystemType.LINUX, user_data=ec2.UserData.for_linux()
        )


class RepolaceSiteStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        kwargs.setdefault("termination_protection", True)
        super().__init__(scope, construct_id, **kwargs)

        def ctx(key: str, default: str = "") -> str:
            return str(self.node.try_get_context(key) or default)

        architecture = ctx("architecture", "arm64")
        if architecture not in ("arm64", "x86_64"):
            raise ValueError("context 'architecture' must be arm64 or x86_64")
        instance_type = ctx("instance_type", "t4g.small" if architecture == "arm64" else "t3a.small")
        github_repo = ctx("github_repo", "repolace/repolace")
        # The OIDC subject prefix. GitHub issues `repo:<owner>/<repo>` by default, but a repository with immutable subjects
        # (`gh api repos/<owner>/<repo>/actions/oidc/customization/sub`) issues `repo:<owner>@<id>/<repo>@<id>` instead.
        sub_prefix = ctx("github_sub_prefix", f"repo:{github_repo}")
        create_oidc = ctx("create_oidc_provider", "true").lower() != "false"
        ami_id = ctx("ami_id")
        if not re.fullmatch(r"ami-[0-9a-f]{8,17}", ami_id):
            raise ValueError(
                "context 'ami_id' is required (pin an Amazon Linux 2023 AMI of the chosen architecture; "
                "see infra/aws/README.md): -c ami_id=ami-0123456789abcdef0"
            )
        existing_volume_id = ctx("existing_data_volume_id")
        bootstrap_ref = ctx("bootstrap_ref", "main")

        # --- Registry and backups -----------------------------------------------------------------
        repo = ecr.Repository(
            self,
            "SiteRepo",
            repository_name="repolace-site",
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            encryption=ecr.RepositoryEncryption.AES_256,
            lifecycle_rules=[ecr.LifecycleRule(description="Keep the last 10 images", max_image_count=10)],
            removal_policy=RemovalPolicy.RETAIN,
        )
        backups = s3.Bucket(
            self,
            "BackupBucket",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(30),
                    noncurrent_version_expiration=Duration.days(30),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Network: public subnets only, no NAT, no inbound rule anywhere ------------------------
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)],
        )
        sg = ec2.SecurityGroup(self, "HostSg", vpc=vpc, description="Site host: no inbound, outbound only", allow_all_outbound=True)

        # --- Instance role: least privilege --------------------------------------------------------
        role = iam.Role(
            self,
            "HostRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="Site host: SSM, ECR pull, two secrets, backup writes",
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )
        repo.grant_pull(role)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadTheTwoSiteSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[self._secret_arn(SITE_ENV_SECRET), self._secret_arn(TUNNEL_SECRET)],
            )
        )
        # Write only. Restores never read through the instance role (see the README: presigned URL).
        role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteBackups",
                actions=["s3:PutObject", "s3:AbortMultipartUpload"],
                resources=[backups.arn_for_objects("backups/*")],
            )
        )

        # --- The instance. User data is bootstrap only, so editing deploy.sh or the compose file never
        # touches it, and a changed user data never replaces it. -------------------------------------
        instance = ec2.Instance(
            self,
            "Host",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            instance_type=ec2.InstanceType(instance_type),
            machine_image=PinnedImage(ami_id),
            security_group=sg,
            role=role,
            user_data=ec2.UserData.for_linux(),  # filled in below, once the data volume exists
            user_data_causes_replacement=False,
            associate_public_ip_address=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(20, volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True, delete_on_termination=True),
                )
            ],
        )
        # `cdk synth --strict` reports W9010 (hardcoded AMI id) here. That is intended: the id is pinned so a new
        # AMI release can never replace the instance. It is the only warning; the acknowledge API does not cover it.
        # IMDSv2 only, and one hop: a container on the host cannot reach the instance credentials.
        instance.instance.add_property_override(
            "MetadataOptions", {"HttpTokens": "required", "HttpPutResponseHopLimit": 1, "HttpEndpoint": "enabled"}
        )

        # --- Data volume: separate, encrypted, retained; Postgres and the docker volumes live here ------
        if existing_volume_id:
            data_volume_id = existing_volume_id
        else:
            data_volume = ec2.Volume(
                self,
                "DataVolume",
                availability_zone=vpc.public_subnets[0].availability_zone,  # the instance's subnet; never a reference to the instance
                size=Size.gibibytes(20),
                volume_type=ec2.EbsDeviceVolumeType.GP3,
                encrypted=True,
                removal_policy=RemovalPolicy.RETAIN,
            )
            Tags.of(data_volume).add(*SNAPSHOT_TAG)
            data_volume_id = data_volume.volume_id
            snapshot_role = iam.Role(
                self,
                "SnapshotRole",
                assumed_by=iam.ServicePrincipal("dlm.amazonaws.com"),
                description="Data Lifecycle Manager: daily snapshots of the site data volume",
                managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSDataLifecycleManagerServiceRole")],
            )
            dlm.CfnLifecyclePolicy(
                self,
                "DataSnapshots",
                description="Daily snapshot of the site data volume - keep 7",
                state="ENABLED",
                execution_role_arn=snapshot_role.role_arn,
                policy_details=dlm.CfnLifecyclePolicy.PolicyDetailsProperty(
                    resource_types=["VOLUME"],
                    target_tags=[CfnTag(key=SNAPSHOT_TAG[0], value=SNAPSHOT_TAG[1])],
                    schedules=[
                        dlm.CfnLifecyclePolicy.ScheduleProperty(
                            name="daily",
                            copy_tags=True,
                            create_rule=dlm.CfnLifecyclePolicy.CreateRuleProperty(interval=24, interval_unit="HOURS", times=["04:00"]),
                            retain_rule=dlm.CfnLifecyclePolicy.RetainRuleProperty(count=7),
                        )
                    ],
                ),
            )
        ec2.CfnVolumeAttachment(self, "DataAttachment", device="/dev/sdf", instance_id=instance.instance_id, volume_id=data_volume_id)

        instance.user_data.add_commands(
            "set -euo pipefail",
            "mkdir -p /opt/repolace",
            "cat > /opt/repolace/host.env <<'HOSTENV'",
            f"AWS_REGION={self.region}",
            f"ECR_REPO_URI={repo.repository_uri}",
            f"SITE_ENV_SECRET={SITE_ENV_SECRET}",
            f"TUNNEL_SECRET={TUNNEL_SECRET}",
            f"PUBLIC_URL={PUBLIC_URL}",
            f"BACKUP_BUCKET={backups.bucket_name}",
            f"DATA_VOLUME_ID={data_volume_id}",
            f"BOOTSTRAP_REPO={github_repo}",
            f"BOOTSTRAP_REF={bootstrap_ref}",
            "HOSTENV",
            "chmod 0644 /opt/repolace/host.env",
            f"curl -fsSL -o /opt/repolace/bootstrap.sh https://raw.githubusercontent.com/{github_repo}/{bootstrap_ref}/infra/aws/bootstrap.sh",
            "chmod 0755 /opt/repolace/bootstrap.sh",
            "/opt/repolace/bootstrap.sh",
        )

        # --- GitHub OIDC: two roles, one per token subject ----------------------------------------
        if create_oidc:
            provider = iam.OpenIdConnectProvider(self, "GithubOidc", url=f"https://{GITHUB_OIDC_HOST}", client_ids=["sts.amazonaws.com"])
        else:
            provider_arn = f"arn:aws:iam::{self.account}:oidc-provider/{GITHUB_OIDC_HOST}"
            provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(self, "GithubOidc", provider_arn)

        def github_role(construct_id: str, name: str, subject: str, description: str, extra: dict | None = None) -> iam.Role:
            conditions = {f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com", f"{GITHUB_OIDC_HOST}:sub": subject, **(extra or {})}
            return iam.Role(
                self,
                construct_id,
                role_name=name,
                description=description,
                max_session_duration=Duration.hours(1),
                assumed_by=iam.OpenIdConnectPrincipal(provider, {"StringEquals": conditions}),
            )

        # A job without `environment:` presents ...:ref:refs/heads/main; a job with `environment: production`
        # presents ...:environment:production. Each role trusts exactly one of them.
        build_role = github_role(
            "BuildRole",
            "repolace-site-build",
            f"{sub_prefix}:ref:refs/heads/main",
            "GitHub Actions build job: push images to the repolace-site ECR repo",
        )
        build_role.add_to_policy(iam.PolicyStatement(sid="EcrLogin", actions=["ecr:GetAuthorizationToken"], resources=["*"]))
        build_role.add_to_policy(
            iam.PolicyStatement(
                sid="PushToSiteRepo",
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                    "ecr:DescribeImages",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=[repo.repository_arn],
            )
        )

        # The deploy role also pins the workflow file and ref that is running, so another workflow (or the same
        # one from another branch) cannot assume it even from inside the `production` environment.
        deploy_role = github_role(
            "DeployRole",
            "repolace-site-deploy",
            f"{sub_prefix}:environment:production",
            "GitHub Actions deploy job: run deploy.sh on the site host through SSM",
            {f"{GITHUB_OIDC_HOST}:job_workflow_ref": f"{github_repo}/{DEPLOY_WORKFLOW}@refs/heads/main"},
        )
        instance_arn = self.format_arn(service="ec2", resource="instance", resource_name=instance.instance_id)
        document_arn = self.format_arn(service="ssm", account="", resource="document", resource_name="AWS-RunShellScript")
        deploy_role.add_to_policy(
            iam.PolicyStatement(sid="RunCommandOnTheHost", actions=["ssm:SendCommand"], resources=[instance_arn, document_arn])
        )
        # This action does not support resource-level permissions, so the resource is "*".
        deploy_role.add_to_policy(iam.PolicyStatement(sid="ReadCommandResult", actions=["ssm:GetCommandInvocation"], resources=["*"]))

        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "DataVolumeId", value=data_volume_id)
        CfnOutput(self, "EcrRepositoryUri", value=repo.repository_uri)
        CfnOutput(self, "BackupBucketName", value=backups.bucket_name)
        CfnOutput(self, "BuildRoleArn", value=build_role.role_arn)
        CfnOutput(self, "DeployRoleArn", value=deploy_role.role_arn)

    def _secret_arn(self, name: str) -> str:
        """Exactly one secret: Secrets Manager appends a hyphen and six random characters to the name."""
        return self.format_arn(service="secretsmanager", resource="secret", resource_name=f"{name}-??????", arn_format=ArnFormat.COLON_RESOURCE_NAME)

