"""请求/响应模型。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from .constants import (
    CONTENT_MODE_CUSTOM,
    CONTENT_MODE_TEMPLATE,
)

_VARIABLE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")


class EmailContent(BaseModel):
    """邮件内容：内置模板或调用方自定义，二选一。"""

    mode: Literal["template", "custom"] = CONTENT_MODE_TEMPLATE
    template: str | None = Field(default=None, max_length=64)
    subject: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=20000)
    html: str | None = Field(default=None, max_length=50000)
    variables: dict[str, str] = Field(default_factory=dict)

    @field_validator("variables")
    @classmethod
    def _check_variables(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("variables 最多 20 个")
        for key, item in value.items():
            if not _VARIABLE_KEY.match(key):
                raise ValueError(f"variables 键名不合法：{key!r}")
            if len(item) > 1024:
                raise ValueError(f"variables[{key!r}] 超过 1KB")
        return value

    @model_validator(mode="after")
    def _check_mode(self) -> "EmailContent":
        if self.mode == CONTENT_MODE_CUSTOM:
            if not (self.subject or "").strip():
                raise ValueError("custom 模式必须提供 subject")
            if not (self.text or "").strip() and not (self.html or "").strip():
                raise ValueError("custom 模式必须提供 text 或 html")
            if self.subject and any(ch in self.subject for ch in "\r\n"):
                raise ValueError("subject 不能包含换行")
        else:
            if self.template and not self.template.strip():
                raise ValueError("template 不能为空")
        return self


class VerifyCreate(BaseModel):
    email: EmailStr
    purpose: str = Field(default="generic", pattern=r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
    source: str | None = Field(default=None, max_length=64)
    callback_url: str = Field(min_length=1, max_length=2048)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_in: int | None = Field(default=None, ge=60, le=86400)
    content: EmailContent | None = None


class VerifyCreated(BaseModel):
    id: str
    status: str
    expires_in: int
    expires_at: str


class DeliveryInfo(BaseModel):
    status: str
    attempts: int
    last_status_code: int | None = None
    last_error: str | None = None
    delivered_at: str | None = None


class VerifyStatus(BaseModel):
    id: str
    status: str
    email: str
    purpose: str
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    expires_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    delivery: DeliveryInfo | None = None
