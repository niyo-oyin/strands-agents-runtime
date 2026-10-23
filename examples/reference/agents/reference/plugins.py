"""``plugins = ["./plugins.py:AuditPlugin"]`` から読み込まれる Strands の Plugin。

Plugin は ``@hook`` を付けたメソッド（フック）と ``@tool`` を付けたメソッド（ツール）をまとめて Agent に登録する。
参照先はインスタンスでも、引数なしで作れるクラスでも、引数なしのファクトリ関数でもよい。
"""

import logging

from strands.hooks import AfterInvocationEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)


class AuditPlugin(Plugin):
    name = "audit"

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        logger.info("audit: agent=%s messages=%d", event.agent.name, len(event.agent.messages))
