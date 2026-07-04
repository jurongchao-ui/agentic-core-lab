"""auth_tokens — 本地治理服务使用的签名 claims token。

生产系统通常会接 OIDC/JWT/session。当前项目不引第三方依赖,所以这里用
Python 标准库实现一个学习版 signed token:

  - payload 带 sub/tenant/scopes/iat/exp。
  - HMAC-SHA256 签名防篡改。
  - eval_server 可用它替代静态 viewer/reviewer token。

它不是完整 JWT 实现,但已经表达了生产认证最关键的边界:
身份 claims 与权限 scopes 来自可验证 token,而不是服务端写死的字符串。

调用关系图:
  CLI: python -m agentic_core.auth_tokens (create | verify)
  create_signed_token(sub/tenant/scopes, secret) ─▶ HMAC-SHA256 签名 token
  eval_server(收到请求)
      └─▶ verify_signed_token(token, secret) ─▶ AuthClaims(sub/tenant/scopes)
            └─▶ 再交给 tenant_policy.TenantPolicyStore.check 判租户是否允许该 scope
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any


TOKEN_PREFIX = "agct_"


@dataclass(frozen=True)
class AuthClaims:
    """签名 token 解析后的身份与权限 claims。"""

    subject: str
    tenant: str
    scopes: set[str]
    issued_at: int
    expires_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub": self.subject,
            "tenant": self.tenant,
            "scopes": sorted(self.scopes),
            "iat": self.issued_at,
            "exp": self.expires_at,
        }


def create_signed_token(
    secret: str,
    subject: str,
    scopes: set[str],
    tenant: str = "default_tenant",
    ttl_seconds: int = 3600,
    now: int | None = None,
) -> str:
    """创建 HMAC 签名 token。"""

    if not secret:
        raise ValueError("secret is required")
    if not subject:
        raise ValueError("subject is required")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be > 0")
    issued_at = int(time.time() if now is None else now)
    claims = AuthClaims(
        subject=subject,
        tenant=tenant,
        scopes=set(scopes),
        issued_at=issued_at,
        expires_at=issued_at + ttl_seconds,
    )
    payload = _b64_json(claims.to_dict())
    signature = _sign(payload, secret)
    return f"{TOKEN_PREFIX}{payload}.{signature}"


def verify_signed_token(token: str, secret: str, now: int | None = None) -> AuthClaims:
    """验证 token 签名和过期时间,返回 claims。"""

    if not secret:
        raise ValueError("secret is required")
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError("token prefix is invalid")
    body = token[len(TOKEN_PREFIX) :]
    try:
        payload, signature = body.split(".", 1)
    except ValueError as exc:
        raise ValueError("token format is invalid") from exc
    expected = _sign(payload, secret)
    if not hmac.compare_digest(signature, expected):
        raise ValueError("token signature is invalid")
    claims_data = json.loads(_b64_decode(payload).decode("utf-8"))
    if not isinstance(claims_data, dict):
        raise ValueError("token claims must be an object")
    claims = _claims_from_dict(claims_data)
    current_time = int(time.time() if now is None else now)
    if claims.expires_at <= current_time:
        raise ValueError("token is expired")
    return claims


def _claims_from_dict(data: dict[str, Any]) -> AuthClaims:
    subject = data.get("sub")
    tenant = data.get("tenant", "default_tenant")
    scopes = data.get("scopes")
    issued_at = data.get("iat")
    expires_at = data.get("exp")
    if not isinstance(subject, str) or not subject:
        raise ValueError("token subject is invalid")
    if not isinstance(tenant, str) or not tenant:
        raise ValueError("token tenant is invalid")
    if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
        raise ValueError("token scopes are invalid")
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        raise ValueError("token issued-at is invalid")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise ValueError("token expires-at is invalid")
    return AuthClaims(
        subject=subject,
        tenant=tenant,
        scopes=set(scopes),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _b64_json(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64_encode(raw)


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return _b64_encode(digest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create local signed auth tokens for Agentic eval server")
    parser.add_argument("create", nargs="?", help="创建 token")
    parser.add_argument("--secret", required=True, help="签名密钥")
    parser.add_argument("--subject", required=True, help="用户/服务主体")
    parser.add_argument("--tenant", default="default_tenant", help="租户 id")
    parser.add_argument("--scopes", required=True, help="逗号分隔 scopes,如 eval.viewer,eval.reviewer")
    parser.add_argument("--ttl", type=int, default=3600, help="有效期秒数")
    args = parser.parse_args(argv)
    token = create_signed_token(
        secret=args.secret,
        subject=args.subject,
        tenant=args.tenant,
        scopes={item.strip() for item in args.scopes.split(",") if item.strip()},
        ttl_seconds=args.ttl,
    )
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
