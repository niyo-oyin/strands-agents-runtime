"""ランタイム全体で使う例外。"""

from __future__ import annotations


class StrandsRuntimeError(Exception):
    """strands_runtime が送出する例外の基底。CLI はこれを捕まえて 1 行のエラー表示にする。"""


class DefinitionError(StrandsRuntimeError):
    """エージェント定義または runtime.toml が不正。``source`` はファイルパスで、メッセージの先頭に付く。"""

    def __init__(self, message: str, *, source: str | None = None) -> None:
        self.source = source
        super().__init__(f"{source}: {message}" if source else message)


class UnknownAgentError(StrandsRuntimeError):
    """要求されたエージェントがこのランタイムで公開されていない。サーバーは 404 にする。"""

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        super().__init__(f"unknown agent {name!r}; available: {', '.join(available) or '(none)'}")


class SessionBusyError(StrandsRuntimeError):
    """同じセッションの別の実行（同期実行またはジョブ）が進行中。サーバーは 409 にする（AgentCore の扱いと同じ）。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"session {session_id!r} already has an invocation in progress; retry later")


class ReferenceResolutionError(StrandsRuntimeError):
    """定義に書かれたモジュール・属性の参照を解決できない。"""


class UnknownJobError(StrandsRuntimeError):
    """指定されたジョブがジャーナルにない。サーバーは 404 にする。"""

    def __init__(self, session_id: str, job_id: str | None) -> None:
        self.session_id = session_id
        self.job_id = job_id
        what = f"job {job_id!r}" if job_id else "any job"
        super().__init__(f"session {session_id!r} has no record of {what}")


class JobsDisabledError(StrandsRuntimeError):
    """ジョブのジャーナルを置く Storage がない構成で ``mode = "job"`` が要求された。サーバーは 400 にする。"""

    def __init__(self) -> None:
        super().__init__(
            "jobs are disabled: [session] backend has no storage for the journal; "
            'set [jobs] storage = "local" or "s3" in runtime.toml'
        )
