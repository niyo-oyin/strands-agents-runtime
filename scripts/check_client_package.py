"""クライアントの wheel を独立環境にインストールし、extras ごとの依存分離を確認する。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = """
import importlib.util
import sys
from strands_runtime_client import AgentCoreClient, HttpClient, new_session_id
from strands_runtime_client.events import TextEvent, parse_event

extra = sys.argv[1]
for name in ("strands_runtime", "strands", "strands_harness", "bedrock_agentcore", "aws_cdk", "openai"):
    assert importlib.util.find_spec(name) is None, name
assert "httpx" not in sys.modules and "boto3" not in sys.modules
assert parse_event('{"type":"text","text":"hello"}') == TextEvent(text="hello")
assert len(new_session_id()) == 36
for name, selected in (("httpx", "http"), ("boto3", "agentcore")):
    assert (importlib.util.find_spec(name) is not None) == (extra == selected), name
if extra != "http":
    try:
        HttpClient("https://runtime.example")
    except ImportError as exc:
        assert "[http]" in str(exc)
    else:
        raise AssertionError("missing HTTP extra was not reported")
else:
    import httpx
    import json
    from pydantic import BaseModel
    class Answer(BaseModel):
        answer: str
    def respond(request):
        payload = json.loads(request.content)
        assert payload["messages"] == [{"role": "user", "content": "hi"}]
        assert payload["temperature"] == 0 and payload["max_output_tokens"] == 100
        assert payload["timeout_seconds"] == 30 and payload["max_turns"] == 2
        fmt = payload["response_format"]
        assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
        assert fmt["json_schema"]["schema"]["additionalProperties"] is False
        return httpx.Response(200, json={
            "type": "result", "agent": "assistant", "session_id": "s", "stop_reason": "end_turn",
            "text": json.dumps({"answer": "hello"}),
        })
    transport = httpx.MockTransport(respond)
    with HttpClient("https://runtime.example", transport=transport) as client:
        result = client.invoke(messages=[{"role": "user", "content": "hi"}], temperature=0,
                               max_output_tokens=100, timeout_seconds=30, max_turns=2, response_format=Answer)
        assert isinstance(result.parsed, Answer) and result.parsed.answer == "hello"
if extra != "agentcore":
    try:
        AgentCoreClient("arn:unused")
    except ImportError as exc:
        assert "[agentcore]" in str(exc)
    else:
        raise AssertionError("missing AgentCore extra was not reported")
else:
    import boto3
    session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
    AgentCoreClient("arn:unused", boto_session=session)
print("isolated client OK:", extra or "core", sys.version.split()[0])
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python executable or version to verify")
    args = parser.parse_args()
    scratch = ROOT / ".runtime"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="client-package-", dir=scratch) as directory:
        work = Path(directory)
        subprocess.run(
            ["uv", "build", "--package", "strands-agents-runtime-client", "--out-dir", str(work / "dist")],
            cwd=ROOT,
            check=True,
        )
        [wheel] = (work / "dist").glob("*.whl")
        for extra in ("", "http", "agentcore"):
            venv = work / (extra or "core")
            subprocess.run(["uv", "venv", "--python", args.python, str(venv)], cwd=ROOT, check=True)
            executable = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            requirement = str(wheel) + (f"[{extra}]" if extra else "")
            subprocess.run(["uv", "pip", "install", "--python", str(executable), requirement], cwd=ROOT, check=True)
            subprocess.run([str(executable), "-I", "-c", PROBE, extra], cwd=work, check=True)


if __name__ == "__main__":
    main()
