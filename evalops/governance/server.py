"""eval_server — 本地 eval governance 服务。

这是 `eval_dashboard.py` 的服务端边界层。dashboard 模块负责聚合数据,
server 模块负责:
  - 接收 HTTP 请求。
  - 可选 Bearer Token 认证。
  - 调用已有 dashboard / registry 能力。
  - 返回 HTML 或 JSON。
  - 在受保护的窄接口中应用 dataset review 决策。

本模块刻意只使用 Python 标准库。学习阶段先把“服务端入口、路由、健康检查、
治理 API、认证边界、最小写入边界”立住,不引入 FastAPI / Flask。

调用关系图:
  python -m agentic_core.eval_server --report ... --history ... --dataset ...
    └─▶ ThreadingHTTPServer
        ├─▶ GET /health         -> 服务健康状态
        ├─▶ GET /dashboard      -> EvalDashboard.to_html()
        ├─▶ GET /api/dashboard  -> EvalDashboard.to_dict()
        ├─▶ GET /api/rubrics    -> list_judge_rubrics()
        ├─▶ GET /api/reviews/status -> review_state()
        ├─▶ GET /api/reviews/decisions -> paginated review decisions
        └─▶ POST /api/reviews/apply -> review_dataset() 写出 golden dataset
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from evalops.governance.auth_tokens import verify_signed_token
from evalops.governance.dashboard import build_dashboard
from evalops.judge_registry import list_judge_rubrics
from evalops.review import load_dataset, review_dataset, review_state
from evalops.review_store import SQLiteReviewStore
from agentic_core.observability.event_payloads import validate_event_payload
from agentic_core.observability.event_writer import EventWriter, JsonlEventWriter, redact_event
from agentic_core.memory.store import now_iso
from agentic_core.runtime.schemas import EventRecord
from evalops.governance.tenant_policy import TenantPolicyDecision, TenantPolicyStore


SERVICE_NAME = "agentic_eval_governance_server"
VIEW_SCOPE = "eval.viewer"
REVIEW_SCOPE = "eval.reviewer"


@dataclass(frozen=True)
class EvalHttpRequest:
    """路由层请求对象。

    之前只传 path 足够支撑 GET。加上 auth 之后,路由层必须能看到 method
    和 headers,否则测试会绕过真正的服务端边界。
    """

    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, list[str]] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True)
class EvalHttpResponse:
    """路由层返回值。

    把“请求路径 -> 响应内容”的决策抽成纯函数,测试时不需要真的绑定端口。
    真正的 HTTP handler 只负责把这个对象写回 socket。
    """

    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalAuthPrincipal:
    """通过 Bearer token 认证后的主体。"""

    subject: str
    tenant_id: str
    scopes: set[str]
    token_kind: str


@dataclass(frozen=True)
class EvalServerConfig:
    """本地治理服务配置。

    report_path/history_path/dataset_path 都是只读输入文件。服务启动后每次请求
    都重新读取这些文件,这样本地 eval 产物更新后刷新浏览器即可看到新结果。
    """

    host: str = "127.0.0.1"
    port: int = 8765
    report_path: str | None = None
    history_path: str | None = None
    dataset_path: str | None = None
    review_output_path: str | None = None
    auth_token: str | None = None
    viewer_token: str | None = None
    reviewer_token: str | None = None
    signed_token_secret: str | None = None
    tenant_policy_path: str | None = None
    audit_writer: EventWriter | None = None
    review_store_path: str | None = None


def handle_eval_request(
    request: EvalHttpRequest | str,
    config: EvalServerConfig,
) -> EvalHttpResponse:
    """处理治理服务路由,返回可被 HTTP handler 写出的响应。

    `request` 仍兼容旧的 path 字符串,方便简单测试和教学阅读;生产化路径应传
    `EvalHttpRequest`,这样 method/header/auth 都不会被绕开。
    """

    try:
        normalized_request = _normalize_request(request)
        method = normalized_request.method.upper()
        path = normalized_request.path

        auth_response = _authorize(normalized_request, config)
        if auth_response is not None:
            return auth_response

        if method == "POST" and path == "/api/reviews/apply":
            return _handle_review_apply(normalized_request, config)

        if method != "GET":
            return _json_response(
                {
                    "error": "method_not_allowed",
                    "message": f"Method {method} is not allowed for {path}",
                    "allowedMethods": ["GET"],
                },
                status=405,
                headers={"Allow": "GET"},
            )

        if path in ("", "/", "/dashboard"):
            dashboard = build_dashboard(
                report_path=config.report_path,
                history_path=config.history_path,
                dataset_path=config.dataset_path,
            )
            return _html_response(dashboard.to_html())

        if path == "/health":
            return _json_response(
                {
                    "ok": True,
                    "service": SERVICE_NAME,
                    "authRequired": _auth_required(config),
                }
            )

        if path == "/api/dashboard":
            dashboard = build_dashboard(
                report_path=config.report_path,
                history_path=config.history_path,
                dataset_path=config.dataset_path,
            )
            return _json_response(dashboard.to_dict())

        if path == "/api/rubrics":
            return _json_response(
                {
                    "type": "agentic_eval_judge_rubrics",
                    "rubrics": [rubric.to_dict() for rubric in list_judge_rubrics()],
                }
            )

        if path == "/api/reviews/status":
            if not config.dataset_path:
                return _json_response(
                    {
                        "error": "server_configuration_error",
                        "message": "GET /api/reviews/status requires --dataset.",
                    },
                    status=409,
                )
            dataset = _load_review_state_dataset(config)
            return _json_response(review_state(dataset))

        if path == "/api/reviews/decisions":
            return _handle_review_decisions(normalized_request, config)

        return _json_response(
            {
                "error": "not_found",
                "message": f"Unknown route: {path}",
            },
            status=404,
        )
    except Exception as exc:  # pragma: no cover - 具体分支由集成错误决定
        return _json_response(
            {
                "error": "internal_server_error",
                "message": str(exc),
            },
            status=500,
        )


def make_handler(config: EvalServerConfig) -> type[BaseHTTPRequestHandler]:
    """创建绑定配置的 HTTP handler 类。

    `BaseHTTPRequestHandler` 由标准库 server 实例化,不方便直接传构造参数。
    所以这里用闭包把 config 固定住,测试里也可以传临时文件路径。
    """

    class EvalGovernanceHandler(BaseHTTPRequestHandler):
        server_version = "AgenticEvalGovernanceServer/1.0"

        def do_GET(self) -> None:
            self._send_response(self._handle_request())

        def do_POST(self) -> None:
            self._send_response(self._handle_request())

        def log_message(self, format: str, *args: Any) -> None:
            """关闭默认访问日志,避免测试和 CLI 输出被刷屏。"""

            return

        def _send_response(self, response: EvalHttpResponse) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)

        def _handle_request(self) -> EvalHttpResponse:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(content_length) if content_length > 0 else b""
            request = EvalHttpRequest(
                method=self.command,
                path=path,
                headers={key.lower(): value for key, value in self.headers.items()},
                query=parse_qs(parsed_url.query),
                body=body,
            )
            return handle_eval_request(request, config)

    return EvalGovernanceHandler


def _normalize_request(request: EvalHttpRequest | str) -> EvalHttpRequest:
    if isinstance(request, str):
        parsed_url = urlparse(request)
        return EvalHttpRequest(method="GET", path=parsed_url.path, query=parse_qs(parsed_url.query))
    return request


def _authorize(request: EvalHttpRequest, config: EvalServerConfig) -> EvalHttpResponse | None:
    """可选 Bearer Token 认证 + scope 授权。

    本地学习默认不开启认证。只要配置任意 token,除 `/health` 外所有路由都需要
    `Authorization: Bearer <token>`。

    Scope:
      - eval.viewer: 允许读 dashboard / API。
      - eval.reviewer: 允许 POST /api/reviews/apply。
      - auth_token: 兼容旧配置,等价 admin token,同时拥有 viewer/reviewer。
    """

    required_scope = _required_scope(request)
    if required_scope is None or not _auth_required(config):
        return None
    authorization = request.headers.get("authorization", "")
    expected_prefix = "Bearer "
    if not authorization.startswith(expected_prefix):
        return _unauthorized_response("missing_bearer_token")
    token = authorization[len(expected_prefix) :]
    principal = _principal_for_token(token, config)
    if principal is None:
        return _unauthorized_response("invalid_bearer_token")
    if required_scope not in principal.scopes:
        return _forbidden_response(required_scope, principal.scopes)
    tenant_decision = _tenant_policy_decision(principal, required_scope, config)
    if tenant_decision is not None and not tenant_decision.allowed:
        return _tenant_policy_forbidden_response(tenant_decision, principal)
    return None


def _required_scope(request: EvalHttpRequest) -> str | None:
    if request.path == "/health":
        return None
    if request.method.upper() == "POST" and request.path == "/api/reviews/apply":
        return REVIEW_SCOPE
    return VIEW_SCOPE


def _auth_required(config: EvalServerConfig) -> bool:
    return bool(_configured_token_scopes(config) or config.signed_token_secret)


def _configured_token_scopes(config: EvalServerConfig) -> dict[str, set[str]]:
    token_scopes: dict[str, set[str]] = {}
    if config.auth_token:
        token_scopes.setdefault(config.auth_token, set()).update({VIEW_SCOPE, REVIEW_SCOPE})
    if config.viewer_token:
        token_scopes.setdefault(config.viewer_token, set()).add(VIEW_SCOPE)
    if config.reviewer_token:
        token_scopes.setdefault(config.reviewer_token, set()).update({VIEW_SCOPE, REVIEW_SCOPE})
    return token_scopes


def _principal_for_token(token: str, config: EvalServerConfig) -> EvalAuthPrincipal | None:
    token_scopes = _configured_token_scopes(config)
    for configured_token, scopes in token_scopes.items():
        if hmac.compare_digest(token, configured_token):
            return EvalAuthPrincipal(
                subject="static_token",
                tenant_id="local_static",
                scopes=set(scopes),
                token_kind="static",
            )
    if config.signed_token_secret:
        try:
            claims = verify_signed_token(token, config.signed_token_secret)
        except ValueError:
            return None
        return EvalAuthPrincipal(
            subject=claims.subject,
            tenant_id=claims.tenant,
            scopes=set(claims.scopes),
            token_kind="signed",
        )
    return None


def _tenant_policy_decision(
    principal: EvalAuthPrincipal,
    required_scope: str,
    config: EvalServerConfig,
) -> TenantPolicyDecision | None:
    if not config.tenant_policy_path:
        return None
    return TenantPolicyStore.from_file(config.tenant_policy_path).evaluate(
        tenant_id=principal.tenant_id,
        required_scope=required_scope,
    )


def _handle_review_apply(request: EvalHttpRequest, config: EvalServerConfig) -> EvalHttpResponse:
    """应用 dataset review 决策并写出 golden dataset。

    写入 API 不接受客户端传文件路径。输入和输出路径都来自服务端配置,避免
    一个本地治理服务意外变成“任意文件写入”入口。
    """

    if not _has_configured_scope(config, REVIEW_SCOPE):
        _write_review_apply_audit(
            config,
            success=False,
            payload={
                "input": config.dataset_path,
                "output": config.review_output_path,
                "error": "write_auth_required",
                "errorType": "configuration",
            },
        )
        return _json_response(
            {
                "error": "write_auth_required",
                "message": "POST /api/reviews/apply requires a reviewer/admin token.",
            },
            status=403,
        )
    if not config.dataset_path or not config.review_output_path:
        _write_review_apply_audit(
            config,
            success=False,
            payload={
                "input": config.dataset_path,
                "output": config.review_output_path,
                "error": "POST /api/reviews/apply requires --dataset and --review-output.",
                "errorType": "configuration",
            },
        )
        return _json_response(
            {
                "error": "server_configuration_error",
                "message": "POST /api/reviews/apply requires --dataset and --review-output.",
            },
            status=409,
        )

    payload_or_error = _parse_json_body(request.body)
    if isinstance(payload_or_error, EvalHttpResponse):
        _write_review_apply_audit(
            config,
            success=False,
            payload={
                "input": config.dataset_path,
                "output": config.review_output_path,
                "error": response_error_message(payload_or_error),
                "errorType": "bad_request",
            },
        )
        return payload_or_error
    payload = payload_or_error

    try:
        approve = _string_list(payload.get("approve"), "approve")
        reject = _string_list(payload.get("reject"), "reject")
        reviewer = _optional_str(payload.get("reviewer"), "reviewer")
        review_session_id = _optional_str(payload.get("reviewSessionId"), "reviewSessionId")
        input_dataset = load_dataset(config.dataset_path)
        dataset_for_review = _merge_review_store_decisions(input_dataset, config)
        existing_decision_count = _review_decision_count(dataset_for_review)
        reviewed = review_dataset(
            dataset_for_review,
            approve=approve,
            reject=reject,
            approve_all=_optional_bool(payload.get("approveAll"), "approveAll") or False,
            reviewer=reviewer,
            review_session_id=review_session_id,
            notes=_optional_str(payload.get("notes"), "notes"),
            judge_rubric=_optional_str(payload.get("judgeRubric"), "judgeRubric"),
            judge_rubric_version=_optional_str(payload.get("judgeRubricVersion"), "judgeRubricVersion"),
            expected_judge_score=_optional_int(payload.get("expectedJudgeScore"), "expectedJudgeScore"),
            expected_judge_passed=_optional_bool(payload.get("expectedJudgePassed"), "expectedJudgePassed"),
            judge_score_tolerance=_optional_int(payload.get("judgeScoreTolerance"), "judgeScoreTolerance"),
            judge_notes=_optional_str(payload.get("judgeNotes"), "judgeNotes"),
        )
        stored_decisions = _persist_new_review_decisions(reviewed, existing_decision_count, config)
    except ValueError as exc:
        _write_review_apply_audit(
            config,
            success=False,
            payload={
                "input": config.dataset_path,
                "output": config.review_output_path,
                "error": str(exc),
                "errorType": "bad_request",
            },
        )
        return _json_response({"error": "bad_request", "message": str(exc)}, status=400)
    except Exception as exc:
        _write_review_apply_audit(
            config,
            success=False,
            payload={
                "input": config.dataset_path,
                "output": config.review_output_path,
                "error": str(exc),
                "errorType": exc.__class__.__name__,
            },
        )
        raise

    output_path = Path(config.review_output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    decisions = reviewed.get("reviewDecisions")
    _write_review_apply_audit(
        config,
        success=True,
        payload={
            "input": config.dataset_path,
            "output": config.review_output_path,
            "reviewer": reviewer,
            "reviewSessionId": review_session_id,
            "approve": approve,
            "reject": reject,
            "reviewSummary": reviewed.get("reviewSummary", {}),
            "reviewDecisionsCount": len(decisions) if isinstance(decisions, list) else 0,
            "storedReviewDecisionsCount": len(stored_decisions),
        },
    )
    return _json_response(
        {
            "type": "agentic_eval_review_apply_result",
            "input": config.dataset_path,
            "output": config.review_output_path,
            "reviewStore": config.review_store_path,
            "reviewSummary": reviewed.get("reviewSummary", {}),
            "reviewDecisionsCount": len(decisions) if isinstance(decisions, list) else 0,
            "storedReviewDecisionsCount": len(stored_decisions),
        }
    )


def _load_review_state_dataset(config: EvalServerConfig) -> dict[str, Any]:
    if config.dataset_path is None:
        raise ValueError("review status requires dataset_path")
    dataset = load_dataset(config.dataset_path)
    return _merge_review_store_decisions(dataset, config)


def _merge_review_store_decisions(dataset: dict[str, Any], config: EvalServerConfig) -> dict[str, Any]:
    if not config.review_store_path:
        return dataset
    return SQLiteReviewStore(config.review_store_path).merge_decisions_into_dataset(dataset)


def _review_decision_count(dataset: dict[str, Any]) -> int:
    decisions = dataset.get("reviewDecisions")
    return len(decisions) if isinstance(decisions, list) else 0


def _persist_new_review_decisions(
    reviewed_dataset: dict[str, Any],
    existing_decision_count: int,
    config: EvalServerConfig,
) -> list[dict[str, Any]]:
    decisions = reviewed_dataset.get("reviewDecisions")
    if not config.review_store_path or not isinstance(decisions, list):
        return []
    new_decisions = [dict(item) for item in decisions[existing_decision_count:] if isinstance(item, dict)]
    stored_decisions = SQLiteReviewStore(config.review_store_path).append_decisions(new_decisions)
    for offset, stored_decision in enumerate(stored_decisions):
        decisions[existing_decision_count + offset] = stored_decision
    return stored_decisions


def _handle_review_decisions(request: EvalHttpRequest, config: EvalServerConfig) -> EvalHttpResponse:
    """分页查看 SQLite review store 里的审核流水。"""

    if not config.review_store_path:
        return _json_response(
            {
                "error": "server_configuration_error",
                "message": "GET /api/reviews/decisions requires --review-store.",
            },
            status=409,
        )
    try:
        page = SQLiteReviewStore(config.review_store_path).query_decisions(
            case_name=_query_str(request.query, "caseName"),
            reviewer=_query_str(request.query, "reviewer"),
            review_session_id=_query_str(request.query, "reviewSessionId"),
            limit=_query_int(request.query, "limit", default=50, minimum=0, maximum=200),
            offset=_query_int(request.query, "offset", default=0, minimum=0),
        )
    except ValueError as exc:
        return _json_response({"error": "bad_request", "message": str(exc)}, status=400)
    return _json_response(page)


def _query_str(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    value = values[0]
    return value if value != "" else None


def _query_int(
    query: dict[str, list[str]],
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw_value = _query_str(query, name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def response_error_message(response: EvalHttpResponse) -> str:
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"HTTP {response.status}"
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or f"HTTP {response.status}")
    return f"HTTP {response.status}"


def _write_review_apply_audit(
    config: EvalServerConfig,
    success: bool,
    payload: dict[str, Any],
) -> None:
    if config.audit_writer is None:
        return
    event_type = "eval_review_apply" if success else "eval_review_apply_failed"
    validation = validate_event_payload(event_type, payload)
    event = EventRecord(
        id=f"eval_server_event_{uuid.uuid4().hex}",
        event_type=event_type,
        run_id="eval_server",
        payload=dict(payload),
        created_at=now_iso(),
        source="eval_server",
        level="info" if success else "warn",
        payload_schema_version=validation.schema_version,
        payload_schema_valid=validation.valid,
        payload_schema_errors=list(validation.errors),
    )
    try:
        config.audit_writer.write(redact_event(event))
    except Exception:
        # 审计写入失败不能反过来阻断人工审核主流程。
        return


def _parse_json_body(body: bytes) -> dict[str, Any] | EvalHttpResponse:
    try:
        data = json.loads(body.decode("utf-8") if body else "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _json_response({"error": "bad_json", "message": str(exc)}, status=400)
    if not isinstance(data, dict):
        return _json_response(
            {"error": "bad_request", "message": "JSON body must be an object."},
            status=400,
        )
    return data


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return list(value)


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _optional_bool(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _unauthorized_response(reason: str) -> EvalHttpResponse:
    return _json_response(
        {
            "error": "unauthorized",
            "message": "Valid Bearer token required.",
            "reason": reason,
        },
        status=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden_response(required_scope: str, scopes: set[str]) -> EvalHttpResponse:
    return _json_response(
        {
            "error": "forbidden",
            "message": "Bearer token does not have the required scope.",
            "reason": "insufficient_scope",
            "requiredScope": required_scope,
            "scopes": sorted(scopes),
        },
        status=403,
    )


def _tenant_policy_forbidden_response(
    decision: TenantPolicyDecision,
    principal: EvalAuthPrincipal,
) -> EvalHttpResponse:
    return _json_response(
        {
            "error": "forbidden",
            "message": "Tenant policy does not allow this request.",
            "reason": decision.reason,
            "requiredScope": decision.required_scope,
            "tenantId": decision.tenant_id,
            "subject": principal.subject,
            "tokenKind": principal.token_kind,
            "allowedScopes": sorted(decision.allowed_scopes) if decision.allowed_scopes is not None else None,
        },
        status=403,
    )


def _has_configured_scope(config: EvalServerConfig, required_scope: str) -> bool:
    if any(required_scope in scopes for scopes in _configured_token_scopes(config).values()):
        return True
    return bool(config.signed_token_secret)


def _json_response(
    data: dict[str, Any],
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> EvalHttpResponse:
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    return EvalHttpResponse(
        status=status,
        content_type="application/json; charset=utf-8",
        body=body,
        headers=dict(headers or {}),
    )


def _html_response(
    html: str,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> EvalHttpResponse:
    body = html.encode("utf-8")
    return EvalHttpResponse(
        status=status,
        content_type="text/html; charset=utf-8",
        body=body,
        headers=dict(headers or {}),
    )


def run_server(config: EvalServerConfig) -> None:
    """启动阻塞式本地 HTTP 服务。"""

    server = ThreadingHTTPServer((config.host, config.port), make_handler(config))
    print(f"Agentic Eval Governance Server: http://{config.host}:{config.port}/dashboard")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a local eval governance dashboard/API")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--report", help="eval_harness --json 输出文件")
    parser.add_argument("--history", help="eval_history JSONL 文件")
    parser.add_argument("--dataset", help="eval dataset/golden JSON 文件")
    parser.add_argument("--review-output", help="POST /api/reviews/apply 输出 golden dataset JSON")
    parser.add_argument(
        "--review-store",
        default=os.environ.get("AGENTIC_EVAL_SERVER_REVIEW_STORE"),
        help="可选 SQLite review store 路径;也可用 AGENTIC_EVAL_SERVER_REVIEW_STORE",
    )
    parser.add_argument(
        "--audit-events",
        default=os.environ.get("AGENTIC_EVAL_SERVER_AUDIT_EVENTS"),
        help="可选 JSONL 审计事件输出路径;也可用 AGENTIC_EVAL_SERVER_AUDIT_EVENTS",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("AGENTIC_EVAL_SERVER_TOKEN"),
        help="可选 admin Bearer token,拥有 viewer/reviewer;也可用 AGENTIC_EVAL_SERVER_TOKEN",
    )
    parser.add_argument(
        "--viewer-token",
        default=os.environ.get("AGENTIC_EVAL_SERVER_VIEWER_TOKEN"),
        help="可选只读 Bearer token,拥有 eval.viewer;也可用 AGENTIC_EVAL_SERVER_VIEWER_TOKEN",
    )
    parser.add_argument(
        "--reviewer-token",
        default=os.environ.get("AGENTIC_EVAL_SERVER_REVIEWER_TOKEN"),
        help="可选审核 Bearer token,拥有 eval.viewer/eval.reviewer;也可用 AGENTIC_EVAL_SERVER_REVIEWER_TOKEN",
    )
    parser.add_argument(
        "--signed-token-secret",
        default=os.environ.get("AGENTIC_EVAL_SERVER_SIGNING_SECRET"),
        help="可选 signed claims token HMAC 密钥;也可用 AGENTIC_EVAL_SERVER_SIGNING_SECRET",
    )
    parser.add_argument(
        "--tenant-policy",
        default=os.environ.get("AGENTIC_EVAL_SERVER_TENANT_POLICY"),
        help="可选 tenant policy JSON 路径;也可用 AGENTIC_EVAL_SERVER_TENANT_POLICY",
    )
    args = parser.parse_args(argv)

    run_server(
        EvalServerConfig(
            host=args.host,
            port=args.port,
            report_path=args.report,
            history_path=args.history,
            dataset_path=args.dataset,
            review_output_path=args.review_output,
            auth_token=args.auth_token,
            viewer_token=args.viewer_token,
            reviewer_token=args.reviewer_token,
            signed_token_secret=args.signed_token_secret,
            tenant_policy_path=args.tenant_policy,
            audit_writer=JsonlEventWriter(args.audit_events) if args.audit_events else None,
            review_store_path=args.review_store,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
