"""writing-flow のエッジ条件。reviewer の最終テキストが REWRITE で始まるときだけ drafter へ戻る。"""

from strands.multiagent.graph import GraphState


def needs_rewrite(state: GraphState) -> bool:
    review = state.results.get("reviewer")
    return review is not None and str(review.result).strip().startswith("REWRITE")
