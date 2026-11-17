"""The app's AWS footprint: the API, the task worker, their Postgres/Redis/RabbitMQ and the Cloudflare
Tunnel connector on ONE VM, separate from the public site's. Nothing here is named with an em dash; the
secrets are referenced by name only and are created by hand in the console.

Why its own VM and stack, and not the site's: this host holds the model-provider keys and the GitHub App
private key, and it runs model-written code in rootless Docker containers. The public site host holds
neither and must not share a blast radius with it.

Differences from `RepolaceSiteStack` (see `.claude/rules/api-and-ui.md`, "Access", and `docs/operations.md`):
- Ubuntu 24.04 on x86_64, pinned by AMI id: Docker's rootless install is documented for Ubuntu, not for
  Amazon Linux 2023 (its docker package ships no rootless setup tool).
- Three extra EBS volumes: the retained data volume (Postgres and the broker), the rootless Docker data
  root (images and containers, rebuildable), and the task workspace (a small ext4 volume, which gives the
  size and inode limits the sandbox needs: see "Deployment requirements for the sandbox host" in CLAUDE.md).
- Two env secrets, one per process, so the model-provider keys exist only in the worker's.
- The GitHub OIDC provider is imported, not created: the site stack owns it and an account has one.
"""

import re

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
from aws_cdk import aws_dlm as dlm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from stacks.site_stack import GITHUB_OIDC_HOST, PinnedImage

API_ENV_SECRET = "repolace-app-api-env"
WORKER_ENV_SECRET = "repolace-app-worker-env"
TUNNEL_SECRET = "repolace-app-tunnel-credentials"
DEPLOY_WORKFLOW = ".github/workflows/deploy-app.yml"
#: The deploy job's GitHub environment. Its own name, so approving a site deploy never approves an app deploy.
DEPLOY_ENVIRONMENT = "production-app"
APP_SNAPSHOT_TAG = ("repolace-app-snapshot", "daily")
DEFAULT_INSTANCE_TYPE = "m6i.large"  # 2 vCPU, 8 GiB, not burstable: a task runs the test suite for minutes at a time


class RepolaceAppStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        kwargs.setdefault("termination_protection", True)
        super().__init__(scope, construct_id, **kwargs)

        def ctx(key: str, default: str = "") -> str:
            return str(self.node.try_get_context(key) or default)

        instance_type = ctx("app_instance_type", DEFAULT_INSTANCE_TYPE)
        github_repo = ctx("github_repo", "repolace/repolace")
        sub_prefix = ctx("github_sub_prefix", f"repo:{github_repo}")
        public_url = ctx("public_url")
        if not re.fullmatch(r"https://[a-z0-9.-]+", public_url):
            raise ValueError(
                "context 'public_url' is required, the app's public origin: -c public_url=https://app.example.com "
                "(it becomes API_BASE_URL, the OAuth callback origin and the cookie 'Secure' flag)"
            )
        ami_id = ctx("app_ami_id")
        if not re.fullmatch(r"ami-[0-9a-f]{8,17}", ami_id):
            raise ValueError(
                "context 'app_ami_id' is required (pin a Canonical Ubuntu 24.04 LTS amd64 AMI; see infra/aws/README.md): "
                "-c app_ami_id=ami-0123456789abcdef0"
            )
        sizes = {
            "data_gib": int(ctx("data_volume_gib", "30")),
            "docker_gib": int(ctx("docker_volume_gib", "80")),
            "workspace_gib": int(ctx("workspace_volume_gib", "30")),
        }
        existing_data_volume_id = ctx("app_existing_data_volume_id")
        bootstrap_ref = ctx("app_bootstrap_ref", "main")  # pin a commit sha for a real deploy: this script runs as root

        # --- Registries and backups ---------------------------------------------------------------
        def image_repo(construct_id: str, name: str, keep: int) -> ecr.Repository:
            return ecr.Repository(
                self,
                construct_id,
                repository_name=name,
                image_scan_on_push=True,
                image_tag_mutability=ecr.TagMutability.IMMUTABLE,
                encryption=ecr.RepositoryEncryption.AES_256,
                lifecycle_rules=[ecr.LifecycleRule(description=f"Keep the last {keep} images", max_image_count=keep)],
                removal_policy=RemovalPolicy.RETAIN,
            )

        api_repo = image_repo("ApiRepo", "repolace-api", 10)
        worker_repo = image_repo("WorkerRepo", "repolace-worker", 5)  # carries torch: large, so fewer kept
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
        sg = ec2.SecurityGroup(self, "HostSg", vpc=vpc, description="App host: no inbound, outbound only", allow_all_outbound=True)

        # --- Instance role: least privilege --------------------------------------------------------
        role = iam.Role(
            self,
            "HostRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="App host: SSM, ECR pull, three secrets, backup writes",
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )
        api_repo.grant_pull(role)
        worker_repo.grant_pull(role)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadTheThreeAppSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[self._secret_arn(n) for n in (API_ENV_SECRET, WORKER_ENV_SECRET, TUNNEL_SECRET)],
            )
        )
        # Write only: a restore never reads through the instance role (presigned URL, as for the site).
        role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteBackups",
                actions=["s3:PutObject", "s3:AbortMultipartUpload"],
                resources=[backups.arn_for_objects("backups/*")],
            )
        )

        # --- The instance. User data is bootstrap only, so editing deploy files never replaces it ----
        instance = ec2.Instance(
            self,
            "Host",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            instance_type=ec2.InstanceType(instance_type),
            machine_image=PinnedImage(ami_id),
            security_group=sg,
            role=role,
            user_data=ec2.UserData.for_linux(),  # filled in below, once the volumes exist
            user_data_causes_replacement=False,
            associate_public_ip_address=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",  # Ubuntu's root device name (Amazon Linux uses /dev/xvda)
                    volume=ec2.BlockDeviceVolume.ebs(
                        20, volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True, delete_on_termination=True
                    ),
                )
            ],
        )
        # IMDSv2 only, one hop. This stops a rootful bridge container (the forwarded packet's TTL drops), and
        # nothing else: rootless Docker's user-space networking re-originates connections from the host's own
        # namespace, so a rootless container can still reach 169.254.169.254. `bootstrap-app.sh` must therefore
        # also drop that address for the Docker user (host firewall). See README, "The app host".
        instance.instance.add_property_override(
            "MetadataOptions", {"HttpTokens": "required", "HttpPutResponseHopLimit": 1, "HttpEndpoint": "enabled"}
        )
        az = vpc.public_subnets[0].availability_zone  # the instance's subnet; never a reference to the instance

        # --- Data volume: retained and snapshotted. Postgres, Redis and RabbitMQ bind-mount from here ----
        if existing_data_volume_id:
            data_volume_id = existing_data_volume_id
        else:
            data_volume = ec2.Volume(
                self,
                "DataVolume",
                availability_zone=az,
                size=Size.gibibytes(sizes["data_gib"]),
                volume_type=ec2.EbsDeviceVolumeType.GP3,
                encrypted=True,
                removal_policy=RemovalPolicy.RETAIN,
            )
            Tags.of(data_volume).add(*APP_SNAPSHOT_TAG)
            data_volume_id = data_volume.volume_id
            snapshot_role = iam.Role(
                self,
                "SnapshotRole",
                assumed_by=iam.ServicePrincipal("dlm.amazonaws.com"),
                description="Data Lifecycle Manager: daily snapshots of the app data volume",
                managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSDataLifecycleManagerServiceRole")],
            )
            dlm.CfnLifecyclePolicy(
                self,
                "DataSnapshots",
                description="Daily snapshot of the app data volume - keep 7",
                state="ENABLED",
                execution_role_arn=snapshot_role.role_arn,
                policy_details=dlm.CfnLifecyclePolicy.PolicyDetailsProperty(
                    resource_types=["VOLUME"],
                    target_tags=[CfnTag(key=APP_SNAPSHOT_TAG[0], value=APP_SNAPSHOT_TAG[1])],
                    schedules=[
                        dlm.CfnLifecyclePolicy.ScheduleProperty(
                            name="daily",
                            copy_tags=True,
                            create_rule=dlm.CfnLifecyclePolicy.CreateRuleProperty(interval=24, interval_unit="HOURS", times=["04:30"]),
                            retain_rule=dlm.CfnLifecyclePolicy.RetainRuleProperty(count=7),
                        )
                    ],
                ),
            )
        ec2.CfnVolumeAttachment(self, "DataAttachment", device="/dev/sdf", instance_id=instance.instance_id, volume_id=data_volume_id)

        # --- The two scratch volumes: rebuildable, so deleted with the stack and never snapshotted ----
        volume_ids: dict[str, str] = {}
        for volume_id, device, gib in (("DockerVolume", "/dev/sdg", sizes["docker_gib"]), ("WorkspaceVolume", "/dev/sdh", sizes["workspace_gib"])):
            volume = ec2.Volume(
                self,
                volume_id,
                availability_zone=az,
                size=Size.gibibytes(gib),
                volume_type=ec2.EbsDeviceVolumeType.GP3,
                encrypted=True,
                removal_policy=RemovalPolicy.DESTROY,
            )
            volume_ids[volume_id] = volume.volume_id
            ec2.CfnVolumeAttachment(self, f"{volume_id}Attachment", device=device, instance_id=instance.instance_id, volume_id=volume.volume_id)

        instance.user_data.add_commands(
            "set -euo pipefail",
            "mkdir -p /opt/repolace",
            "cat > /opt/repolace/host.env <<'HOSTENV'",
            f"AWS_REGION={self.region}",
            f"API_ECR_REPO_URI={api_repo.repository_uri}",
            f"WORKER_ECR_REPO_URI={worker_repo.repository_uri}",
            f"API_ENV_SECRET={API_ENV_SECRET}",
            f"WORKER_ENV_SECRET={WORKER_ENV_SECRET}",
            f"TUNNEL_SECRET={TUNNEL_SECRET}",
            f"PUBLIC_URL={public_url}",
            f"BACKUP_BUCKET={backups.bucket_name}",
            f"DATA_VOLUME_ID={data_volume_id}",
            f"DOCKER_VOLUME_ID={volume_ids['DockerVolume']}",
            f"WORKSPACE_VOLUME_ID={volume_ids['WorkspaceVolume']}",
            f"BOOTSTRAP_REPO={github_repo}",
            f"BOOTSTRAP_REF={bootstrap_ref}",
            "HOSTENV",
            "chmod 0644 /opt/repolace/host.env",
            f"curl -fsSL -o /opt/repolace/bootstrap-app.sh https://raw.githubusercontent.com/{github_repo}/{bootstrap_ref}/infra/aws/bootstrap-app.sh",
            "chmod 0755 /opt/repolace/bootstrap-app.sh",
            "/opt/repolace/bootstrap-app.sh",
        )

        # --- GitHub OIDC: the provider is the site stack's; two roles of ours, one per token subject ----
        provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            self, "GithubOidc", f"arn:aws:iam::{self.account}:oidc-provider/{GITHUB_OIDC_HOST}"
        )

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

        build_role = github_role(
            "BuildRole",
            "repolace-app-build",
            f"{sub_prefix}:ref:refs/heads/main",
            "GitHub Actions build job: push images to the repolace-api and repolace-worker ECR repos",
            # The build job has no environment, so its `sub` is the same as every job on main without one,
            # including the site's. Pinning the workflow file is what keeps those out.
            {f"{GITHUB_OIDC_HOST}:job_workflow_ref": f"{github_repo}/{DEPLOY_WORKFLOW}@refs/heads/main"},
        )
        build_role.add_to_policy(iam.PolicyStatement(sid="EcrLogin", actions=["ecr:GetAuthorizationToken"], resources=["*"]))
        build_role.add_to_policy(
            iam.PolicyStatement(
                sid="PushToAppRepos",
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
                resources=[api_repo.repository_arn, worker_repo.repository_arn],
            )
        )

        deploy_role = github_role(
            "DeployRole",
            "repolace-app-deploy",
            f"{sub_prefix}:environment:{DEPLOY_ENVIRONMENT}",
            "GitHub Actions deploy job: run the app deploy script on the app host through SSM",
            {f"{GITHUB_OIDC_HOST}:job_workflow_ref": f"{github_repo}/{DEPLOY_WORKFLOW}@refs/heads/main"},
        )
        instance_arn = self.format_arn(service="ec2", resource="instance", resource_name=instance.instance_id)
        document_arn = self.format_arn(service="ssm", account="", resource="document", resource_name="AWS-RunShellScript")
        deploy_role.add_to_policy(
            iam.PolicyStatement(sid="RunCommandOnTheHost", actions=["ssm:SendCommand"], resources=[instance_arn, document_arn])
        )
        deploy_role.add_to_policy(iam.PolicyStatement(sid="ReadCommandResult", actions=["ssm:GetCommandInvocation"], resources=["*"]))

        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "DataVolumeId", value=data_volume_id)
        CfnOutput(self, "ApiRepositoryUri", value=api_repo.repository_uri)
        CfnOutput(self, "WorkerRepositoryUri", value=worker_repo.repository_uri)
        CfnOutput(self, "BackupBucketName", value=backups.bucket_name)
        CfnOutput(self, "BuildRoleArn", value=build_role.role_arn)
        CfnOutput(self, "DeployRoleArn", value=deploy_role.role_arn)

    def _secret_arn(self, name: str) -> str:
        """Exactly one secret: Secrets Manager appends a hyphen and six random characters to the name."""
        return self.format_arn(service="secretsmanager", resource="secret", resource_name=f"{name}-??????", arn_format=ArnFormat.COLON_RESOURCE_NAME)
