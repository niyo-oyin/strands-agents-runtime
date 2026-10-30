"""``strands-runtime`` コマンド。validate / list / run / serve。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from strands_runtime.definitions.loader import ResolvedAgent, is_path_reference
from strands_runtime.definitions.schema import BUILTIN_INJECTORS, AgentDefinition, GraphDefinition
from strands_runtime.errors import StrandsRuntimeError
from strands_runtime.factory.references import load_attribute
from strands_runtime.invoke import InvocationRequest
from strands_runtime.logging_setup import configure_logging
from strands_runtime.runtime import Runtime
from strands_runtime_client.events import ErrorEvent, ResultEvent, TextEvent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strands-runtime", description="Declarative agent runtime on Strands Agents")
    parser.add_argument("-c", "--config", type=Path, default=None, help="path to runtime.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="load every definition, resolve code references, check skills")
    validate.add_argument("--mcp", action="store_true", help="also start every MCP server and list its tools")
    sub.add_parser("list", help="list published agents")

    run = sub.add_parser("run", help="invoke an agent once without starting the server")
    run.add_argument("agent")
    run.add_argument("--messages", required=True, help="JSON array of user/assistant messages")
    run.add_argument("--store", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--session-id")
    run.add_argument("--actor-id")
    run.add_argument("--json", action="store_true", help="print every event as a JSON line")
    run.add_argument("--state", help="invocation_state as a JSON object, passed to tools")
    run.add_argument("--reasoning", action="store_true", help="include the model's reasoning events")
    run.add_argument("--tool-results", choices=["preview", "full"], default="preview")

    serve = sub.add_parser("serve", help="start the HTTP server (AgentCore contract)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    reservation = sub.add_parser(
        "reservation", help="inspect or release an external session reservation after stopping its owner"
    )
    reservation.add_argument("session_id")
    reservation.add_argument("--release-owner", help="release only if the reservation still has this exact owner token")
    return parser


def main(argv: list[str] | None = None) -> int:
    """終了コードを返す。定義や参照の問題は ``StrandsRuntimeError`` として 1 行のエラーにまとめる。"""
    args = build_parser().parse_args(argv)
    configure_logging(logging.DEBUG if args.verbose else logging.WARNING)
    try:
        runtime = Runtime.load(args.config)
        match args.command:
            case "validate":
                return _validate(runtime, connect_mcp=args.mcp)
            case "list":
                return _list(runtime)
            case "run":
                return asyncio.run(_run(runtime, args))
            case "serve":
                return _serve(runtime, args)
            case "reservation":
                return asyncio.run(_reservation(runtime, args))
    except StrandsRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


async def _reservation(runtime: Runtime, args: argparse.Namespace) -> int:
    if runtime.shared_store is None:
        print("error: reservation management requires session.backend = dynamodb or cosmos", file=sys.stderr)
        return 1
    if args.release_owner:
        released = await runtime.shared_store.recover(args.session_id, args.release_owner)
        print(json.dumps({"released": released}))
        return 0 if released else 1
    print(json.dumps(await runtime.shared_store.inspect(args.session_id), ensure_ascii=False))
    return 0


def _validate(runtime: Runtime, *, connect_mcp: bool = False) -> int:
    """定義の読み込み（Runtime.load で済んでいる）に加えて、コード参照・スキル・（任意で）MCP 接続を確かめる。"""
    problems: list[str] = []
    for agent in runtime.registry:
        problems.extend(_check_references(agent))
        problems.extend(_check_skills(agent))
        if connect_mcp:
            problems.extend(_check_mcp(agent))
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    print(f"ok: {len(runtime.registry.published)} published agent(s): {', '.join(runtime.registry.published)}")
    return 0


def _check_references(agent: ResolvedAgent) -> list[str]:
    """定義が指すコードをすべて import してみる。Agent は組み立てないので、資格情報も MCP サーバーも要らない。"""
    problems: list[str] = []
    d = agent.definition
    refs = [*agent.plugins, *(i for i in agent.interventions if not i.endswith(".cedar"))]
    if isinstance(d, GraphDefinition):
        refs.extend(e.condition for e in d.edges if e.condition)
    elif isinstance(d, AgentDefinition):
        if d.model.module:
            refs.append(d.model.module)
        if d.structured_output:
            refs.append(d.structured_output.model)
        refs.extend(i for i in d.context.inject if i not in BUILTIN_INJECTORS)
    for ref in refs:
        try:
            load_attribute(ref)
        except StrandsRuntimeError as exc:
            problems.append(f"{d.name}: {exc}")
    # Cedar ポリシーは extras の [cedar]（cedarpy）が要る。入っていなければ最初の呼び出しではなく、ここで気づけるようにする。
    if any(i.endswith(".cedar") for i in agent.interventions):
        try:
            import strands.vended_interventions as interventions

            interventions.CedarAuthorization  # noqa: B018 - 遅延 import を発火させるための参照
        except ImportError as exc:
            problems.append(f"{d.name}: {exc}")
    # ツールのモジュール参照は ``pkg.module`` だけ（``:func`` なし）もあり得るので、モジュール部分の import だけ試す。
    for tool in agent.tools:
        if not is_path_reference(tool):
            module = tool.split(":", 1)[0]
            try:
                __import__(module)
            except ImportError as exc:
                problems.append(f"{d.name}: tool module {module!r}: {exc}")
    return problems


def _check_skills(agent: ResolvedAgent) -> list[str]:
    """``SKILL.md`` を厳格に解析する（name / description の有無、name とディレクトリ名の一致）。"""
    if not agent.skills:
        return []
    from strands.vended_plugins.skills import AgentSkills

    try:
        AgentSkills(skills=list(agent.skills), strict=True)
    except Exception as exc:  # SDK は ValueError / OSError 系をそのまま送出する
        return [f"{agent.name}: skills: {exc}"]
    return []


def _check_mcp(agent: ResolvedAgent) -> list[str]:
    """各 MCP サーバーを 1 度起動してツール一覧を取る。時間がかかるので ``--mcp`` 指定時だけ。"""
    if not agent.mcp_servers:
        return []
    from strands.tools.mcp import MCPClient

    problems: list[str] = []
    for server_name, config in agent.mcp_servers.items():
        try:
            [client] = MCPClient.load_servers({"mcpServers": {server_name: config}})
            with client:
                names = [tool.tool_name for tool in client.list_tools_sync()]
            print(f"{agent.name}: mcp {server_name}: {', '.join(names) or '(no tools)'}")
        except Exception as exc:
            problems.append(f"{agent.name}: mcp {server_name}: {exc}")
    return problems


def _list(runtime: Runtime) -> int:
    for name in runtime.registry.published:
        d = runtime.registry.get(name).definition
        if isinstance(d, AgentDefinition):
            detail = d.model.id or d.model.module or runtime.config.model.default_id
        else:
            detail = f"{d.kind}: {', '.join(d.node_ids)}"
        print(f"{name}\t{detail}\t{d.description}")
    return 0


async def _run(runtime: Runtime, args: argparse.Namespace) -> int:
    """サーバーを立てずに 1 回呼び出す。既定はテキストだけを表示し、``--json`` でイベントを 1 行ずつ出す。"""
    request = InvocationRequest(
        agent=args.agent,
        messages=json.loads(args.messages),
        store=args.store,
        session_id=args.session_id,
        actor_id=args.actor_id,
        invocation_state=json.loads(args.state) if args.state else {},
        include_reasoning=args.reasoning,
        tool_result_detail=args.tool_results,
    )
    status = 0
    async for event in runtime.invoker.stream(request):
        if args.json:
            print(json.dumps(event.model_dump(mode="json"), ensure_ascii=False))
        elif isinstance(event, TextEvent):
            print(event.text, end="", flush=True)
        elif isinstance(event, ResultEvent):
            print()
            print(
                f"[session {event.session_id} | {event.stop_reason} | {event.metrics.total_tokens} tokens]",
                file=sys.stderr,
            )
        if isinstance(event, ErrorEvent):
            print(f"error: {event.error_type}: {event.message}", file=sys.stderr)
            status = 1
    return status


def _serve(runtime: Runtime, args: argparse.Namespace) -> int:
    from strands_runtime.server import run_app

    port = args.port or runtime.config.server.port
    # コンテナ内は 0.0.0.0、それ以外は 127.0.0.1。
    in_container = Path("/.dockerenv").exists() or bool(os.environ.get("DOCKER_CONTAINER"))
    host = args.host or runtime.config.server.host or ("0.0.0.0" if in_container else "127.0.0.1")
    print(f"strands-runtime: serving {', '.join(runtime.registry.published)} at http://{host}:{port}", flush=True)
    print("  POST /invocations  GET /ping   (Ctrl-C to stop)", flush=True)
    run_app(runtime, port=port, host=host, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
