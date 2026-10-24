"""``[[edges]] condition = "./conditions.py:needs_follow_up"`` から読み込まれるエッジ条件。

``GraphState`` から各ノードの結果（``state.results[node_id].result``）を見て真偽を返す。
実行可能なノードの判定と入力の組み立てで複数回評価されるので、副作用を持たせない。
"""

from strands.multiagent.graph import GraphState


def needs_follow_up(state: GraphState) -> bool:
    previous = state.results.get("helper")
    return previous is not None and "?" in str(previous.result)
