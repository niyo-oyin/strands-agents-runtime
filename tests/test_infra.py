"""The CDK stack synthesizes without an AWS account; assert what the template wires for each Runtime."""

from __future__ import annotations

import pytest

pytest.importorskip("aws_cdk")

from aws_cdk import App
from aws_cdk.assertions import Match, Template

from infra.app import REPO_ROOT, build_app
from infra.spec import Deployments
from infra.stack import StrandsRuntimeStack


def synth(spec: dict) -> Template:
    app = App()
    stack = StrandsRuntimeStack(app, "T", repo_root=REPO_ROOT, deployments=Deployments.model_validate(spec))
    return Template.from_stack(stack)


def test_repo_deployments_synthesize() -> None:
    template = Template.from_stack(build_app().node.find_child("StrandsRuntime"))  # type: ignore[arg-type]
    template.resource_count_is("AWS::BedrockAgentCore::Runtime", 1)


def test_runtime_gets_config_path_bucket_and_secret_reference() -> None:
    template = synth(
        {
            "stack": {"name": "T"},
            "runtime": [
                {
                    "name": "assistant",
                    "config": "deploy/example/runtime.toml",
                    "platform_version": "V2",
                    "env": {"OTEL_SERVICE_NAME": "x"},
                    "secrets": {"OPENROUTER_API_KEY": "strands-runtime/openrouter"},
                }
            ],
        }
    )
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "AgentRuntimeName": "assistant",
            "PlatformVersion": "V2",
            "ProtocolConfiguration": "HTTP",
            "LifecycleConfiguration": {"IdleRuntimeSessionTimeout": 900, "MaxLifetime": 28800},
            "EnvironmentVariables": {
                "STRANDS_RUNTIME_CONFIG": "/app/deploy/example/runtime.toml",
                "OTEL_SERVICE_NAME": "x",
                "SESSION_BUCKET": Match.any_value(),
                "OPENROUTER_API_KEY": Match.string_like_regexp(
                    r"\{\{resolve:secretsmanager:strands-runtime/openrouter.*\}\}"
                ),
            },
        },
    )
    # The execution role may read and write session objects and call Bedrock models.
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        # array_with is order-sensitive: the Bedrock statement is added before the bucket grant.
                        Match.object_like({"Action": Match.array_with(["bedrock:InvokeModelWithResponseStream"])}),
                        Match.object_like({"Action": Match.array_with(["s3:PutObject"])}),
                    ]
                )
            }
        },
    )


def test_two_runtimes_share_one_image_and_bucket() -> None:
    template = synth(
        {
            "stack": {"name": "T"},
            "runtime": [
                {"name": "a", "config": "deploy/example/runtime.toml"},
                {"name": "b", "config": "deploy/example/runtime.toml", "platform_version": "V1"},
            ],
        }
    )
    template.resource_count_is("AWS::BedrockAgentCore::Runtime", 2)
    template.resource_count_is("AWS::S3::Bucket", 1)


def test_spec_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="unique"):
        Deployments.model_validate(
            {"stack": {"name": "T"}, "runtime": [{"name": "a", "config": "x"}, {"name": "a", "config": "x"}]}
        )
    with pytest.raises(ValueError, match="idle_timeout_seconds"):
        Deployments.model_validate(
            {
                "stack": {"name": "T"},
                "runtime": [{"name": "a", "config": "x", "idle_timeout_seconds": 1000, "max_lifetime_seconds": 600}],
            }
        )
