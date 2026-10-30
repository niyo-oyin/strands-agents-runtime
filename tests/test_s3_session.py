"""``s3`` セッションバックエンドを moto のプロセス内 S3 で動かす（ネットワークも外部ストアも不要）。"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from strands_runtime.invoke import InvocationRequest
from strands_runtime.runtime import Runtime
from strands_runtime_client.events import ResultEvent
from tests import fakes
from tests.conftest import write_agent, write_runtime

BUCKET = "strands-sessions"


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)


async def test_history_round_trips_through_s3(tmp_path: Path, aws_env: None) -> None:
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket=BUCKET)
        prefix = f"sessions/{uuid.uuid4()}/"
        write_agent(tmp_path / "agents", "s3agent")
        runtime = Runtime.load(
            write_runtime(tmp_path, f'[session]\nbackend = "s3"\nbucket = "{BUCKET}"\nprefix = "{prefix}"')
        )
        fakes.script(fakes.text("first"), fakes.text("second"))
        session_id = f"s3-{uuid.uuid4()}"

        async def ask(prompt: str) -> ResultEvent:
            request = InvocationRequest(agent="s3agent", prompt=prompt, session_id=session_id)
            result = await runtime.invoker.invoke(request)
            assert isinstance(result, ResultEvent), result
            return result

        assert (await ask("one")).text == "first"
        assert (await ask("two")).text == "second"

        history = fakes.seen_messages()[1]
        assert [block.get("text") for m in history for block in m["content"]] == ["one", "first", "two"]

        listing = boto3.client("s3").list_objects_v2(Bucket=BUCKET, Prefix=prefix)
        keys = [obj["Key"] for obj in listing.get("Contents", [])]
        assert keys and all(session_id in key for key in keys), keys
