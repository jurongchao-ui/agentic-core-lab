"""runtime_context — 一次运行的身份/权限上下文。

功能:
  - RuntimeIdentity(frozen): user_id / tenant_id / roles / permission_scopes。
    生产里权限判断不能只看工具,还要看"谁、哪个租户、什么角色、有哪些 scope"。
  - build_runtime_identity_from_env(): 从 AGENTIC_USER_ID/TENANT_ID/ROLES/
    PERMISSION_SCOPES 构造(默认不限 scope,保持学习版原行为)。

调用关系图:
  cli / chat(装配层) ─▶ build_runtime_identity_from_env() ─▶ RuntimeIdentity
  Agent ─▶ 传给 MiddlewarePipeline / ToolGovernancePolicy ─▶ 按 scope/role 做审批与预算
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuntimeIdentity:
    """一次 Agent 运行的身份上下文。

    生产系统里,权限判断不能只看工具本身,还要看:
        - 是哪个用户
        - 属于哪个租户
        - 当前角色是什么
        - 被授予了哪些 permission scopes

    学习版用环境变量构造这个对象,真实生产可从登录态/JWT/session 派生。
    """

    user_id: str = "local_user"
    tenant_id: str = "default_tenant"
    roles: set[str] = field(default_factory=lambda: {"developer"})
    permission_scopes: set[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "userId": self.user_id,
            "tenantId": self.tenant_id,
            "roles": sorted(self.roles),
            "permissionScopes": sorted(self.permission_scopes) if self.permission_scopes is not None else None,
        }


def build_runtime_identity_from_env() -> RuntimeIdentity:
    """从环境变量构造 RuntimeIdentity。

    默认不限制 permission scopes,保持学习版原行为。

    可选环境变量:
        AGENTIC_USER_ID
        AGENTIC_TENANT_ID
        AGENTIC_ROLES=developer,admin
        AGENTIC_PERMISSION_SCOPES=tool:calculator:read,memory:note:write
    """

    scopes = _csv_set(os.getenv("AGENTIC_PERMISSION_SCOPES"))
    return RuntimeIdentity(
        user_id=os.getenv("AGENTIC_USER_ID", "local_user").strip() or "local_user",
        tenant_id=os.getenv("AGENTIC_TENANT_ID", "default_tenant").strip() or "default_tenant",
        roles=_csv_set(os.getenv("AGENTIC_ROLES")) or {"developer"},
        permission_scopes=scopes,
    )


def _csv_set(value: str | None) -> set[str]:
    if value is None:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}
