from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import Mock

import pytest
from strands import Agent

from strands_runtime.definitions.schema import HistorySpec
from strands_runtime.errors import InvalidRequestError, SessionAccessError
from strands_runtime.factory.history_access import authorize_memory
from strands_runtime.factory.sessions import SessionContext, SessionManagerFactory
from strands_runtime.invoke import InvocationRequest
from strands_runtime.runtime import Runtime
from strands_runtime_client.events import ResultEvent
from tests import fakes
from tests.conftest import write_agent, write_runtime
from tests.test_execution_backends import stores as stores


def request(text, **kwargs):
    return InvocationRequest(messages=[{"role": "user", "content": text}], **kwargs)


def texts():
    return [b["text"] for m in fakes.seen_messages()[-1] for b in m["content"] if "text" in b]


async def check_read_only(runtime, reload):
    fakes.script(fakes.text("saved answer"), fakes.text("temporary answer"), fakes.text("final answer"))
    created = await runtime.invoker.invoke(request("saved question", store=True, actor_id="alice"))
    assert isinstance(created, ResultEvent)
    second = await reload.invoker.invoke(
        request("temporary question", session_id=created.session_id, actor_id="alice", store=False)
    )
    assert isinstance(second, ResultEvent)
    assert texts() == ["saved question", "saved answer", "temporary question"]
    await runtime.invoker.invoke(request("next", session_id=created.session_id, actor_id="alice"))
    assert texts() == ["saved question", "saved answer", "next"]
    for store in (True, False):
        with pytest.raises(SessionAccessError):
            await reload.invoker.invoke(request("steal", session_id=created.session_id, actor_id="bob", store=store))


async def test_read_only_local_history_and_owner_survive_runtime_reload(runtime):
    await check_read_only(runtime, Runtime.load(runtime.config_path))


async def test_read_only_s3_history(tmp_path):
    import boto3
    from moto import mock_aws

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="history")
        write_agent(tmp_path / "agents", "a")
        path = write_runtime(
            tmp_path, 'default_agent = "a"\n[session.history]\nbackend = "s3"\nbucket = "history"\nregion = "us-east-1"'
        )
        await check_read_only(Runtime.load(path), Runtime.load(path))


async def test_read_only_nosql_history_and_owner(stores, tmp_path):
    config = stores[0].config
    write_agent(tmp_path / "agents", "a")
    connection = config.dynamodb or config.cosmos
    fields = "\n".join(f'{k} = "{v}"' for k, v in connection.model_dump(exclude_none=True).items())
    path = write_runtime(
        tmp_path,
        f'''default_agent = "a"
[session]
backend = "{config.backend}"
[session.{config.backend}]
{fields}
[session.history]
backend = "{config.backend}"
[session.history.{config.backend}]
{fields}
''',
    )
    await check_read_only(Runtime.load(path), Runtime.load(path))


async def test_default_is_stateless_and_saved_history_requires_actor(runtime):
    runtime.config.session.default_actor_id = None
    fakes.script(fakes.text("answer"), fakes.text("saved"))
    assert isinstance(await runtime.invoker.invoke(request("one")), ResultEvent)
    for options in ({"store": True}, {"session_id": "existing", "store": False}):
        with pytest.raises(InvalidRequestError, match="actor_id"):
            await runtime.invoker.invoke(request("two", **options))
    runtime.config.session.default_actor_id = "single-user"
    saved = await runtime.invoker.invoke(request("save", store=True))
    with pytest.raises(SessionAccessError):
        await runtime.invoker.invoke(request("other", session_id=saved.session_id, actor_id="other"))


async def test_save_complete_history_as_new_conversation_and_reject_reused_transport_id(runtime):
    fakes.script(fakes.text("saved answer"), fakes.text("continued"))
    created = await runtime.invoker.invoke(
        InvocationRequest(
            messages=[
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "new question"},
            ],
            actor_id="alice",
            store=True,
        ),
        session_id="transport-id",
    )
    assert isinstance(created, ResultEvent)
    with pytest.raises(InvalidRequestError, match="specify session_id"):
        await runtime.invoker.invoke(request("overwrite", actor_id="alice", store=True), session_id="transport-id")
    await runtime.invoker.invoke(request("continue", session_id=created.session_id, actor_id="alice"))
    assert texts() == ["old question", "old answer", "new question", "saved answer", "continue"]


async def test_explicit_storage_requires_history_backend(runtime):
    runtime.config.session.history = HistorySpec(backend="none")
    fakes.script(fakes.text("stateless"))
    with pytest.raises(InvalidRequestError, match="history backend"):
        await runtime.invoker.invoke(request("save", store=True, actor_id="alice"))
    assert isinstance(await runtime.invoker.invoke(request("no persistence")), ResultEvent)


@pytest.mark.parametrize("operation", ["status", "result", "follow", "cancel"])
async def test_jobs_check_actor_for_every_operation_including_remote_and_finished(runtime, operation):
    fakes.script(fakes.text("done"))
    job = await runtime.jobs.submit(request("job", actor_id="alice", store=False))
    assert job.actor_id == "alice"
    remote = Runtime.load(runtime.config_path)

    async def reject(manager):
        with pytest.raises(SessionAccessError):
            call = getattr(manager, operation)(job.session_id, job.job_id, actor_id="bob")
            if operation == "follow":
                await anext(call)
            else:
                await call

    await reject(runtime.jobs)
    await reject(remote.jobs)
    async with asyncio.timeout(10):
        async for _ in runtime.jobs.follow(job.session_id, job.job_id, actor_id="alice"):
            pass
    await reject(remote.jobs)
    assert (await remote.jobs.result(job.session_id, job.job_id, actor_id="alice")).text == "done"


class MemoryAPI:
    def __init__(self):
        self.meta = Mock(region_name="us-east-1")
        self.events = []
        self.searches = []

    def create_event(self, **params):
        event = {**copy.deepcopy(params), "eventId": f"event-{len(self.events)}"}
        self.events.append(event)
        return {"event": event}

    def list_events(self, **params):
        return {
            "events": [
                e
                for e in reversed(self.events)
                if all(e[k] == params[k] for k in ("memoryId", "actorId", "sessionId"))
                and all(
                    e.get("metadata", {}).get(f["left"]["metadataKey"]) == f["right"]["metadataValue"]
                    for f in params.get("filter", {}).get("eventMetadata", [])
                )
            ][: params.get("maxResults", 100)]
        }

    def retrieve_memory_records(self, **params):
        self.searches.append(params)
        return {"memoryRecordSummaries": [{"content": {"text": "prefers Japanese"}, "score": 0.9}]}


async def test_memory_store_false_retrieves_long_term_context_without_writing(tmp_path, monkeypatch):
    api = MemoryAPI()
    session = Mock()
    session.region_name = "us-east-1"
    session.client.return_value = api
    monkeypatch.setattr("boto3.Session", Mock(return_value=session))
    factory = SessionManagerFactory(tmp_path)
    spec = HistorySpec(
        backend="agentcore_memory", memory_id="test-memory", retrieval={"/preferences/{actorId}/": {"top_k": 3}}
    )
    manager = factory.build(spec, SessionContext("session", actor_id="alice", store=False, restore=False))
    fakes.script(fakes.text("answer"))
    agent = Agent(model=fakes.make_model(), session_manager=manager, callback_handler=None)
    await agent.invoke_async("hello")
    assert api.events == []
    assert api.searches[0]["searchCriteria"] == {"searchQuery": "hello", "topK": 3}
    assert "alice" in json.dumps(api.searches[0])
    assert "<user_context>prefers Japanese</user_context>" in texts()


def test_memory_owner_is_persistent_metadata_and_read_only_does_not_create_it():
    api = MemoryAPI()
    authorize_memory(api, "memory", "s", "alice", store=False)
    assert api.events == []
    authorize_memory(api, "memory", "s", "alice", store=True)
    authorize_memory(api, "memory", "s", "alice", store=False)
    with pytest.raises(SessionAccessError):
        authorize_memory(api, "memory", "s", "bob", store=False)


async def test_memory_read_only_restores_history_without_saving_changes(tmp_path, monkeypatch):
    api = MemoryAPI()
    boto_session = Mock(region_name="us-east-1")
    boto_session.client.return_value = api
    monkeypatch.setattr("boto3.Session", Mock(return_value=boto_session))
    factory = SessionManagerFactory(tmp_path)
    spec = HistorySpec(backend="agentcore_memory", memory_id="test-memory")
    fakes.script(fakes.text("saved answer"), fakes.text("temporary answer"), fakes.text("final answer"))

    async def invoke(text, *, store, restore):
        manager = factory.build(spec, SessionContext("session", actor_id="alice", store=store, restore=restore))
        agent = Agent(model=fakes.make_model(), agent_id="a", session_manager=manager, callback_handler=None)
        await agent.invoke_async(text)

    await invoke("saved question", store=True, restore=False)
    before = copy.deepcopy(api.events)
    assert before
    await invoke("temporary question", store=False, restore=True)
    assert texts() == ["saved question", "saved answer", "temporary question"]
    assert api.events == before
    await invoke("next", store=True, restore=True)
    assert texts() == ["saved question", "saved answer", "next"]


def test_http_ownership_errors_and_removed_prompt(runtime):
    from starlette.testclient import TestClient

    from strands_runtime.server import create_app

    runtime.config.session.default_actor_id = None
    fakes.script(fakes.text("saved"))
    with TestClient(create_app(runtime)) as client:
        old = client.post("/invocations", json={"prompt": "old"})
        assert old.status_code == 400
        missing = client.post("/invocations", json=request("save", store=True).model_dump(exclude_none=True))
        assert missing.status_code == 400 and "actor_id" in missing.text
        saved = client.post(
            "/invocations",
            json=request("save", store=True, actor_id="alice", stream=False).model_dump(exclude_none=True),
        )
        assert saved.status_code == 200
        sid = saved.json()["session_id"]
        forbidden = client.post(
            "/invocations",
            json=request("read", session_id=sid, actor_id="bob", store=False).model_dump(exclude_none=True),
        )
        assert forbidden.status_code == 403


def test_cli_messages_store_and_actor(runtime, capsys, monkeypatch):
    from strands_runtime.cli import main

    # CLIのプロセス全体向けログ設定を、同一プロセスの他テストへ持ち越さない。
    monkeypatch.setattr("strands_runtime.cli.configure_logging", lambda *args, **kwargs: None)
    fakes.script(fakes.text("saved"), fakes.text("readonly"))
    base = [
        "-c",
        str(runtime.config_path),
        "run",
        "echoer",
        "--messages",
        '[{"role":"user","content":"hi"}]',
        "--actor-id",
        "alice",
        "--json",
    ]
    assert main([*base, "--store"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    sid = events[-1]["session_id"]
    assert main([*base, "--session-id", sid, "--no-store"]) == 0
    assert texts() == ["hi", "saved", "hi"]
