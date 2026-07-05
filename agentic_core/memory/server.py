"""memory.server — 长期记忆审核/维护 HTTP API。

这是 memory_admin CLI 的服务端边界:
  - GET /health
  - GET /api/memories
  - GET /api/memories/conflicts
  - POST /api/memories/archive
  - POST /api/memories/importance
  - POST /api/memories/resolve-conflict

本模块刻意只使用标准库。它复用 memory.admin 的治理函数,避免 HTTP API 和 CLI
各写一套规则导致 drift。
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from agentic_core.memory.admin import (
    archive_memory,
    find_memory_conflicts,
    list_memories,
    resolve_memory_conflict,
    set_memory_importance,
)
from agentic_core.memory.store import JsonMemoryStore


SERVICE_NAME = "agentic_memory_review_server"


@dataclass(frozen=True)
class MemoryReviewRequest:
    """长期记忆审核 API 的路由层请求。"""

    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, list[str]] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True)
class MemoryReviewResponse:
    """长期记忆审核 API 的路由层响应。"""

    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryReviewServerConfig:
    """长期记忆审核服务配置。"""

    host: str = "127.0.0.1"
    port: int = 8770
    memory_path: str = "data/memory.json"
    auth_token: str | None = None
    default_user_id: str = "local_user"
    default_tenant_id: str = "default_tenant"


def handle_memory_review_request(
    request: MemoryReviewRequest | str,
    config: MemoryReviewServerConfig,
) -> MemoryReviewResponse:
    """处理 memory review API 路由。

    字符串 request 兼容简单测试;生产路径应该传 MemoryReviewRequest,这样 method/header/body
    不会被绕开。
    """

    try:
        normalized = _normalize_request(request)
        auth_response = _authorize(normalized, config)
        if auth_response is not None:
            return auth_response

        method = normalized.method.upper()
        path = normalized.path
        if path == "/health" and method == "GET":
            return _json_response(
                {
                    "ok": True,
                    "service": SERVICE_NAME,
                    "authRequired": bool(config.auth_token),
                    "writeAuthRequired": True,
                }
            )

        if method == "GET" and path == "/api/memories":
            return _handle_list(normalized, config)
        if method == "GET" and path == "/api/memories/conflicts":
            return _handle_conflicts(normalized, config)
        if method == "POST" and path == "/api/memories/archive":
            return _handle_archive(normalized, config)
        if method == "POST" and path == "/api/memories/importance":
            return _handle_importance(normalized, config)
        if method == "POST" and path == "/api/memories/resolve-conflict":
            return _handle_resolve_conflict(normalized, config)
        if method not in {"GET", "POST"}:
            return _json_response(
                {"error": "method_not_allowed", "message": f"Method {method} is not allowed."},
                status=405,
                headers={"Allow": "GET, POST"},
            )
        return _json_response({"error": "not_found", "message": f"Unknown route: {path}"}, status=404)
    except ValueError as exc:
        return _json_response({"error": "bad_request", "message": str(exc)}, status=400)
    except Exception as exc:  # pragma: no cover - 兜底保护服务端边界
        return _json_response({"error": "internal_server_error", "message": str(exc)}, status=500)


def make_handler(config: MemoryReviewServerConfig) -> type[BaseHTTPRequestHandler]:
    """创建绑定配置的 HTTP handler。"""

    class MemoryReviewHandler(BaseHTTPRequestHandler):
        server_version = "AgenticMemoryReviewServer/1.0"

        def do_GET(self) -> None:
            self._send_response(self._handle_request())

        def do_POST(self) -> None:
            self._send_response(self._handle_request())

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _handle_request(self) -> MemoryReviewResponse:
            parsed = urlparse(self.path)
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(content_length) if content_length > 0 else b""
            return handle_memory_review_request(
                MemoryReviewRequest(
                    method=self.command,
                    path=parsed.path,
                    headers={key.lower(): value for key, value in self.headers.items()},
                    query=parse_qs(parsed.query),
                    body=body,
                ),
                config,
            )

        def _send_response(self, response: MemoryReviewResponse) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.body)

    return MemoryReviewHandler


def _handle_list(request: MemoryReviewRequest, config: MemoryReviewServerConfig) -> MemoryReviewResponse:
    store = JsonMemoryStore(config.memory_path)
    user_id, tenant_id = _namespace(request, config)
    include_archived = _query_bool(request, "includeArchived", default=False)
    memory_type = _query_one(request, "type")
    memories = list_memories(store, user_id, tenant_id, include_archived, memory_type)
    return _json_response(
        {
            "type": "agentic_memory_review_list",
            "path": config.memory_path,
            "userId": user_id,
            "tenantId": tenant_id,
            "includeArchived": include_archived,
            "count": len(memories),
            "memories": [memory.to_dict() for memory in memories],
        }
    )


def _handle_conflicts(request: MemoryReviewRequest, config: MemoryReviewServerConfig) -> MemoryReviewResponse:
    store = JsonMemoryStore(config.memory_path)
    user_id, tenant_id = _namespace(request, config)
    conflicts = find_memory_conflicts(store, user_id, tenant_id)
    return _json_response(
        {
            "type": "agentic_memory_review_conflicts",
            "path": config.memory_path,
            "userId": user_id,
            "tenantId": tenant_id,
            "count": len(conflicts),
            "conflicts": conflicts,
        }
    )


def _handle_archive(request: MemoryReviewRequest, config: MemoryReviewServerConfig) -> MemoryReviewResponse:
    _require_write_auth(request, config)
    body = _json_body(request)
    store = JsonMemoryStore(config.memory_path)
    user_id, tenant_id = _namespace_from_body(body, config)
    memory = archive_memory(
        store,
        memory_id=_required_str(body, "memoryId"),
        reason=_required_str(body, "reason"),
        user_id=user_id,
        tenant_id=tenant_id,
    )
    return _json_response({"type": "agentic_memory_review_archive", "memory": memory.to_dict()})


def _handle_importance(request: MemoryReviewRequest, config: MemoryReviewServerConfig) -> MemoryReviewResponse:
    _require_write_auth(request, config)
    body = _json_body(request)
    store = JsonMemoryStore(config.memory_path)
    user_id, tenant_id = _namespace_from_body(body, config)
    memory = set_memory_importance(
        store,
        memory_id=_required_str(body, "memoryId"),
        importance=_required_int(body, "importance"),
        user_id=user_id,
        tenant_id=tenant_id,
    )
    return _json_response({"type": "agentic_memory_review_importance", "memory": memory.to_dict()})


def _handle_resolve_conflict(request: MemoryReviewRequest, config: MemoryReviewServerConfig) -> MemoryReviewResponse:
    _require_write_auth(request, config)
    body = _json_body(request)
    store = JsonMemoryStore(config.memory_path)
    user_id, tenant_id = _namespace_from_body(body, config)
    result = resolve_memory_conflict(
        store,
        user_id=user_id,
        tenant_id=tenant_id,
        keep_memory_id=_required_str(body, "keepMemoryId"),
        reason=_required_str(body, "reason"),
    )
    return _json_response(result)


def _normalize_request(request: MemoryReviewRequest | str) -> MemoryReviewRequest:
    if isinstance(request, MemoryReviewRequest):
        return request
    parsed = urlparse(request)
    return MemoryReviewRequest(method="GET", path=parsed.path or "/", query=parse_qs(parsed.query))


def _authorize(
    request: MemoryReviewRequest,
    config: MemoryReviewServerConfig,
) -> MemoryReviewResponse | None:
    if request.path == "/health":
        return None
    if config.auth_token is None:
        return None
    header = request.headers.get("authorization", "")
    expected = f"Bearer {config.auth_token}"
    if not hmac.compare_digest(header, expected):
        return _json_response({"error": "unauthorized", "message": "Valid bearer token required."}, status=401)
    return None


def _require_write_auth(request: MemoryReviewRequest, config: MemoryReviewServerConfig) -> None:
    if not config.auth_token:
        raise ValueError("write routes require --token or AGENTIC_MEMORY_REVIEW_TOKEN")
    header = request.headers.get("authorization", "")
    if not hmac.compare_digest(header, f"Bearer {config.auth_token}"):
        raise ValueError("valid bearer token required for write route")


def _namespace(request: MemoryReviewRequest, config: MemoryReviewServerConfig) -> tuple[str, str]:
    return (
        _query_one(request, "userId") or config.default_user_id,
        _query_one(request, "tenantId") or config.default_tenant_id,
    )


def _namespace_from_body(body: dict[str, Any], config: MemoryReviewServerConfig) -> tuple[str, str]:
    return (
        str(body.get("userId") or config.default_user_id),
        str(body.get("tenantId") or config.default_tenant_id),
    )


def _query_one(request: MemoryReviewRequest, key: str) -> str | None:
    values = request.query.get(key) or []
    return values[0] if values else None


def _query_bool(request: MemoryReviewRequest, key: str, default: bool) -> bool:
    value = _query_one(request, key)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _json_body(request: MemoryReviewRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    data = json.loads(request.body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    return data


def _required_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _required_int(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _json_response(data: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> MemoryReviewResponse:
    return MemoryReviewResponse(
        status=status,
        content_type="application/json; charset=utf-8",
        body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        headers=headers or {},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve long-term memory review API")
    parser.add_argument("--host", default=os.getenv("AGENTIC_MEMORY_REVIEW_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AGENTIC_MEMORY_REVIEW_PORT", "8770")))
    parser.add_argument("--path", default=os.getenv("AGENTIC_MEMORY_REVIEW_PATH", "data/memory.json"))
    parser.add_argument("--token", default=os.getenv("AGENTIC_MEMORY_REVIEW_TOKEN"))
    parser.add_argument("--user-id", default=os.getenv("AGENTIC_USER_ID", "local_user"))
    parser.add_argument("--tenant-id", default=os.getenv("AGENTIC_TENANT_ID", "default_tenant"))
    args = parser.parse_args(argv)
    config = MemoryReviewServerConfig(
        host=args.host,
        port=args.port,
        memory_path=args.path,
        auth_token=args.token,
        default_user_id=args.user_id,
        default_tenant_id=args.tenant_id,
    )
    server = ThreadingHTTPServer((config.host, config.port), make_handler(config))
    print(f"Memory review server listening on http://{config.host}:{config.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
