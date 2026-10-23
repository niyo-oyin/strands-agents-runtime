"""CloudFormation スタック 1 つ分。共有イメージ、Runtime ごとのリソース、Runtime が必要とする周辺リソース。"""

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
"""Dockerfile がプロジェクトを置くディレクトリ。``STRANDS_RUNTIME_CONFIG`` はこの配下を指す。"""


class StrandsRuntimeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, repo_root: Path, deployments: Deployments) -> None:
        super().__init__(scope, construct_id)
        self.repo_root = repo_root

        # 全 Runtime で 1 つのイメージを共有する（リポジトリの Dockerfile からデプロイ時に linux/arm64 でビルド）。
        artifact = agentcore.AgentRuntimeArtifact.from_asset(
            str(repo_root),
            platform=Platform.LINUX_ARM64,
            exclude=[".git", ".venv", ".env", ".runtime", ".sessions", "cdk.out", "tests", "tests_integ", "docs"],
        )

        # 各 Runtime の runtime.toml からセッションバックエンドを読み、必要な周辺リソースを決める。
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
                # 会話履歴はスタックを消しても残す。
                removal_policy=RemovalPolicy.RETAIN,
            )

        self.memory: agentcore.Memory | None = None
        if "agentcore_memory" in backends.values():
            # 短期記憶（会話イベント）だけを持つ。長期記憶の戦略はエージェント定義側の設定として後から足す。
            self.memory = agentcore.Memory(self, "Memory", expiration_duration=Duration.days(30))

        self.runtimes: dict[str, agentcore.Runtime] = {}
        for spec in deployments.runtime:
            self.runtimes[spec.name] = self._runtime(spec, artifact, backends[spec.name])

    def _runtime(self, spec: RuntimeSpec, artifact: agentcore.AgentRuntimeArtifact, backend: str) -> agentcore.Runtime:
        """Runtime 1 つと、その環境変数・権限を作る。"""
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
            # CloudFormation の動的参照 ``{{resolve:secretsmanager:...}}`` にする。
            # 値はデプロイ時に差し替えられ、テンプレートには入らない。
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
        # L2 コンストラクトが付けるのは ECR・ログ・X-Ray・ワークロード ID の権限だけで、モデル呼び出しは含まれない。
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
