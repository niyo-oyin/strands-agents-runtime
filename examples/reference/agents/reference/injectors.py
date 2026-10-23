"""``[context] inject = ["./injectors.py:render_turn"]`` から読み込まれる注入関数。

各モデル呼び出しの直前に呼ばれ、返した文字列がそのターンに一時的に添えられる（会話履歴には残らない）。
``None`` か空文字を返すと何も注入しない。
``InjectionContext`` からは会話（``messages``）、ツールやフックと共有する ``state``、``agent`` を参照できる。
"""

from strands.injection import InjectionContext


def render_turn(context: InjectionContext) -> str | None:
    turns = sum(1 for message in context.messages if message.get("role") == "user")
    return f"<turn>{turns}</turn>"
