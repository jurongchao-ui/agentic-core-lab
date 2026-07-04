"""tenant_policy — 本地治理服务的租户级授权策略。

signed token 解决“这个请求是谁、来自哪个 tenant、拥有哪些 scopes”的问题。
TenantPolicyStore 解决“这个 tenant 当前是否允许使用这些 scopes”的问题。

生产系统里这通常来自策略中心/数据库。学习版先用 JSON 文件表达:

```json
{
  "schemaVersion": 1,
  "tenants": {
    "tenant_a": {
      "enabled": true,
      "allowedScopes": ["eval.viewer", "eval.reviewer"]
    },
    "tenant_suspended": {
      "enabled": false,
      "allowedScopes": ["eval.viewer"]
    }
  }
}
```

调用关系图:
  CLI: python -m agentic_core.tenant_policy
  eval_server(处理请求时)
      ├─▶ auth_tokens.verify_signed_token ─▶ AuthClaims(tenant / scopes)   # 你是谁、要用什么权限
      └─▶ TenantPolicyStore.check(tenant_id, scope) ─▶ TenantPolicyDecision # 这个租户当前允不允许
            两道都过才放行(签名身份 + 租户策略)。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TenantPolicy:
    """单个 tenant 的授权策略。"""

    tenant_id: str
    enabled: bool
    allowed_scopes: set[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenantId": self.tenant_id,
            "enabled": self.enabled,
            "allowedScopes": sorted(self.allowed_scopes) if self.allowed_scopes is not None else None,
        }


@dataclass(frozen=True)
class TenantPolicyDecision:
    """租户策略检查结果。"""

    allowed: bool
    reason: str
    tenant_id: str
    required_scope: str
    allowed_scopes: set[str] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "tenantId": self.tenant_id,
            "requiredScope": self.required_scope,
            "allowedScopes": sorted(self.allowed_scopes) if self.allowed_scopes is not None else None,
        }


class TenantPolicyStore:
    """从 JSON 加载并评估 tenant policy。"""

    def __init__(self, tenants: dict[str, TenantPolicy]) -> None:
        self.tenants = dict(tenants)

    @classmethod
    def from_file(cls, path: str | Path) -> "TenantPolicyStore":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("tenant policy file must be a JSON object")
        raw_tenants = data.get("tenants")
        if not isinstance(raw_tenants, dict):
            raise ValueError("tenant policy file must contain tenants object")
        tenants: dict[str, TenantPolicy] = {}
        for tenant_id, raw_policy in raw_tenants.items():
            if not isinstance(tenant_id, str) or not tenant_id:
                raise ValueError("tenant id must be a non-empty string")
            if not isinstance(raw_policy, dict):
                raise ValueError(f"tenant policy for {tenant_id} must be an object")
            tenants[tenant_id] = _policy_from_dict(tenant_id, raw_policy)
        return cls(tenants)

    def evaluate(self, tenant_id: str, required_scope: str) -> TenantPolicyDecision:
        policy = self.tenants.get(tenant_id)
        if policy is None:
            return TenantPolicyDecision(
                allowed=False,
                reason="unknown_tenant",
                tenant_id=tenant_id,
                required_scope=required_scope,
                allowed_scopes=None,
            )
        if not policy.enabled:
            return TenantPolicyDecision(
                allowed=False,
                reason="tenant_disabled",
                tenant_id=tenant_id,
                required_scope=required_scope,
                allowed_scopes=policy.allowed_scopes,
            )
        if policy.allowed_scopes is not None and required_scope not in policy.allowed_scopes:
            return TenantPolicyDecision(
                allowed=False,
                reason="scope_not_allowed_for_tenant",
                tenant_id=tenant_id,
                required_scope=required_scope,
                allowed_scopes=policy.allowed_scopes,
            )
        return TenantPolicyDecision(
            allowed=True,
            reason="allowed",
            tenant_id=tenant_id,
            required_scope=required_scope,
            allowed_scopes=policy.allowed_scopes,
        )


def _policy_from_dict(tenant_id: str, data: dict[str, Any]) -> TenantPolicy:
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"tenant {tenant_id} enabled must be a boolean")
    allowed_scopes = data.get("allowedScopes")
    if allowed_scopes is None:
        scopes = None
    elif isinstance(allowed_scopes, list) and all(isinstance(item, str) for item in allowed_scopes):
        scopes = set(allowed_scopes)
    else:
        raise ValueError(f"tenant {tenant_id} allowedScopes must be a list of strings")
    return TenantPolicy(tenant_id=tenant_id, enabled=enabled, allowed_scopes=scopes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect local tenant policy")
    parser.add_argument("show", nargs="?", help="显示策略")
    parser.add_argument("--path", required=True, help="tenant policy JSON")
    args = parser.parse_args(argv)
    store = TenantPolicyStore.from_file(args.path)
    data = {
        "schemaVersion": 1,
        "type": "agentic_tenant_policy",
        "tenants": {tenant_id: policy.to_dict() for tenant_id, policy in sorted(store.tenants.items())},
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
