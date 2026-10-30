from __future__ import annotations

import asyncio
import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from strands_runtime.definitions.schema import ExecutionSection, RuntimeConfig, SessionSection
from strands_runtime.errors import CapacityError, DefinitionError, SessionBusyError
from strands_runtime.invoke import InvocationRequest
from strands_runtime.invoke.admission import AdmissionController
from strands_runtime.invoke.shared_store import SharedStore
from strands_runtime.jobs import manager as manager_module
from strands_runtime.runtime import Runtime
from tests import fakes
from tests.conftest import write_agent, write_runtime


class CosmosContainer:
    """条件付き書き込みを原子的に行う、SDK境界のテスト用コンテナ。"""

    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()
        self.version = 0

    def read(self):
        return {"partitionKey": {"paths": ["/id"]}}

    def read_item(self, item, partition_key):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        assert item == partition_key
        with self.lock:
            if item not in self.rows:
                raise CosmosResourceNotFoundError(status_code=404)
            return copy.deepcopy(self.rows[item])

    def create_item(self, row):
        from azure.cosmos.exceptions import CosmosResourceExistsError

        with self.lock:
            if row["id"] in self.rows:
                raise CosmosResourceExistsError(status_code=409)
            self._put(row)

    def upsert_item(self, row):
        with self.lock:
            self._put(row)

    def _put(self, row):
        self.version += 1
        self.rows[row["id"]] = {**row, "_etag": str(self.version)}

    def delete_item(self, item, partition_key, etag=None, match_condition=None):
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError

        assert item == partition_key
        assert match_condition in (None, MatchConditions.IfNotModified)
        with self.lock:
            row = self.rows.get(item)
            if row is None:
                raise CosmosResourceNotFoundError(status_code=404)
            if etag is not None and row["_etag"] != etag:
                raise CosmosHttpResponseError(status_code=412)
            del self.rows[item]

    def query_items(self, *, query, parameters, enable_cross_partition_query):
        values = {p["name"]: p["value"] for p in parameters}
        with self.lock:
            return [
                {"key": row["key"]}
                for row in self.rows.values()
                if row.get("kind") == values["@kind"]
                and row.get("namespace") == values["@namespace"]
                and row["key"].startswith(values["@prefix"])
            ]


@pytest.fixture(params=["dynamodb", "cosmos"])
def stores(request, monkeypatch):
    if request.param == "dynamodb":
        with mock_aws():
            client = boto3.client("dynamodb", region_name="us-east-1")
            client.create_table(
                TableName="execution",
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            config = SessionSection(backend="dynamodb", dynamodb={"table": "execution", "region": "us-east-1"})
            yield SharedStore(config), SharedStore(config)
    else:
        pytest.importorskip("azure.cosmos")
        container = CosmosContainer()
        client = Mock()
        client.get_database_account.return_value = SimpleNamespace(
            WritableLocations=[{"name": "region"}],
            ConsistencyPolicy={"defaultConsistencyLevel": "Strong"},
        )
        client.get_database_client.return_value.get_container_client.return_value = container
        monkeypatch.setattr("azure.cosmos.CosmosClient", Mock(return_value=client))
        config = SessionSection(
            backend="cosmos",
            cosmos={"endpoint": "https://test.invalid", "database": "test", "container": "execution", "key": "secret"},
        )
        yield SharedStore(config), SharedStore(config)


async def test_external_reservation_rejects_concurrent_workers_and_releases(stores):
    one, two = stores
    async with one.hold("same"):
        with pytest.raises(SessionBusyError):
            async with two.hold("same"):
                pytest.fail("duplicate")
        async with two.hold("different"):
            pass
    async with two.hold("same"):
        pass


async def test_waiting_session_is_reserved_across_instances(stores):
    one, two = stores
    config = ExecutionSection(max_concurrent=1, max_queued=1)
    first = AdmissionController(config, shared_store=one)
    second = AdmissionController(config, shared_store=two)

    async def wait():
        async with first.acquire("waiting"):
            pytest.fail("should still be queued")

    async with first.acquire("active"):
        task = asyncio.create_task(wait())
        try:
            async with asyncio.timeout(5):
                while await two.inspect("waiting") is None:
                    await asyncio.sleep(0.01)
            with pytest.raises(SessionBusyError):
                async with second.acquire("waiting"):
                    pytest.fail("queued duplicate")
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    async with second.acquire("waiting"):
        pass


async def test_recovery_requires_exact_owner_and_old_owner_cannot_delete_new_reservation(stores):
    one, two = stores
    old = one.hold("same")
    await old.__aenter__()
    record = await two.inspect("same")
    assert record and record["process"] and record["created_at"]
    assert not await two.recover("same", "wrong-owner")
    assert await two.recover("same", record["owner"])
    async with two.hold("same"):
        await old.__aexit__(None, None, None)
        with pytest.raises(SessionBusyError):
            async with one.hold("same"):
                pytest.fail("old owner deleted new owner")


async def test_cancel_during_db_acquisition_waits_for_commit_then_releases(stores, monkeypatch):
    one, two = stores
    entered, release = threading.Event(), threading.Event()
    put = one.put

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(5)
        return put(*args, **kwargs)

    monkeypatch.setattr(one, "put", blocked)

    async def hold():
        async with one.hold("cancel"):
            pytest.fail("cancelled acquisition executed")

    task = asyncio.create_task(hold())
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await two.inspect("cancel") is None


async def test_unknown_write_outcome_never_starts_execution_or_steals_reservation(stores, monkeypatch):
    one, two = stores
    put = one.put

    def lost_response(*args, **kwargs):
        put(*args, **kwargs)
        raise OSError("response lost")

    monkeypatch.setattr(one, "put", lost_response)
    with pytest.raises(CapacityError):
        async with one.hold("unknown"):
            pytest.fail("unknown acquisition executed")
    assert await two.inspect("unknown") is not None
    with pytest.raises(SessionBusyError):
        async with two.hold("unknown"):
            pytest.fail("stole reservation")


async def test_shared_journal_round_trips_and_namespace_isolation(stores):
    one, two = stores
    await one.write("jobs/s/j/event", b"\x00\xffpayload")
    assert await two.read("jobs/s/j/event") == b"\x00\xffpayload"
    assert await two.read("missing") is None
    other = SharedStore(one.config.model_copy(update={"namespace": "other"}))
    assert await other.read("jobs/s/j/event") is None
    with pytest.raises(ValueError, match="240 KiB"):
        await one.write("too-big", b"x" * (240 * 1024 + 1))


async def test_jobs_are_observable_and_cancellable_from_another_instance(stores, tmp_path, monkeypatch):
    one, _ = stores
    write_agent(tmp_path / "agents", "a")
    if one.config.backend == "dynamodb":
        backend_config = '[session.dynamodb]\ntable = "execution"\nregion = "us-east-1"'
    else:
        backend_config = '[session.cosmos]\nendpoint = "https://test.invalid"\ndatabase = "test"\ncontainer = "execution"\nkey = "secret"'
    path = write_runtime(
        tmp_path,
        f'''default_agent = "a"
[jobs]
storage = "session"
[session]
backend = "{one.config.backend}"
{backend_config}
''',
    )
    first, second = Runtime.load(path), Runtime.load(path)
    monkeypatch.setattr(manager_module, "HEARTBEAT_SECONDS", 0.02)
    fakes.script(fakes.text("x" * 500))
    fakes.set_chunk_delay(0.01)
    try:
        job = await first.jobs.submit(
            InvocationRequest(messages=[{"role": "user", "content": "go"}], session_id="shared")
        )
        assert (await second.jobs.status("shared")).job_id == job.job_id
        with pytest.raises(SessionBusyError):
            await second.invoker.invoke(
                InvocationRequest(messages=[{"role": "user", "content": "go"}], session_id="shared")
            )
        assert (await second.jobs.cancel("shared")).state == "cancelling"
        async with asyncio.timeout(5):
            async for _ in first.jobs.follow("shared"):
                pass
        assert (await second.jobs.status("shared")).state == "cancelled"
        assert await second.shared_store.inspect("shared") is None
    finally:
        await first.jobs.shutdown()
        fakes.set_chunk_delay(0)


@pytest.mark.parametrize(
    "settings",
    [
        {"session": {"backend": "agentcore"}, "server": {"workers": 2}},
        {"session": {"backend": "none", "history": {"backend": "local"}}},
        {"session": {"backend": "dynamodb", "dynamodb": {"table": "test"}, "history": {"backend": "local"}}},
        {"session": {"backend": "cosmos"}},
        {"session": {"backend": "local", "history": {"backend": "inherit"}}},
        {"session": {"backend": "local", "history": {"backend": "dynamodb"}}},
        {"jobs": {"storage": "session"}},
        {"server": {"environment": "local"}},
        {"execution": {"backend": "sqlite"}},
    ],
)
def test_unsupported_deployment_is_rejected(settings):
    with pytest.raises(ValueError):
        RuntimeConfig.model_validate(settings)


def test_cloud_deployment_cannot_override_history_to_local(tmp_path):
    write_agent(tmp_path / "agents", "a", toml_extra='[session.history]\nbackend = "local"')
    config = write_runtime(
        tmp_path,
        """default_agent = "a"
[session]
backend = "dynamodb"
[session.dynamodb]
table = "execution"
""",
    )
    with pytest.raises(DefinitionError, match="local history"):
        Runtime.load(config)


def test_dynamodb_ttl_is_rejected():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="execution",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.update_time_to_live(
            TableName="execution", TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"}
        )
        store = SharedStore(SessionSection(backend="dynamodb", dynamodb={"table": "execution", "region": "us-east-1"}))
        with pytest.raises(DefinitionError, match="TTL"):
            store.connect()


@pytest.mark.parametrize("change", ["regions", "consistency", "partition", "ttl"])
def test_cosmos_rejects_unsafe_account_or_container_settings(monkeypatch, change):
    pytest.importorskip("azure.cosmos")
    account = SimpleNamespace(WritableLocations=[{}], ConsistencyPolicy={"defaultConsistencyLevel": "Strong"})
    properties = {"partitionKey": {"paths": ["/id"]}}
    if change == "regions":
        account.WritableLocations = [{}, {}]
    elif change == "consistency":
        account.ConsistencyPolicy = {"defaultConsistencyLevel": "Session"}
    elif change == "partition":
        properties["partitionKey"]["paths"] = ["/tenant"]
    else:
        properties["defaultTtl"] = 60
    client = Mock()
    client.get_database_account.return_value = account
    client.get_database_client.return_value.get_container_client.return_value.read.return_value = properties
    monkeypatch.setattr("azure.cosmos.CosmosClient", Mock(return_value=client))
    config = SessionSection(
        backend="cosmos",
        cosmos={
            "endpoint": "https://test.invalid",
            "database": "test",
            "container": "execution",
            "key": "secret",
        },
    )
    with pytest.raises(DefinitionError):
        SharedStore(config).connect()


def test_cosmos_etag_rejects_delete_racing_with_replacement(stores, monkeypatch):
    one, _ = stores
    if one.config.backend != "cosmos":
        pytest.skip("Cosmos ETag race")
    key = one.key("reservation", "race")
    one.put({"id": key, "owner": "old"}, exclusive=True)
    get = one.get

    def raced(key):
        old = get(key)
        one.put({"id": key, "owner": "new"})
        return old

    monkeypatch.setattr(one, "get", raced)
    assert not one.delete_owner(key, "old")
    assert get(key)["owner"] == "new"
