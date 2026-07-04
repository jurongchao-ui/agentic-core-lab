from __future__ import annotations

import json
from pathlib import Path

from evalops.governance.auth_tokens import create_signed_token
from agentic_core.observability.event_writer import MemoryEventWriter
from evalops.governance.server import EvalHttpRequest, EvalServerConfig, handle_eval_request
from agentic_core.runtime.schemas import EventRecord


class BrokenAuditWriter:
    def write(self, event: EventRecord) -> None:
        raise OSError("audit sink unavailable")


def test_health_route_returns_ok() -> None:
    response = handle_eval_request("/health", EvalServerConfig())
    data = response_json(response)

    assert response.status == 200
    assert "application/json" in response.content_type
    assert data == {
        "ok": True,
        "service": "agentic_eval_governance_server",
        "authRequired": False,
    }


def test_api_dashboard_returns_dashboard_json(tmp_path: Path) -> None:
    report_path = write_json(tmp_path / "report.json", sample_report(passed_gate=True))
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())

    response = handle_eval_request(
        "/api/dashboard",
        EvalServerConfig(report_path=str(report_path), dataset_path=str(dataset_path)),
    )
    data = response_json(response)

    assert response.status == 200
    assert "application/json" in response.content_type
    assert data["type"] == "agentic_eval_governance_dashboard"
    assert data["reportSummary"]["passedGate"] is True
    assert data["reviewQueueSummary"]["totalItems"] == 1
    assert data["rubricValidation"]["valid"] is True


def test_dashboard_route_returns_html(tmp_path: Path) -> None:
    report_path = write_json(tmp_path / "report.json", sample_report(passed_gate=True))

    response = handle_eval_request("/dashboard", EvalServerConfig(report_path=str(report_path)))
    body = response_text(response)

    assert response.status == 200
    assert "text/html" in response.content_type
    assert "Agentic Eval Governance Dashboard" in body
    assert "Eval Report" in body


def test_rubrics_route_returns_registered_rubrics() -> None:
    response = handle_eval_request("/api/rubrics", EvalServerConfig())
    data = response_json(response)

    assert response.status == 200
    assert "application/json" in response.content_type
    assert data["type"] == "agentic_eval_judge_rubrics"
    assert any(rubric["name"] == "agentic_core_default" for rubric in data["rubrics"])


def test_review_status_route_returns_multi_user_state(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())

    response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/reviews/status",
            headers={"authorization": "Bearer viewer-secret"},
        ),
        EvalServerConfig(dataset_path=str(dataset_path), viewer_token="viewer-secret"),
    )
    data = response_json(response)

    assert response.status == 200
    assert data["type"] == "agentic_eval_review_state"
    assert data["summary"]["totalCases"] == 1
    assert data["cases"][0]["caseName"] == "case_needs_review"


def test_review_status_route_reads_sqlite_review_store_when_configured(tmp_path: Path) -> None:
    from evalops.review_store import SQLiteReviewStore

    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    store_path = tmp_path / "reviews.db"
    SQLiteReviewStore(store_path).append_decisions(
        [
            {
                "caseName": "case_needs_review",
                "status": "rejected",
                "reviewer": "store_reviewer",
                "reviewSessionId": "store_session",
            }
        ]
    )

    response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/reviews/status",
            headers={"authorization": "Bearer viewer-secret"},
        ),
        EvalServerConfig(
            dataset_path=str(dataset_path),
            viewer_token="viewer-secret",
            review_store_path=str(store_path),
        ),
    )
    data = response_json(response)

    assert response.status == 200
    assert data["summary"]["totalReviewDecisions"] == 1
    assert data["cases"][0]["currentStatus"] == "rejected"
    assert data["cases"][0]["reviewers"] == ["store_reviewer"]
    assert data["cases"][0]["reviewSessions"] == ["store_session"]


def test_review_decisions_route_returns_paginated_sqlite_decisions(tmp_path: Path) -> None:
    from evalops.review_store import SQLiteReviewStore

    store_path = tmp_path / "reviews.db"
    SQLiteReviewStore(store_path).append_decisions(
        [
            {
                "caseName": "case_a",
                "status": "approved",
                "reviewer": "a",
                "reviewSessionId": "session_1",
                "reviewedAt": "2026-07-04T00:00:00+08:00",
            },
            {
                "caseName": "case_b",
                "status": "rejected",
                "reviewer": "b",
                "reviewSessionId": "session_1",
                "reviewedAt": "2026-07-04T00:01:00+08:00",
            },
            {
                "caseName": "case_c",
                "status": "approved",
                "reviewer": "a",
                "reviewSessionId": "session_2",
                "reviewedAt": "2026-07-04T00:02:00+08:00",
            },
        ]
    )

    response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/reviews/decisions",
            headers={"authorization": "Bearer viewer-secret"},
            query={"reviewer": ["a"], "limit": ["1"], "offset": ["1"]},
        ),
        EvalServerConfig(viewer_token="viewer-secret", review_store_path=str(store_path)),
    )
    data = response_json(response)

    assert response.status == 200
    assert data["type"] == "agentic_eval_review_decisions_page"
    assert data["filters"]["reviewer"] == "a"
    assert data["pagination"] == {
        "total": 2,
        "limit": 1,
        "offset": 1,
        "returned": 1,
        "hasMore": False,
    }
    assert data["decisions"][0]["caseName"] == "case_c"


def test_review_decisions_route_parses_query_string_path(tmp_path: Path) -> None:
    from evalops.review_store import SQLiteReviewStore

    store_path = tmp_path / "reviews.db"
    SQLiteReviewStore(store_path).append_decisions(
        [
            {
                "caseName": "case_a",
                "status": "approved",
                "reviewer": "a",
                "reviewedAt": "2026-07-04T00:00:00+08:00",
            },
            {
                "caseName": "case_b",
                "status": "approved",
                "reviewer": "b",
                "reviewedAt": "2026-07-04T00:01:00+08:00",
            },
        ]
    )

    response = handle_eval_request(
        "/api/reviews/decisions?caseName=case_b&limit=10",
        EvalServerConfig(review_store_path=str(store_path)),
    )
    data = response_json(response)

    assert response.status == 200
    assert data["pagination"]["total"] == 1
    assert data["decisions"][0]["caseName"] == "case_b"


def test_review_decisions_route_requires_review_store() -> None:
    response = handle_eval_request("/api/reviews/decisions", EvalServerConfig())
    data = response_json(response)

    assert response.status == 409
    assert data["error"] == "server_configuration_error"


def test_review_decisions_route_rejects_invalid_pagination(tmp_path: Path) -> None:
    from evalops.review_store import SQLiteReviewStore

    store_path = tmp_path / "reviews.db"
    SQLiteReviewStore(store_path)

    response = handle_eval_request(
        "/api/reviews/decisions?limit=201",
        EvalServerConfig(review_store_path=str(store_path)),
    )
    data = response_json(response)

    assert response.status == 400
    assert data["error"] == "bad_request"
    assert "limit must be <= 200" in data["message"]


def test_review_status_route_requires_dataset_config() -> None:
    response = handle_eval_request("/api/reviews/status", EvalServerConfig())
    data = response_json(response)

    assert response.status == 409
    assert data["error"] == "server_configuration_error"


def test_unknown_route_returns_json_404() -> None:
    response = handle_eval_request("/missing", EvalServerConfig())
    data = response_json(response)

    assert response.status == 404
    assert "application/json" in response.content_type
    assert data["error"] == "not_found"


def test_health_route_reports_auth_required_when_token_configured() -> None:
    response = handle_eval_request("/health", EvalServerConfig(auth_token="secret"))
    data = response_json(response)

    assert response.status == 200
    assert data["authRequired"] is True


def test_health_route_reports_auth_required_when_signed_secret_configured() -> None:
    response = handle_eval_request("/health", EvalServerConfig(signed_token_secret="secret"))
    data = response_json(response)

    assert response.status == 200
    assert data["authRequired"] is True


def test_auth_token_required_for_governance_routes() -> None:
    response = handle_eval_request(
        EvalHttpRequest(method="GET", path="/api/dashboard"),
        EvalServerConfig(auth_token="secret"),
    )
    data = response_json(response)

    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert data["error"] == "unauthorized"
    assert data["reason"] == "missing_bearer_token"


def test_invalid_auth_token_is_rejected() -> None:
    response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/dashboard",
            headers={"authorization": "Bearer wrong"},
        ),
        EvalServerConfig(auth_token="secret"),
    )
    data = response_json(response)

    assert response.status == 401
    assert data["reason"] == "invalid_bearer_token"


def test_valid_auth_token_allows_request() -> None:
    response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/rubrics",
            headers={"authorization": "Bearer secret"},
        ),
        EvalServerConfig(auth_token="secret"),
    )
    data = response_json(response)

    assert response.status == 200
    assert data["type"] == "agentic_eval_judge_rubrics"


def test_signed_viewer_token_can_read_but_cannot_apply_review(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    output_path = tmp_path / "golden.json"
    token = create_signed_token(
        secret="signing-secret",
        subject="viewer_1",
        tenant="tenant_a",
        scopes={"eval.viewer"},
    )

    read_response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/rubrics",
            headers={"authorization": f"Bearer {token}"},
        ),
        EvalServerConfig(signed_token_secret="signing-secret"),
    )
    write_response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": f"Bearer {token}"},
            body=json.dumps({"approve": ["case_needs_review"]}).encode("utf-8"),
        ),
        EvalServerConfig(
            dataset_path=str(dataset_path),
            review_output_path=str(output_path),
            signed_token_secret="signing-secret",
        ),
    )
    write_data = response_json(write_response)

    assert read_response.status == 200
    assert write_response.status == 403
    assert write_data["requiredScope"] == "eval.reviewer"
    assert write_data["scopes"] == ["eval.viewer"]
    assert not output_path.exists()


def test_signed_reviewer_token_can_apply_review(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    output_path = tmp_path / "golden.json"
    token = create_signed_token(
        secret="signing-secret",
        subject="reviewer_1",
        tenant="tenant_a",
        scopes={"eval.viewer", "eval.reviewer"},
    )

    response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": f"Bearer {token}"},
            body=json.dumps({"approve": ["case_needs_review"], "reviewer": "reviewer"}).encode("utf-8"),
        ),
        EvalServerConfig(
            dataset_path=str(dataset_path),
            review_output_path=str(output_path),
            signed_token_secret="signing-secret",
        ),
    )
    data = response_json(response)
    saved = json.loads(output_path.read_text(encoding="utf-8"))

    assert response.status == 200
    assert data["reviewSummary"]["approved"] == 1
    assert saved["cases"][0]["reviewer"] == "reviewer"


def test_signed_token_is_checked_against_tenant_policy(tmp_path: Path) -> None:
    policy_path = write_json(tmp_path / "tenant-policy.json", sample_tenant_policy())
    token = create_signed_token(
        secret="signing-secret",
        subject="viewer_1",
        tenant="tenant_viewer",
        scopes={"eval.viewer", "eval.reviewer"},
    )

    read_response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/rubrics",
            headers={"authorization": f"Bearer {token}"},
        ),
        EvalServerConfig(
            signed_token_secret="signing-secret",
            tenant_policy_path=str(policy_path),
        ),
    )
    write_response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": f"Bearer {token}"},
            body=json.dumps({"approve": ["case_needs_review"]}).encode("utf-8"),
        ),
        EvalServerConfig(
            dataset_path=str(write_json(tmp_path / "dataset.json", sample_dataset())),
            review_output_path=str(tmp_path / "golden.json"),
            signed_token_secret="signing-secret",
            tenant_policy_path=str(policy_path),
        ),
    )
    write_data = response_json(write_response)

    assert read_response.status == 200
    assert write_response.status == 403
    assert write_data["reason"] == "scope_not_allowed_for_tenant"
    assert write_data["tenantId"] == "tenant_viewer"
    assert write_data["allowedScopes"] == ["eval.viewer"]


def test_signed_token_is_rejected_when_tenant_disabled_or_unknown(tmp_path: Path) -> None:
    policy_path = write_json(tmp_path / "tenant-policy.json", sample_tenant_policy())
    disabled_token = create_signed_token(
        secret="signing-secret",
        subject="disabled_user",
        tenant="tenant_disabled",
        scopes={"eval.viewer"},
    )
    unknown_token = create_signed_token(
        secret="signing-secret",
        subject="unknown_user",
        tenant="missing_tenant",
        scopes={"eval.viewer"},
    )

    disabled_response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/rubrics",
            headers={"authorization": f"Bearer {disabled_token}"},
        ),
        EvalServerConfig(
            signed_token_secret="signing-secret",
            tenant_policy_path=str(policy_path),
        ),
    )
    unknown_response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/rubrics",
            headers={"authorization": f"Bearer {unknown_token}"},
        ),
        EvalServerConfig(
            signed_token_secret="signing-secret",
            tenant_policy_path=str(policy_path),
        ),
    )

    assert disabled_response.status == 403
    assert response_json(disabled_response)["reason"] == "tenant_disabled"
    assert unknown_response.status == 403
    assert response_json(unknown_response)["reason"] == "unknown_tenant"


def test_static_token_uses_local_static_tenant_when_policy_configured(tmp_path: Path) -> None:
    policy_path = write_json(
        tmp_path / "tenant-policy.json",
        {
            "schemaVersion": 1,
            "tenants": {
                "local_static": {
                    "enabled": True,
                    "allowedScopes": ["eval.viewer"],
                }
            },
        },
    )

    read_response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/rubrics",
            headers={"authorization": "Bearer viewer-secret"},
        ),
        EvalServerConfig(
            viewer_token="viewer-secret",
            tenant_policy_path=str(policy_path),
        ),
    )
    write_response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": "Bearer reviewer-secret"},
            body=json.dumps({"approve": ["case_needs_review"]}).encode("utf-8"),
        ),
        EvalServerConfig(
            dataset_path=str(write_json(tmp_path / "dataset.json", sample_dataset())),
            review_output_path=str(tmp_path / "golden.json"),
            reviewer_token="reviewer-secret",
            tenant_policy_path=str(policy_path),
        ),
    )
    write_data = response_json(write_response)

    assert read_response.status == 200
    assert write_response.status == 403
    assert write_data["tenantId"] == "local_static"
    assert write_data["reason"] == "scope_not_allowed_for_tenant"


def test_signed_token_rejects_expired_or_invalid_token() -> None:
    expired = create_signed_token(
        secret="signing-secret",
        subject="viewer_1",
        scopes={"eval.viewer"},
        ttl_seconds=1,
        now=100,
    )

    expired_response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/rubrics",
            headers={"authorization": f"Bearer {expired}"},
        ),
        EvalServerConfig(signed_token_secret="signing-secret"),
    )
    invalid_response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/rubrics",
            headers={"authorization": "Bearer not-a-token"},
        ),
        EvalServerConfig(signed_token_secret="signing-secret"),
    )

    assert expired_response.status == 401
    assert response_json(expired_response)["reason"] == "invalid_bearer_token"
    assert invalid_response.status == 401
    assert response_json(invalid_response)["reason"] == "invalid_bearer_token"


def test_viewer_token_can_read_but_cannot_apply_review(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    output_path = tmp_path / "golden.json"

    read_response = handle_eval_request(
        EvalHttpRequest(
            method="GET",
            path="/api/rubrics",
            headers={"authorization": "Bearer viewer-secret"},
        ),
        EvalServerConfig(viewer_token="viewer-secret"),
    )
    write_response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": "Bearer viewer-secret"},
            body=json.dumps({"approve": ["case_needs_review"]}).encode("utf-8"),
        ),
        EvalServerConfig(
            dataset_path=str(dataset_path),
            review_output_path=str(output_path),
            viewer_token="viewer-secret",
        ),
    )
    write_data = response_json(write_response)

    assert read_response.status == 200
    assert write_response.status == 403
    assert write_data["error"] == "forbidden"
    assert write_data["requiredScope"] == "eval.reviewer"
    assert write_data["scopes"] == ["eval.viewer"]
    assert not output_path.exists()


def test_reviewer_token_can_apply_review(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    output_path = tmp_path / "golden.json"

    response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": "Bearer reviewer-secret"},
            body=json.dumps({"approve": ["case_needs_review"], "reviewer": "reviewer"}).encode("utf-8"),
        ),
        EvalServerConfig(
            dataset_path=str(dataset_path),
            review_output_path=str(output_path),
            reviewer_token="reviewer-secret",
        ),
    )
    data = response_json(response)
    saved = json.loads(output_path.read_text(encoding="utf-8"))

    assert response.status == 200
    assert data["reviewSummary"]["approved"] == 1
    assert saved["cases"][0]["reviewer"] == "reviewer"


def test_non_get_methods_return_method_not_allowed() -> None:
    response = handle_eval_request(
        EvalHttpRequest(method="POST", path="/api/dashboard"),
        EvalServerConfig(),
    )
    data = response_json(response)

    assert response.status == 405
    assert response.headers["Allow"] == "GET"
    assert data["error"] == "method_not_allowed"


def test_review_apply_requires_auth_token_even_when_auth_disabled(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    output_path = tmp_path / "golden.json"

    response = handle_eval_request(
        EvalHttpRequest(method="POST", path="/api/reviews/apply", body=b"{}"),
        EvalServerConfig(dataset_path=str(dataset_path), review_output_path=str(output_path)),
    )
    data = response_json(response)

    assert response.status == 403
    assert data["error"] == "write_auth_required"
    assert not output_path.exists()


def test_review_apply_requires_configured_output_path(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())

    response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": "Bearer secret"},
            body=json.dumps({"approve": ["case_needs_review"]}).encode("utf-8"),
        ),
        EvalServerConfig(dataset_path=str(dataset_path), auth_token="secret"),
    )
    data = response_json(response)

    assert response.status == 409
    assert data["error"] == "server_configuration_error"


def test_review_apply_writes_reviewed_dataset(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    output_path = tmp_path / "golden.json"
    audit_events: list[EventRecord] = []

    response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": "Bearer secret"},
            body=json.dumps(
                {
                    "approve": ["case_needs_review"],
                    "reviewer": "jr",
                    "notes": "server approved",
                    "judgeRubric": "strict_answer_quality",
                    "judgeRubricVersion": "v1",
                    "expectedJudgeScore": 95,
                    "expectedJudgePassed": True,
                    "judgeScoreTolerance": 5,
                    "judgeNotes": "人工确认",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        ),
        EvalServerConfig(
            dataset_path=str(dataset_path),
            review_output_path=str(output_path),
            auth_token="secret",
            audit_writer=MemoryEventWriter(audit_events),
        ),
    )
    data = response_json(response)
    saved = json.loads(output_path.read_text(encoding="utf-8"))

    assert response.status == 200
    assert data["type"] == "agentic_eval_review_apply_result"
    assert data["reviewSummary"]["approved"] == 1
    assert saved["cases"][0]["reviewRequired"] is False
    assert saved["cases"][0]["reviewStatus"] == "approved"
    assert saved["cases"][0]["reviewer"] == "jr"
    assert saved["cases"][0]["judgeRubric"] == "strict_answer_quality"
    assert saved["cases"][0]["expectedJudgeScore"] == 95
    assert saved["cases"][0]["expectedJudgePassed"] is True
    assert len(audit_events) == 1
    assert audit_events[0].event_type == "eval_review_apply"
    assert audit_events[0].source == "eval_server"
    assert audit_events[0].payload_schema_valid is True
    assert audit_events[0].payload["approve"] == ["case_needs_review"]
    assert audit_events[0].payload["reviewer"] == "jr"


def test_review_apply_persists_new_decisions_to_sqlite_review_store(tmp_path: Path) -> None:
    from evalops.review_store import SQLiteReviewStore

    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    output_path = tmp_path / "golden.json"
    store_path = tmp_path / "reviews.db"

    response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": "Bearer secret"},
            body=json.dumps(
                {
                    "approve": ["case_needs_review"],
                    "reviewer": "jr",
                    "reviewSessionId": "review_session_1",
                    "notes": "server approved",
                }
            ).encode("utf-8"),
        ),
        EvalServerConfig(
            dataset_path=str(dataset_path),
            review_output_path=str(output_path),
            auth_token="secret",
            review_store_path=str(store_path),
        ),
    )
    data = response_json(response)
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    decisions = SQLiteReviewStore(store_path).list_decisions()

    assert response.status == 200
    assert data["storedReviewDecisionsCount"] == 1
    assert data["reviewStore"] == str(store_path)
    assert len(decisions) == 1
    assert decisions[0]["caseName"] == "case_needs_review"
    assert decisions[0]["reviewer"] == "jr"
    assert decisions[0]["reviewSessionId"] == "review_session_1"
    assert decisions[0]["id"].startswith("review_decision_")
    assert saved["reviewDecisions"] == decisions


def test_review_apply_rejects_bad_json(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    output_path = tmp_path / "golden.json"

    response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": "Bearer secret"},
            body=b"{bad json",
        ),
        EvalServerConfig(
            dataset_path=str(dataset_path),
            review_output_path=str(output_path),
            auth_token="secret",
        ),
    )
    data = response_json(response)

    assert response.status == 400
    assert data["error"] == "bad_json"
    assert not output_path.exists()


def test_review_apply_rejects_invalid_body_fields(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    output_path = tmp_path / "golden.json"
    audit_events: list[EventRecord] = []

    response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": "Bearer secret"},
            body=json.dumps({"approve": "case_needs_review"}).encode("utf-8"),
        ),
        EvalServerConfig(
            dataset_path=str(dataset_path),
            review_output_path=str(output_path),
            auth_token="secret",
            audit_writer=MemoryEventWriter(audit_events),
        ),
    )
    data = response_json(response)

    assert response.status == 400
    assert data["error"] == "bad_request"
    assert "approve must be a list of strings" in data["message"]
    assert not output_path.exists()
    assert len(audit_events) == 1
    assert audit_events[0].event_type == "eval_review_apply_failed"
    assert audit_events[0].level == "warn"
    assert audit_events[0].payload_schema_valid is True
    assert audit_events[0].payload["errorType"] == "bad_request"


def test_review_apply_writes_audit_event_for_missing_server_config() -> None:
    audit_events: list[EventRecord] = []

    response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": "Bearer secret"},
            body=b"{}",
        ),
        EvalServerConfig(auth_token="secret", audit_writer=MemoryEventWriter(audit_events)),
    )
    data = response_json(response)

    assert response.status == 409
    assert data["error"] == "server_configuration_error"
    assert len(audit_events) == 1
    assert audit_events[0].event_type == "eval_review_apply_failed"
    assert audit_events[0].payload["errorType"] == "configuration"


def test_review_apply_ignores_audit_writer_failure(tmp_path: Path) -> None:
    dataset_path = write_json(tmp_path / "dataset.json", sample_dataset())
    output_path = tmp_path / "golden.json"

    response = handle_eval_request(
        EvalHttpRequest(
            method="POST",
            path="/api/reviews/apply",
            headers={"authorization": "Bearer secret"},
            body=json.dumps({"approve": ["case_needs_review"], "reviewer": "jr"}).encode("utf-8"),
        ),
        EvalServerConfig(
            dataset_path=str(dataset_path),
            review_output_path=str(output_path),
            auth_token="secret",
            audit_writer=BrokenAuditWriter(),
        ),
    )
    data = response_json(response)

    assert response.status == 200
    assert data["reviewSummary"]["approved"] == 1
    assert output_path.exists()


def response_json(response) -> dict:
    return json.loads(response_text(response))


def response_text(response) -> str:
    return response.body.decode("utf-8")


def sample_report(passed_gate: bool) -> dict:
    return {
        "total": 1,
        "passed": 1 if passed_gate else 0,
        "failed": 0 if passed_gate else 1,
        "passedGate": passed_gate,
        "metrics": {"case_pass_rate": 1.0 if passed_gate else 0.0, "tool_success_rate": 1.0},
        "eventCounts": {"run_completed": 1},
        "gateFailures": [] if passed_gate else ["gate failed"],
        "cases": [],
    }


def sample_dataset() -> dict:
    return {
        "schemaVersion": 1,
        "type": "agentic_eval_dataset",
        "generatedAt": "2026-07-04T00:00:00+00:00",
        "cases": [
            {
                "name": "case_needs_review",
                "goal": "帮我计算",
                "reviewRequired": True,
                "expectedAnswerContains": [],
                "judgeRubric": "agentic_core_default",
                "judgeRubricVersion": "v1",
            }
        ],
        "reviewDecisions": [
            {
                "caseName": "case_needs_review",
                "status": "approved",
                "reviewer": "a",
                "judgeLabels": {"expectedJudgeScore": 100, "expectedJudgePassed": True},
            }
        ],
    }


def sample_tenant_policy() -> dict:
    return {
        "schemaVersion": 1,
        "tenants": {
            "tenant_a": {
                "enabled": True,
                "allowedScopes": ["eval.viewer", "eval.reviewer"],
            },
            "tenant_viewer": {
                "enabled": True,
                "allowedScopes": ["eval.viewer"],
            },
            "tenant_disabled": {
                "enabled": False,
                "allowedScopes": ["eval.viewer", "eval.reviewer"],
            },
        },
    }


def write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path
