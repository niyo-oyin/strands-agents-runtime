"""One CloudFormation stack: the shared image, per-Runtime resources, and what each Runtime needs around it."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk.aws_ecr_assets import Platform
from constructs import Construct

from infra.spec import Deployments, RuntimeSpec, session_backend

IMAGE_APP_DIR = "/app"
"""Where the Dockerfile puts the project; ``STRANDS_RUNTIME_CONFIG`` points inside it."""


class StrandsRuntimeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, repo_root: Path, deployments: Deployments) -> None:
        super().__init__(scope, construct_id)
        self.repo_root = repo_root

        # One image for every Runtime (linux/arm64, built from the repo's Dockerfile at deploy time).
        artifact = agentcore.AgentRuntimeArtifact.from_asset(
            str(repo_root),
            platform=Platform.LINUX_ARM64,
            exclude=[".git", ".venv", ".env", ".runtime", ".sessions", "cdk.out", "tests", "tests_integ", "docs"],
        )

        backends = {spec.name: session_backend(repo_root, spec.config) for spec in deployments.runtime}

        self.session_bucket: s3.Bucket | None = None
        if "s3" in backends.values():
            self.session_bucket = s3.Bucket(
                self,
                "SessionBucket",
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                encryption=s3.BucketEncryption.S3_MANAGED,
                enforce_ssl=True,
                versioned=False,
                removal_policy=RemovalPolicy.RETAIN,
            )

        self.memory: agentcore.Memory | None = None
        if "agentcore_memory" in backends.values():
            # Short-term memory only; long-term strategies are an agent-definition concern (phase 3).
            self.memory = agentcore.Memory(self, "Memory", expiration_duration=Duration.days(30))

        self.runtimes: dict[str, agentcore.Runtime] = {}
        for spec in deployments.runtime:
            self.runtimes[spec.name] = self._runtime(spec, artifact, backends[spec.name])

    def _runtime(self, spec: RuntimeSpec, artifact: agentcore.AgentRuntimeArtifact, backend: str) -> agentcore.Runtime:
        env: dict[str, str] = {
            "STRANDS_RUNTIME_CONFIG": f"{IMAGE_APP_DIR}/{spec.config}",
            **spec.env,
        }
        if backend == "s3":
            assert self.session_bucket is not None
            env["SESSION_BUCKET"] = self.session_bucket.bucket_name
        if backend == "agentcore_memory":
            assert self.memory is not None
            env["AGENTCORE_MEMORY_ID"] = self.memory.memory_id
        for name, secret_name in spec.secrets.items():
            # A CloudFormation dynamic reference: the value is substituted at deploy time, never in the template.
            env[name] = SecretValue.secrets_manager(secret_name).unsafe_unwrap()

        runtime = agentcore.Runtime(
            self,
            f"Runtime-{spec.name}",
            runtime_name=spec.name,
            agent_runtime_artifact=artifact,
            protocol_configuration=agentcore.ProtocolType.HTTP,
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_iam(),
            platform_version=agentcore.PlatformVersion.of(spec.platform_version),
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.seconds(spec.idle_timeout_seconds),
                max_lifetime=Duration.seconds(spec.max_lifetime_seconds),
            ),
            environment_variables=env,
            tracing_enabled=True,
            description=f"strands-agents-runtime ({spec.config})",
        )
        # The L2 construct grants ECR, logs, X-Ray and workload identity, but not model access.
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:ApplyGuardrail"],
                resources=["*"],
            )
        )
        if backend == "s3":
            assert self.session_bucket is not None
            self.session_bucket.grant_read_write(runtime.role)
        if backend == "agentcore_memory":
            assert self.memory is not None
            self.memory.grant_full_access(runtime.role)

        CfnOutput(self, f"{spec.name}RuntimeArn", value=runtime.agent_runtime_arn)
        CfnOutput(self, f"{spec.name}RuntimeId", value=runtime.agent_runtime_id)
        return runtime
