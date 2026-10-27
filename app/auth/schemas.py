from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")
PASSWORD_MIN = 12
PASSWORD_MAX = 128
DISPLAY_NAME_MAX = 64


def normalize_username(value: str) -> str:
    return value.strip().lower()


def check_username(value: str) -> str:
    normalized = normalize_username(value)
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("用户名需为 3～32 位小写字母、数字或下划线")
    return normalized


def check_password(value: str) -> str:
    if not PASSWORD_MIN <= len(value) <= PASSWORD_MAX:
        raise ValueError(f"密码长度需为 {PASSWORD_MIN}～{PASSWORD_MAX} 个字符")
    return value


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    display_name: str = Field(min_length=1, max_length=DISPLAY_NAME_MAX)
    password: str

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        return check_username(value)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("显示名不能为空")
        return value

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        return check_password(value)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        return normalize_username(value)


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    permissions: list[str]


class CustomerSummary(BaseModel):
    id: str
    username: str
    display_name: str
    status: str
    created_at: str


class CustomerPage(BaseModel):
    items: list[CustomerSummary]
    total: int
    page: int
    page_size: int
