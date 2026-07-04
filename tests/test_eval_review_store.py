from __future__ import annotations

import json

import pytest

from evalops.review import review_state
from evalops.review_store import SQLiteReviewStore, main


def test_sqlite_review_store_appends_and_lists_decisions(tmp_path) -> None:
    store = SQLiteReviewStore(tmp_path / "reviews.db")

    stored = store.append_decisions(
        [
            {
                "caseName": "case_a",
                "status": "approved",
                "reviewer": "jr",
                "reviewSessionId": "session_1",
                "judgeLabels": {"expectedJudgeScore": 95, "expectedJudgePassed": True},
            }
        ]
    )
    decisions = store.list_decisions()

    assert len(stored) == 1
    assert stored[0]["id"].startswith("review_decision_")
    assert stored[0]["reviewedAt"]
    assert decisions == stored
    assert store.list_decisions(case_name="case_a")[0]["reviewer"] == "jr"
    assert store.list_decisions(case_name="missing") == []


def test_sqlite_review_store_queries_decisions_with_pagination_and_filters(tmp_path) -> None:
    store = SQLiteReviewStore(tmp_path / "reviews.db")
    store.append_decisions(
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

    page = store.query_decisions(reviewer="a", limit=1, offset=1)
    session_page = store.query_decisions(review_session_id="session_1", limit=10)

    assert page["type"] == "agentic_eval_review_decisions_page"
    assert page["filters"]["reviewer"] == "a"
    assert page["pagination"] == {
        "total": 2,
        "limit": 1,
        "offset": 1,
        "returned": 1,
        "hasMore": False,
    }
    assert page["decisions"][0]["caseName"] == "case_c"
    assert [item["caseName"] for item in session_page["decisions"]] == ["case_a", "case_b"]


def test_sqlite_review_store_validates_required_fields(tmp_path) -> None:
    store = SQLiteReviewStore(tmp_path / "reviews.db")

    with pytest.raises(ValueError, match="caseName"):
        store.append_decisions([{"status": "approved"}])

    with pytest.raises(ValueError, match="status"):
        store.append_decisions([{"caseName": "case_a"}])


def test_sqlite_review_store_imports_dataset_and_builds_state(tmp_path) -> None:
    store = SQLiteReviewStore(tmp_path / "reviews.db")
    imported = store.import_dataset_decisions(sample_reviewed_dataset())

    merged = store.merge_decisions_into_dataset(sample_base_dataset())
    state = review_state(merged, score_tolerance=5)

    assert len(imported) == 2
    assert merged["reviewDecisions"][0]["caseName"] == "case_a"
    assert state["summary"]["totalCases"] == 2
    assert state["summary"]["totalReviewDecisions"] == 2
    assert state["summary"]["conflictCases"] == 0
    by_case = {item["caseName"]: item for item in state["cases"]}
    assert by_case["case_a"]["currentStatus"] == "approved"
    assert by_case["case_b"]["currentStatus"] == "rejected"


def test_sqlite_review_store_ignores_dataset_decisions_by_default(tmp_path) -> None:
    store = SQLiteReviewStore(tmp_path / "reviews.db")
    store.append_decisions(
        [
            {
                "caseName": "case_a",
                "status": "approved",
                "reviewer": "from_store",
            }
        ]
    )

    dataset = sample_reviewed_dataset()
    merged = store.merge_decisions_into_dataset(dataset)
    merged_with_existing = store.merge_decisions_into_dataset(dataset, include_existing=True)

    assert len(merged["reviewDecisions"]) == 1
    assert merged["reviewDecisions"][0]["reviewer"] == "from_store"
    assert len(merged_with_existing["reviewDecisions"]) == 3


def test_sqlite_review_store_cli_import_list_and_state(tmp_path, capsys) -> None:
    store_path = tmp_path / "reviews.db"
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(sample_reviewed_dataset(), ensure_ascii=False), encoding="utf-8")

    init_code = main(["init", "--path", str(store_path)])
    import_code = main(["import", "--path", str(store_path), "--input", str(dataset_path), "--json"])
    list_code = main(["list", "--path", str(store_path), "--case-name", "case_a", "--json"])
    query_code = main(["query", "--path", str(store_path), "--reviewer", "a", "--limit", "1", "--json"])
    state_code = main(["state", "--path", str(store_path), "--input", str(dataset_path), "--json"])

    output = capsys.readouterr().out
    chunks = [json.loads(chunk) for chunk in _json_chunks(output)]
    assert init_code == 0
    assert import_code == 0
    assert list_code == 0
    assert query_code == 0
    assert state_code == 0
    assert chunks[0]["importedDecisions"] == 2
    assert chunks[1][0]["caseName"] == "case_a"
    assert chunks[2]["type"] == "agentic_eval_review_decisions_page"
    assert chunks[2]["pagination"]["total"] == 1
    assert chunks[3]["type"] == "agentic_eval_review_state"
    assert chunks[3]["summary"]["totalReviewDecisions"] == 2


def sample_base_dataset() -> dict:
    return {
        "schemaVersion": 1,
        "type": "agentic_eval_dataset",
        "cases": [
            {"name": "case_a", "goal": "帮我计算", "reviewRequired": True},
            {"name": "case_b", "goal": "帮我保存记忆", "reviewRequired": True},
        ],
    }


def sample_reviewed_dataset() -> dict:
    dataset = sample_base_dataset()
    dataset["reviewDecisions"] = [
        {
            "caseName": "case_a",
            "status": "approved",
            "reviewer": "a",
            "reviewSessionId": "session_a",
            "reviewedAt": "2026-07-04T00:00:00+08:00",
            "judgeLabels": {"expectedJudgeScore": 95, "expectedJudgePassed": True},
        },
        {
            "caseName": "case_b",
            "status": "rejected",
            "reviewer": "b",
            "reviewSessionId": "session_b",
            "reviewedAt": "2026-07-04T00:01:00+08:00",
        },
    ]
    return dataset


def _json_chunks(output: str) -> list[str]:
    decoder = json.JSONDecoder()
    chunks: list[str] = []
    index = 0
    while index < len(output):
        while index < len(output) and output[index].isspace():
            index += 1
        if index >= len(output):
            break
        if output.startswith("Initialized review store:", index):
            newline = output.find("\n", index)
            index = len(output) if newline == -1 else newline + 1
            continue
        _, end = decoder.raw_decode(output[index:])
        chunks.append(output[index : index + end])
        index += end
    return chunks
