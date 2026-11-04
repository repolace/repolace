"""The public site's AWS footprint: one VM with no inbound ports, an ECR repo, a backup bucket and two
GitHub OIDC roles (build and deploy). Nothing here is named with an em dash; the secrets are referenced by
name only and are created by hand in the console."""

import base64
import io
import tarfile
from pathlib import Path

from aws_cdk import ArnFormat, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

AWS_DIR = Path(__file__).resolve().parent.parent
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
SITE_ENV_SECRET = "repolace-site-env"
TUNNEL_SECRET = "repolace-tunnel-credentials"
PUBLIC_URL = "https://repolace.rounakkumarsingh.com"
#: The files the instance needs before its first deploy; later deploys refresh them from the image.
HOST_FILES = (
    "deploy.sh",
    "backup.sh",
    "backup.service",
    "backup.timer",
    "bootstrap.sh",
    "compose.site.yml",
    "cloudflared/config.yml",
    "site-db/init.sql",
)


def _bundle() -> str:
    """The host files as a base64 tar.gz, small enough for user data (16 KB limit)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in HOST_FILES:
            info = tar.gettarinfo(str(AWS_DIR / name), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0  # reproducible: the same files give the same user data
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            with open(AWS_DIR / name, "rb") as f:
                tar.addfile(info, f)
    return base64.b64encode(buf.getvalue()).decode()


class RepolaceSiteStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        architecture = str(self.node.try_get_context("architecture") or "arm64")
        if architecture not in ("arm64", "x86_64"):
            raise ValueError("context 'architecture' must be arm64 or x86_64")
        default_type = "t4g.small" if architecture == "arm64" else "t3a.small"
        instance_type = str(self.node.try_get_context("instance_type") or default_type)
        github_repo = str(self.node.try_get_context("github_repo") or "repolace/repolace")
        create_oidc = str(self.node.try_get_context("create_oidc_provider") or "true").lower() != "false"

        # --- Registry and backups -----------------------------------------------------------------
        repo = ecr.Repository(
            self,
            "SiteRepo",
            repository_name="repolace-site",
            image_scan_on_push=True,
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
        role.add_to_policy(
            iam.PolicyStatement(sid="ReadWriteBackups", actions=["s3:PutObject", "s3:AbortMultipartUpload", "s3:GetObject"], resources=[backups.arn_for_objects("backups/*")])
        )

        # --- User data: extract the host files, write host.env, run bootstrap.sh -------------------
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -euo pipefail",
            "mkdir -p /opt/repolace",
            f"echo '{_bundle()}' | base64 -d | tar -xz -C /opt/repolace",
            "cat > /opt/repolace/host.env <<'HOSTENV'",
            f"AWS_REGION={self.region}",
            f"ECR_REPO_URI={repo.repository_uri}",
            f"SITE_ENV_SECRET={SITE_ENV_SECRET}",
            f"TUNNEL_SECRET={TUNNEL_SECRET}",
            f"PUBLIC_URL={PUBLIC_URL}",
            f"BACKUP_BUCKET={backups.bucket_name}",
            "HOSTENV",
            "chmod 0644 /opt/repolace/host.env",
            "/opt/repolace/bootstrap.sh",
        )
        cpu = ec2.AmazonLinuxCpuType.ARM_64 if architecture == "arm64" else ec2.AmazonLinuxCpuType.X86_64
        instance = ec2.Instance(
            self,
            "Host",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            instance_type=ec2.InstanceType(instance_type),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(cpu_type=cpu),
            security_group=sg,
            role=role,
            user_data=user_data,
            require_imdsv2=True,
            associate_public_ip_address=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(20, volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True, delete_on_termination=True),
                )
            ],
        )

        # --- GitHub OIDC: two roles, one per token subject ----------------------------------------
        if create_oidc:
            provider = iam.OpenIdConnectProvider(self, "GithubOidc", url=f"https://{GITHUB_OIDC_HOST}", client_ids=["sts.amazonaws.com"])
            provider_arn = provider.open_id_connect_provider_arn
        else:
            provider_arn = f"arn:aws:iam::{self.account}:oidc-provider/{GITHUB_OIDC_HOST}"
            provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(self, "GithubOidc", provider_arn)

        def github_role(construct_id: str, name: str, subject: str, description: str) -> iam.Role:
            return iam.Role(
                self,
                construct_id,
                role_name=name,
                description=description,
                max_session_duration=Duration.hours(1),
                assumed_by=iam.OpenIdConnectPrincipal(
                    provider,
                    {
                        "StringEquals": {
                            f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                            f"{GITHUB_OIDC_HOST}:sub": subject,
                        }
                    },
                ),
            )

        # A job without `environment:` presents ...:ref:refs/heads/main; a job with `environment: production`
        # presents ...:environment:production. Each role trusts exactly one of them.
        build_role = github_role(
            "BuildRole",
            "repolace-site-build",
            f"repo:{github_repo}:ref:refs/heads/main",
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
                ],
                resources=[repo.repository_arn],
            )
        )

        deploy_role = github_role(
            "DeployRole",
            "repolace-site-deploy",
            f"repo:{github_repo}:environment:production",
            "GitHub Actions deploy job: run deploy.sh on the site host through SSM",
        )
        instance_arn = self.format_arn(service="ec2", resource="instance", resource_name=instance.instance_id)
        document_arn = self.format_arn(service="ssm", account="", resource="document", resource_name="AWS-RunShellScript")
        deploy_role.add_to_policy(
            iam.PolicyStatement(sid="RunCommandOnTheHost", actions=["ssm:SendCommand"], resources=[instance_arn, document_arn])
        )
        # These two actions do not support resource-level permissions, so the resource is "*".
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadCommandResults", actions=["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"], resources=["*"]
            )
        )

        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "EcrRepositoryUri", value=repo.repository_uri)
        CfnOutput(self, "BackupBucketName", value=backups.bucket_name)
        CfnOutput(self, "BuildRoleArn", value=build_role.role_arn)
        CfnOutput(self, "DeployRoleArn", value=deploy_role.role_arn)

    def _secret_arn(self, name: str) -> str:
        """Exactly one secret: Secrets Manager appends a hyphen and six random characters to the name."""
        return self.format_arn(service="secretsmanager", resource="secret", resource_name=f"{name}-??????", arn_format=ArnFormat.COLON_RESOURCE_NAME)
