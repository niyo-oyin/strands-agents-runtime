"""``[structured_output] model = "./schemas.py:Answer"`` から読み込まれる pydantic モデル。"""

from pydantic import BaseModel, Field


class Answer(BaseModel):
    answer: str = Field(description="質問への回答")
    confidence: float = Field(ge=0.0, le=1.0, description="回答の確からしさ")
