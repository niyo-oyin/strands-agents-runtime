"""クライアントとサーバーで共有する入力形式。Strands SDK には依存しない。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Message(BaseModel):
    """会話の1メッセージ。content は文字列または Strands 形式の content block 配列。"""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]] = Field(min_length=1)


class JsonSchemaFormat(BaseModel):
    """Chat Completions 形式の JSON Schema 指定。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    schema_: dict[str, Any] = Field(alias="schema")
    description: str | None = None
    strict: bool | None = None


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text", "json_object", "json_schema"]
    json_schema: JsonSchemaFormat | None = None

    @model_validator(mode="after")
    def _schema_required(self) -> ResponseFormat:
        if (self.type == "json_schema") != (self.json_schema is not None):
            raise ValueError("json_schema is required only when response_format.type is 'json_schema'")
        return self


class FunctionName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)


class FunctionToolChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["function"]
    function: FunctionName


ToolChoice = Literal["auto", "none", "required"] | FunctionToolChoice
ReasoningEffort = Literal["auto", "off", "none", "minimal", "low", "medium", "high", "xhigh", "max"]


class GenerationOptions(BaseModel):
    """指定された生成条件だけを上書きする。None は定義・プロバイダーの既定値を使う。"""

    model_config = ConfigDict(extra="forbid")
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    temperature: float | None = Field(default=None, ge=0, le=2, strict=True, allow_inf_nan=False)
    top_p: float | None = Field(default=None, ge=0, le=1, strict=True, allow_inf_nan=False)
    reasoning_effort: ReasoningEffort | None = None
    response_format: ResponseFormat | None = None
    tool_choice: ToolChoice | None = None

    def generation_overrides(self) -> dict[str, Any]:
        return self.model_dump(include=set(GenerationOptions.model_fields), exclude_none=True, by_alias=True)


class InvocationInput(GenerationOptions):
    """実行入力。観測リクエストでは入力を省略でき、実行時は require_input で必須にする。"""

    model_config = ConfigDict(extra="forbid")

    prompt: str | list[dict[str, Any]] | None = Field(default=None, min_length=1)
    messages: list[Message] | None = Field(default=None, min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, pattern=r"^\S+/\S+$")
    timeout_seconds: float | None = Field(default=None, gt=0, strict=True, allow_inf_nan=False)
    max_turns: int | None = Field(default=None, gt=0, strict=True)
    max_total_tokens: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def _consistent_input(self) -> InvocationInput:
        if self.prompt is not None and self.messages is not None:
            raise ValueError("prompt and messages are mutually exclusive")
        if self.session_id is not None and self.messages is not None and len(self.messages) != 1:
            raise ValueError("messages must contain exactly one message when session_id is specified")
        return self

    def require_input(self) -> None:
        if self.prompt is None and self.messages is None:
            raise ValueError("exactly one of prompt or messages is required")
