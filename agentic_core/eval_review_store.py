"""eval_review_store — eval 人工审核决策的 SQLite 存储。

`eval_review.py` 适合本地单人流程: 读一个 dataset JSON,写一个新的 golden JSON。
生产协作流程还需要一个服务端真相源: 多个 reviewer 的每次 decision 都应该能被
追加、查询、汇总,而不是只散落在某个 JSON 文件里。

本模块先用 Python 标准库 `sqlite3` 做最小生产形态:

  - SQLiteReviewStore: append-only 保存 review decisions。
  - merge_decisions_into_dataset(): 用 dataset 的 cases 做元数据,用 store 里的
    decisions 做审核状态真相源。
  - CLI: init/import/list/state,方便本地验证和脚本化。

调用关系图:
  eval_server POST /api/reviews/apply ─▶ SQLiteReviewStore.append_decisions()
  eval_server GET /api/reviews/status ─▶ merge_decisions_into_dataset() ─▶ review_state()
  CLI ─▶ init/import/list/state
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .eval_review import format_review_state, load_dataset, review_state
from .memory import now_iso


SCHEMA_VERSION = 1


class SQLiteReviewStore:
    """用 SQLite 保存 eval review decisions。

    这里保存的是“审核动作/决策”,不是完整 dataset。dataset 仍负责提供 case 的
    goal、expectedTools、expectedAnswerContains 等测试元数据;store 负责回答:
    谁在什么 session 对哪个 case 给了什么审核结论。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def append_decisions(self, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """追加保存 review decisions,返回补齐 id/reviewedAt 后的决策列表。"""

        normalized = [_normalize_decision(decision) for decision in decisions]
        if not normalized:
            return []
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO review_decisions (
                    id,
                    case_name,
                    status,
                    reviewer,
                    review_session_id,
                    notes,
                    reviewed_at,
                    judge_labels_json,
                    decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_decision_row(decision) for decision in normalized],
            )
        return normalized

    def import_dataset_decisions(self, dataset: dict[str, Any]) -> list[dict[str, Any]]:
        """把 dataset 里已有的 reviewDecisions 导入 SQLite。"""

        decisions = dataset.get("reviewDecisions")
        if not isinstance(decisions, list):
            return []
        return self.append_decisions([dict(item) for item in decisions if isinstance(item, dict)])

    def list_decisions(
        self,
        case_name: str | None = None,
        reviewer: str | None = None,
        review_session_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按时间顺序列出 review decisions。"""

        where_sql, params = _decision_filters_sql(
            case_name=case_name,
            reviewer=reviewer,
            review_session_id=review_session_id,
        )
        sql = "SELECT decision_json FROM review_decisions" + where_sql
        sql += " ORDER BY reviewed_at ASC, id ASC"
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be >= 0")
            sql += " LIMIT ?"
            params.append(limit)
        if offset:
            if limit is None:
                sql += " LIMIT -1"
            sql += " OFFSET ?"
            params.append(offset)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        decisions: list[dict[str, Any]] = []
        for (raw_json,) in rows:
            data = json.loads(str(raw_json))
            if isinstance(data, dict):
                decisions.append(data)
        return decisions

    def query_decisions(
        self,
        case_name: str | None = None,
        reviewer: str | None = None,
        review_session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页查询 review decisions,返回适合 API 输出的结构。"""

        if limit < 0:
            raise ValueError("limit must be >= 0")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        where_sql, params = _decision_filters_sql(
            case_name=case_name,
            reviewer=reviewer,
            review_session_id=review_session_id,
        )
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM review_decisions" + where_sql, params).fetchone()[0])
        decisions = self.list_decisions(
            case_name=case_name,
            reviewer=reviewer,
            review_session_id=review_session_id,
            limit=limit,
            offset=offset,
        )
        return {
            "schemaVersion": 1,
            "type": "agentic_eval_review_decisions_page",
            "generatedAt": now_iso(),
            "reviewStore": str(self.path),
            "filters": {
                "caseName": case_name,
                "reviewer": reviewer,
                "reviewSessionId": review_session_id,
            },
            "pagination": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "returned": len(decisions),
                "hasMore": offset + len(decisions) < total,
            },
            "decisions": decisions,
        }

    def merge_decisions_into_dataset(
        self,
        dataset: dict[str, Any],
        include_existing: bool = False,
    ) -> dict[str, Any]:
        """返回带 reviewDecisions 的 dataset 副本。

        默认 `include_existing=False`: 当启用 SQLite store 时,数据库是 review
        decisions 真相源。dataset JSON 只提供 cases 元数据,避免同一批 decision 在
        JSON 和 DB 中重复统计。
        """

        merged = {key: value for key, value in dataset.items() if key != "reviewDecisions"}
        cases = dataset.get("cases")
        merged["cases"] = [dict(case) for case in cases if isinstance(case, dict)] if isinstance(cases, list) else []
        existing = dataset.get("reviewDecisions") if include_existing else None
        decisions: list[dict[str, Any]] = []
        if isinstance(existing, list):
            decisions.extend(dict(item) for item in existing if isinstance(item, dict))
        decisions.extend(self.list_decisions())
        merged["reviewDecisions"] = _dedupe_decisions(decisions)
        return merged

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_decisions (
                    id TEXT PRIMARY KEY,
                    case_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reviewer TEXT,
                    review_session_id TEXT,
                    notes TEXT,
                    reviewed_at TEXT NOT NULL,
                    judge_labels_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO review_store_meta (key, value)
                VALUES ('schemaVersion', ?)
                """,
                (str(SCHEMA_VERSION),),
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_decisions_case ON review_decisions(case_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_decisions_reviewer ON review_decisions(reviewer)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_review_decisions_session ON review_decisions(review_session_id)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_decisions_time ON review_decisions(reviewed_at)")


def _normalize_decision(decision: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(decision)
    case_name = normalized.get("caseName")
    status = normalized.get("status")
    if not isinstance(case_name, str) or not case_name:
        raise ValueError("review decision requires caseName")
    if not isinstance(status, str) or not status:
        raise ValueError("review decision requires status")
    if not isinstance(normalized.get("id"), str) or not normalized.get("id"):
        normalized["id"] = f"review_decision_{uuid.uuid4().hex}"
    if not isinstance(normalized.get("reviewedAt"), str) or not normalized.get("reviewedAt"):
        normalized["reviewedAt"] = now_iso()
    labels = normalized.get("judgeLabels")
    if labels is not None and not isinstance(labels, dict):
        raise ValueError("review decision judgeLabels must be an object")
    return normalized


def _decision_row(decision: dict[str, Any]) -> tuple[str, str, str, str | None, str | None, str | None, str, str, str]:
    labels = decision.get("judgeLabels")
    labels_json = json.dumps(labels if isinstance(labels, dict) else {}, ensure_ascii=False, sort_keys=True)
    decision_json = json.dumps(decision, ensure_ascii=False, sort_keys=True)
    return (
        str(decision["id"]),
        str(decision["caseName"]),
        str(decision["status"]),
        _optional_string(decision.get("reviewer")),
        _optional_string(decision.get("reviewSessionId")),
        _optional_string(decision.get("notes")),
        str(decision["reviewedAt"]),
        labels_json,
        decision_json,
    )


def _decision_filters_sql(
    case_name: str | None = None,
    reviewer: str | None = None,
    review_session_id: str | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if case_name:
        clauses.append("case_name = ?")
        params.append(case_name)
    if reviewer:
        clauses.append("reviewer = ?")
        params.append(reviewer)
    if review_session_id:
        clauses.append("review_session_id = ?")
        params.append(review_session_id)
    return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", params)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _dedupe_decisions(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 id 去重;没有 id 的历史 decision 用稳定字段组成临时 key。"""

    seen: set[tuple[Any, ...]] = set()
    output: list[dict[str, Any]] = []
    for item in decisions:
        key: tuple[Any, ...]
        if item.get("id"):
            key = ("id", item.get("id"))
        else:
            key = (
                "legacy",
                item.get("caseName"),
                item.get("status"),
                item.get("reviewer"),
                item.get("reviewSessionId"),
                item.get("reviewedAt"),
            )
        if key in seen:
            continue
        seen.add(key)
        output.append(dict(item))
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Store and inspect eval review decisions in SQLite")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化 SQLite review store")
    init_parser.add_argument("--path", required=True, help="SQLite review store 路径")

    import_parser = subparsers.add_parser("import", help="导入 dataset.reviewDecisions")
    import_parser.add_argument("--path", required=True, help="SQLite review store 路径")
    import_parser.add_argument("--input", required=True, help="dataset JSON")
    import_parser.add_argument("--json", action="store_true", help="输出 JSON")

    list_parser = subparsers.add_parser("list", help="列出 store 中的 review decisions")
    list_parser.add_argument("--path", required=True, help="SQLite review store 路径")
    list_parser.add_argument("--case-name", help="只查看某个 case")
    list_parser.add_argument("--reviewer", help="只查看某个 reviewer")
    list_parser.add_argument("--review-session-id", help="只查看某个 review session")
    list_parser.add_argument("--limit", type=int, help="最多返回多少条")
    list_parser.add_argument("--offset", type=int, default=0, help="跳过多少条")
    list_parser.add_argument("--json", action="store_true", help="输出 JSON")

    query_parser = subparsers.add_parser("query", help="分页查询 store 中的 review decisions")
    query_parser.add_argument("--path", required=True, help="SQLite review store 路径")
    query_parser.add_argument("--case-name", help="只查看某个 case")
    query_parser.add_argument("--reviewer", help="只查看某个 reviewer")
    query_parser.add_argument("--review-session-id", help="只查看某个 review session")
    query_parser.add_argument("--limit", type=int, default=50, help="最多返回多少条")
    query_parser.add_argument("--offset", type=int, default=0, help="跳过多少条")
    query_parser.add_argument("--json", action="store_true", help="输出 JSON")

    state_parser = subparsers.add_parser("state", help="基于 dataset cases + store decisions 输出审核状态")
    state_parser.add_argument("--path", required=True, help="SQLite review store 路径")
    state_parser.add_argument("--input", required=True, help="dataset JSON")
    state_parser.add_argument("--score-tolerance", type=int, default=10, help="judge 分数最大允许差")
    state_parser.add_argument("--json", action="store_true", help="输出 JSON")

    args = parser.parse_args(argv)
    store = SQLiteReviewStore(args.path)

    if args.command == "init":
        print(f"Initialized review store: {store.path}")
        return 0

    if args.command == "import":
        imported = store.import_dataset_decisions(load_dataset(args.input))
        result = {
            "type": "agentic_eval_review_store_import",
            "path": str(store.path),
            "importedDecisions": len(imported),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Imported {len(imported)} review decisions into {store.path}")
        return 0

    if args.command == "list":
        decisions = store.list_decisions(
            case_name=args.case_name,
            reviewer=args.reviewer,
            review_session_id=args.review_session_id,
            limit=args.limit,
            offset=args.offset,
        )
        if args.json:
            print(json.dumps(decisions, ensure_ascii=False, indent=2))
        else:
            print(_format_decisions(decisions))
        return 0

    if args.command == "query":
        page = store.query_decisions(
            case_name=args.case_name,
            reviewer=args.reviewer,
            review_session_id=args.review_session_id,
            limit=args.limit,
            offset=args.offset,
        )
        if args.json:
            print(json.dumps(page, ensure_ascii=False, indent=2))
        else:
            print(_format_decision_page(page))
        return 0

    dataset = store.merge_decisions_into_dataset(load_dataset(args.input))
    state = review_state(dataset, score_tolerance=args.score_tolerance)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        print(format_review_state(state))
    return 0


def _format_decisions(decisions: list[dict[str, Any]]) -> str:
    if not decisions:
        return "No review decisions."
    lines = ["Eval Review Decisions"]
    for item in decisions:
        lines.append(
            f"- {item.get('caseName', '')}: {item.get('status', '')} "
            f"reviewer={item.get('reviewer') or ''} session={item.get('reviewSessionId') or ''}"
        )
    return "\n".join(lines)


def _format_decision_page(page: dict[str, Any]) -> str:
    pagination = page.get("pagination")
    pagination_data = pagination if isinstance(pagination, dict) else {}
    lines = [
        "Eval Review Decisions",
        (
            f"Total: {pagination_data.get('total', 0)}, "
            f"Returned: {pagination_data.get('returned', 0)}, "
            f"Offset: {pagination_data.get('offset', 0)}, "
            f"Limit: {pagination_data.get('limit', 0)}"
        ),
    ]
    decisions = page.get("decisions")
    if isinstance(decisions, list) and decisions:
        for item in decisions:
            if isinstance(item, dict):
                lines.append(
                    f"- {item.get('caseName', '')}: {item.get('status', '')} "
                    f"reviewer={item.get('reviewer') or ''} session={item.get('reviewSessionId') or ''}"
                )
    else:
        lines.append("No review decisions.")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
