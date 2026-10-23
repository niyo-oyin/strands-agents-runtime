"""構造化出力の確認用スキーマ。agent.toml の ``[structured_output] model = "./schemas.py:ContactCard"`` から参照される。"""

from pydantic import BaseModel, Field


class ContactCard(BaseModel):
    name: str = Field(description="人物のフルネーム")
    email: str | None = Field(default=None, description="メールアドレス")
    company: str | None = Field(default=None, description="所属組織")
    topics: list[str] = Field(default_factory=list, description="話題のキーワード")
