from __future__ import annotations

import asyncio
import os

import pytest
from strands.types.exceptions import StorageError

from strands_runtime.definitions.schema import RuntimeConfig
from strands_runtime.factory.nosql_storage import NoSQLStorage
from strands_runtime.invoke import InvocationRequest
from strands_runtime.invoke.shared_store import SharedStore
from strands_runtime.runtime import Runtime
from strands_runtime_client.events import ResultEvent
from tests import fakes
from tests.conftest import write_agent, write_runtime
from tests.test_execution_backends import stores as stores


async def test_large_history_roundtrip_listing_isolation_and_delete(stores):
    one, two = map(NoSQLStorage, stores)
    data = os.urandom(2 * 1024 * 1024 + 17)
    await one.write("session/a/snapshot", data)
    await one.write("session/b/snapshot", b"")
    assert await two.read("session/a/snapshot") == data
    assert await two.read("session/b/snapshot") == b""
    assert await two.list("session/a/") == ["session/a/snapshot"]
    assert len(await two.list("")) == 2
    isolated = NoSQLStorage(SharedStore(stores[0].config.model_copy(update={"namespace": "other"})))
    assert await isolated.list("") == []
    assert await isolated.read("session/a/snapshot") is None
    await two.delete("session/a/snapshot")
    await two.delete("missing")
    assert await one.read("session/a/snapshot") is None
    assert await one.list("") == ["session/b/snapshot"]


@pytest.mark.parametrize("published", [False, True])
async def test_write_failure_only_exposes_complete_snapshot(stores, monkeypatch, published):
    one, two = map(NoSQLStorage, stores)
    await one.write("conversation", b"previous")
    put = one.store.put

    def fail(row, **kwargs):
        if row.get("kind") == "history":
            if published:
                put(row, **kwargs)
            raise OSError("response lost" if published else "write failed")
        return put(row, **kwargs)

    monkeypatch.setattr(one.store, "put", fail)
    data = os.urandom(500_000)
    with pytest.raises(OSError):
        await one.write("conversation", data)
    assert await two.read("conversation") == (data if published else b"previous")


async def test_read_retries_when_old_chunks_are_removed_during_update(stores, monkeypatch):
    one, two = map(NoSQLStorage, stores)
    await one.write("conversation", b"old")
    head_key = one.store.key("history", "conversation")
    original_get = two.store.get
    updated = False

    def get(key):
        nonlocal updated
        row = original_get(key)
        if key == head_key and not updated:
            updated = True
            one._write("conversation", b"new")
        return row

    monkeypatch.setattr(two.store, "get", get)
    assert await two.read("conversation") == b"new"


async def test_incomplete_snapshot_is_never_returned(stores):
    one, two = map(NoSQLStorage, stores)
    await one.write("conversation", b"history")
    head = one.store.get(one.store.key("history", "conversation"))
    one.store.delete(one._chunk_key("conversation", head["generation"], 0))
    with pytest.raises(StorageError, match="incomplete"):
        await two.read("conversation")


async def test_runtime_restores_nosql_history_and_large_job_result_in_fresh_runtime(stores, tmp_path, monkeypatch):
    config = stores[0].config
    write_agent(tmp_path / "agents", "a")
    connection = config.dynamodb or config.cosmos
    fields = "\n".join(f'{k} = "{v}"' for k, v in connection.model_dump(exclude_none=True).items())
    path = write_runtime(
        tmp_path,
        f'''default_agent = "a"
[session]
backend = "local"
[session.history]
backend = "{config.backend}"
[session.history.{config.backend}]
{fields}
''',
    )
    first, second = Runtime.load(path), Runtime.load(path)
    fakes.script(fakes.text("first"), fakes.text("second"))
    assert (await first.invoker.invoke(InvocationRequest(prompt="one", session_id="conversation"))).text == "first"
    assert (await second.invoker.invoke(InvocationRequest(prompt="two", session_id="conversation"))).text == "second"
    assert [block.get("text") for m in fakes.seen_messages()[1] for block in m["content"]] == ["one", "first", "two"]

    async def execute(run):
        yield ResultEvent(agent="a", session_id=run.session.session_id, text="x" * 500_000, stop_reason="end_turn")

    monkeypatch.setattr(first.invoker, "_execute", execute)
    job = await first.jobs.submit(InvocationRequest(prompt="go", session_id="job"))
    async with asyncio.timeout(10):
        async for _ in first.jobs.follow("job", job.job_id):
            pass
    restored = await second.jobs.result("job", job.job_id)
    assert isinstance(restored, ResultEvent) and restored.text == "x" * 500_000


async def test_none_sessions_allow_parallel_same_id_without_history(tmp_path):
    write_agent(tmp_path / "agents", "a")
    path = write_runtime(tmp_path, 'default_agent = "a"\n[session]\nbackend = "none"')
    runtime = Runtime.load(path)
    fakes.script(fakes.text("first"), fakes.text("second"))
    fakes.set_chunk_delay(0.01)
    try:
        results = await asyncio.gather(
            *(runtime.invoker.invoke(InvocationRequest(prompt=prompt, session_id="same")) for prompt in ("one", "two"))
        )
    finally:
        fakes.set_chunk_delay(0)
    assert {r.text for r in results} == {"first", "second"}
    assert all(len(messages) == 1 for messages in fakes.seen_messages())
    assert not runtime.jobs.enabled
    assert not (tmp_path / ".runtime").exists()


def test_agentcore_does_not_enable_memory_implicitly():
    config = RuntimeConfig.model_validate({"session": {"backend": "agentcore"}})
    assert config.session.history.backend == "none"


def test_disabled_sessions_reject_agent_history_override(tmp_path):
    from strands_runtime.errors import DefinitionError

    write_agent(tmp_path / "agents", "a", toml_extra='[session.history]\nbackend = "local"')
    path = write_runtime(tmp_path, '[session]\nbackend = "none"')
    with pytest.raises(DefinitionError, match="cannot save history"):
        Runtime.load(path)
