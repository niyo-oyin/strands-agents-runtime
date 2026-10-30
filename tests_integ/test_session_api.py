"""実HTTPサーバーとOpenRouterで、クライアント・履歴・所有者の契約を確認する。

uv run --env-file .env pytest tests_integ/test_session_api.py -q
"""

from __future__ import annotations

import os
import socket
import threading
import time
import tomllib
from pathlib import Path

import pytest
import uvicorn
from pydantic import BaseModel

from strands_runtime.runtime import Runtime
from strands_runtime.server import create_app
from strands_runtime_client import HttpClient, InvocationError
from strands_runtime_client.events import ResultEvent

pytestmark = pytest.mark.skipif(not os.environ.get("OPENROUTER_API_KEY"), reason="OPENROUTER_API_KEY is not set")
ROOT = Path(__file__).resolve().parents[1]
MODEL = tomllib.loads((ROOT / "examples/feature-check/agents/skilled/agent.toml").read_text(encoding="utf-8"))["model"][
    "id"
]


def messages(text):
    return [{"role": "user", "content": text}]


@pytest.fixture
def live_client(tmp_path):
    agent = tmp_path / "agents" / "memory-check"
    agent.mkdir(parents=True)
    (agent / "agent.toml").write_text(
        f'''name = "memory-check"
[model]
id = "{MODEL}"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
[limits]
timeout_seconds = 60
''',
        encoding="utf-8",
    )
    (agent / "prompt.md").write_text(
        "Follow the user's instructions. Be concise. When asked for the code, return only the latest code "
        "explicitly supplied by a user in this conversation. If none was supplied, return NONE. Do not invent a code.",
        encoding="utf-8",
    )
    config = tmp_path / "runtime.toml"
    config.write_text(
        """[runtime]
agents_dir = "./agents"
default_agent = "memory-check"
[session]
backend = "local"
[session.history]
backend = "local"
dir = "./history"
[jobs]
storage = "local"
dir = "./jobs"
""",
        encoding="utf-8",
    )
    runtime = Runtime.load(config)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(runtime), log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            with HttpClient(f"http://127.0.0.1:{port}", timeout=90) as client:
                yield client
        finally:
            server.should_exit = True
            thread.join(timeout=20)
            assert not thread.is_alive(), "HTTP server did not stop"


def snapshot(directory):
    return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def test_saved_conversation_read_only_and_actor_ownership(live_client, tmp_path):
    first = live_client.invoke(
        messages=messages("The code is AZURE-742. Reply with the code only."),
        actor_id="alice",
        store=True,
        temperature=0,
        max_output_tokens=512,
    )
    assert "AZURE-742" in first.text
    assert first.metrics.total_tokens > 0
    saved = snapshot(tmp_path / "history")
    assert saved
    readonly = live_client.invoke(
        messages=messages("First repeat the previous code. Then change the code to CORAL-193 and repeat the new code."),
        session_id=first.session_id,
        actor_id="alice",
        store=False,
        temperature=0,
        max_output_tokens=512,
    )
    assert "AZURE-742" in readonly.text and "CORAL-193" in readonly.text
    assert snapshot(tmp_path / "history") == saved
    for actor in (None, "bob"):
        with pytest.raises(InvocationError, match="HTTP 400" if actor is None else "HTTP 403"):
            live_client.invoke(
                messages=messages("What is the code?"), session_id=first.session_id, actor_id=actor, store=False
            )
    continued = live_client.invoke(
        messages=messages("What is the code?"),
        session_id=first.session_id,
        actor_id="alice",
        temperature=0,
        max_output_tokens=512,
    )
    assert "AZURE-742" in continued.text and "CORAL-193" not in continued.text


def test_full_messages_and_anonymous_stateless_calls(live_client, tmp_path):
    result = live_client.invoke(
        messages=[
            {"role": "user", "content": "The code is RIVER-825."},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": "What is the code?"},
        ],
        temperature=0,
        max_output_tokens=512,
    )
    assert "RIVER-825" in result.text
    fresh = live_client.invoke(
        messages=messages("What is the code?"), store=False, temperature=0, max_output_tokens=512
    )
    assert fresh.text.strip() == "NONE"
    assert snapshot(tmp_path / "history") == {}


def test_jobs_keep_actor_and_reject_other_actors(live_client, tmp_path):
    job = live_client.submit(
        messages=messages("The code is JOB-361. Reply with the code only."),
        actor_id="alice",
        store=False,
        temperature=0,
        max_output_tokens=512,
    )
    assert job.actor_id == "alice" and job.status().actor_id == "alice"
    other = live_client.job(job.session_id, job.job_id, actor_id="bob")
    for operation in (other.status, other.result, other.cancel, lambda: list(other.follow())):
        with pytest.raises(InvocationError, match="HTTP 403"):
            operation()
    events = list(job.follow())
    assert isinstance(events[-1], ResultEvent) and "JOB-361" in events[-1].text
    reattached = live_client.job(job.session_id, job.job_id, actor_id="alice")
    assert reattached.result().text == events[-1].text
    assert job.cancel().state == "completed"
    assert snapshot(tmp_path / "history") == {}


def test_pydantic_output_and_request_overrides(live_client):
    class Answer(BaseModel):
        total: int

    result = live_client.invoke(
        messages=messages("What is 19 + 23? Return total as an integer."),
        response_format=Answer,
        model=MODEL,
        temperature=0,
        max_output_tokens=512,
        timeout_seconds=60,
        max_turns=3,
        max_total_tokens=4000,
    )
    assert isinstance(result.parsed, Answer) and result.parsed.total == 42
    assert result.metrics.total_tokens > 0
