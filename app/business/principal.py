"""Backend-owned identity. Never build this from model or debug-page fields."""

from __future__ import annotations

from dataclasses import dataclass

CUSTOMER_PERMISSIONS = frozenset(
    {
        "orders:read",
        "devices:read",
        "tickets:read",
        "tickets:write",
        "knowledge:search",
    }
)
ADMIN_PERMISSIONS = CUSTOMER_PERMISSIONS | frozenset(
    {
        "admin:manage",
        "orders:write",
        "devices:write",
        "tickets:manage",
        "knowledge:manage",
    }
)

ROLE_CUSTOMER = "customer"
ROLE_ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    username: str
    display_name: str
    role: str
    permissions: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @classmethod
    def for_user(cls, *, user_id: str, username: str, display_name: str, role: str):
        permissions = ADMIN_PERMISSIONS if role == ROLE_ADMIN else CUSTOMER_PERMISSIONS
        return cls(
            user_id=user_id,
            username=username,
            display_name=display_name,
            role=role,
            permissions=permissions,
        )

    def can(self, permission: str) -> bool:
        return permission in self.permissions
