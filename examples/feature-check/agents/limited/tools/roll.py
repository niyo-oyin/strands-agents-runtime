"""``[limits] turns`` の確認用。ツール呼び出しを 1 回起こすだけの単純なツール。"""

import random

from strands import tool


@tool
def roll(sides: int = 6) -> int:
    """Roll a die.

    Args:
        sides: Number of faces.
    """
    return random.randint(1, sides)
