from __future__ import annotations

import json

from agentic_core.memory.server import (
    MemoryReviewRequest,
    MemoryReviewServerConfig,
    handle_memory_review_request,
)
from agentic_core.memory.store import JsonMemoryStore
from agentic_core.runtime.schemas import MemoryRecord


def test_memory_review_health_route() -> None:
    response = handle_memory_review_request("/health", MemoryReviewServerConfig(auth_token="secret"))
    data = response_json(response)

    assert response.status == 200
    assert data == {
        "ok": True,
        "service": "agentic_memory_review_server",
        "authRequired": True,
        "writeAuthRequired": True,
    }


def test_memory_review_list_route_returns_namespace_memories(tmp_path) -> None:
    path = seed_memory_store(tmp_path)

    response = handle_memory_review_request(
        "/api/memories?userId=user_a&tenantId=tenant_a",
        MemoryReviewServerConfig(memory_path=path),
    )
    data = response_json(response)

    assert response.status == 200
    assert data["type"] == "agentic_memory_review_list"
    assert data["count"] == 1
    assert data["memories"][0]["id"] == "memory_1"
    assert data["memories"][0]["userId"] == "user_a"


def test_memory_review_get_routes_require_token_when_configured(tmp_path) -> None:
    path = seed_memory_store(tmp_path)

    response = handle_memory_review_request(
        "/api/memories?userId=user_a&tenantId=tenant_a",
        MemoryReviewServerConfig(memory_path=path, auth_token="secret"),
    )

    assert response.status == 401
    assert response_json(response)["error"] == "unauthorized"


def test_memory_review_archive_route_requires_write_token(tmp_path) -> None:
    path = seed_memory_store(tmp_path)

    response = handle_memory_review_request(
        MemoryReviewRequest(
            method="POST",
            path="/api/memories/archive",
            body=json.dumps({"memoryId": "memory_1", "reason": "reviewed"}).encode("utf-8"),
        ),
        MemoryReviewServerConfig(memory_path=path),
    )

    assert response.status == 400
    assert "write routes require" in response_json(response)["message"]


def test_memory_review_archive_route_persists_change(tmp_path) -> None:
    path = seed_memory_store(tmp_path)

    response = handle_memory_review_request(
        MemoryReviewRequest(
            method="POST",
            path="/api/memories/archive",
            headers={"authorization": "Bearer secret"},
            body=json.dumps(
                {
                    "memoryId": "memory_1",
                    "reason": "人工审核归档",
                    "userId": "user_a",
                    "tenantId": "tenant_a",
                }
            ).encode("utf-8"),
        ),
        MemoryReviewServerConfig(memory_path=path, auth_token="secret"),
    )
    data = response_json(response)
    loaded = JsonMemoryStore(path)

    assert response.status == 200
    assert data["type"] == "agentic_memory_review_archive"
    assert data["memory"]["status"] == "archived"
    assert loaded.long_term_memories[0].archive_reason == "人工审核归档"


def test_memory_review_importance_route_persists_change(tmp_path) -> None:
    path = seed_memory_store(tmp_path)

    response = handle_memory_review_request(
        MemoryReviewRequest(
            method="POST",
            path="/api/memories/importance",
            headers={"authorization": "Bearer secret"},
            body=json.dumps(
                {
                    "memoryId": "memory_1",
                    "importance": 88,
                    "userId": "user_a",
                    "tenantId": "tenant_a",
                }
            ).encode("utf-8"),
        ),
        MemoryReviewServerConfig(memory_path=path, auth_token="secret"),
    )
    data = response_json(response)
    loaded = JsonMemoryStore(path)

    assert response.status == 200
    assert data["memory"]["importance"] == 88
    assert loaded.long_term_memories[0].importance == 88


def test_memory_review_conflicts_and_resolve_routes(tmp_path) -> None:
    path = seed_conflicting_memory_store(tmp_path)

    conflicts_response = handle_memory_review_request(
        "/api/memories/conflicts?userId=user_a&tenantId=tenant_a",
        MemoryReviewServerConfig(memory_path=path),
    )
    conflicts = response_json(conflicts_response)

    resolve_response = handle_memory_review_request(
        MemoryReviewRequest(
            method="POST",
            path="/api/memories/resolve-conflict",
            headers={"authorization": "Bearer secret"},
            body=json.dumps(
                {
                    "keepMemoryId": "memory_3",
                    "reason": "保留最新学习偏好",
                    "userId": "user_a",
                    "tenantId": "tenant_a",
                }
            ).encode("utf-8"),
        ),
        MemoryReviewServerConfig(memory_path=path, auth_token="secret"),
    )
    resolved = response_json(resolve_response)
    loaded = JsonMemoryStore(path)

    assert conflicts_response.status == 200
    assert conflicts["count"] == 1
    assert resolve_response.status == 200
    assert resolved["archivedCount"] == 1
    assert loaded.long_term_memories[0].status == "archived"


def test_memory_review_rejects_cross_namespace_write(tmp_path) -> None:
    path = seed_memory_store(tmp_path)

    response = handle_memory_review_request(
        MemoryReviewRequest(
            method="POST",
            path="/api/memories/archive",
            headers={"authorization": "Bearer secret"},
            body=json.dumps(
                {
                    "memoryId": "memory_1",
                    "reason": "wrong namespace",
                    "userId": "user_b",
                    "tenantId": "tenant_a",
                }
            ).encode("utf-8"),
        ),
        MemoryReviewServerConfig(memory_path=path, auth_token="secret"),
    )

    assert response.status == 400
    assert "namespace" in response_json(response)["message"]


def response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def seed_memory_store(tmp_path) -> str:
    path = tmp_path / "memory.json"
    store = JsonMemoryStore(path)
    store.add_long_term_memory(
        "preference",
        "用户偏好: 每次 30 分钟",
        "test",
        {},
        user_id="user_a",
        tenant_id="tenant_a",
    )
    store.add_long_term_memory(
        "preference",
        "用户偏好: 每次 45 分钟",
        "other user",
        {},
        user_id="user_b",
        tenant_id="tenant_a",
    )
    return str(path)


def seed_conflicting_memory_store(tmp_path) -> str:
    path = seed_memory_store(tmp_path)
    store = JsonMemoryStore(path)
    store.long_term_memories.append(
        MemoryRecord(
            id="memory_3",
            memory_type="preference",
            text="用户偏好: 学习任务每次控制在 45 分钟",
            reason="manual import",
            scores={},
            created_at="2026-07-04T00:00:00+00:00",
            updated_at="2026-07-04T00:00:00+00:00",
            user_id="user_a",
            tenant_id="tenant_a",
        )
    )
    store.save()
    return str(path)
