"""エージェントディレクトリから読み込まれるカスタムツールの例。

``@tool`` の関数名がツール名、docstring がモデルに見せる説明になる。
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from strands import tool


@tool
def current_time(timezone: str = "UTC") -> str:
    """Return the current date and time in ISO 8601 format.

    Args:
        timezone: IANA time zone name such as "Asia/Tokyo". Defaults to UTC.
    """
    tz = UTC if timezone.upper() == "UTC" else ZoneInfo(timezone)
    return datetime.now(tz).isoformat(timespec="seconds")
