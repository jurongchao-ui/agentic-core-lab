from __future__ import annotations

from agentic_core.trace_view import format_run_brief, resolve_trace_mode


def test_resolve_trace_mode(monkeypatch) -> None:
    monkeypatch.delenv("AGENTIC_TRACE", raising=False)
    assert resolve_trace_mode("brief") == "brief"
    for value, expected in [("off", "off"), ("json", "json"), ("BRIEF", "brief"), ("garbage", "brief")]:
        monkeypatch.setenv("AGENTIC_TRACE", value)
        assert resolve_trace_mode("brief") == expected


def _sample_result() -> dict:
    return {
        "answer": "目标完成\n第二行",
        "memoryDecision": {
            "save": False,
            "memory_type": "none",
            "text": "",
            "metadata": {
                "source": "rule_fallback",
                "error": "boom",
                "rawModelOutput": "模型说了一堆非法内容",
            },
        },
        "trace": [
            {
                "step": 1,
                "action": {"toolName": "calculator", "input": {"expression": "1+1"}, "source": "hermes", "metadata": {}},
                "observation": {"ok": True, "output": 2, "elapsed_ms": 5},
            },
            {
                "step": 2,
                "action": {
                    "toolName": "note.add",
                    "input": {"text": "n"},
                    "source": "rule_fallback",
                    "metadata": {"error": "bad json", "rawModelOutput": "不是JSON"},
                },
                "observation": {"ok": True, "output": {"text": "n"}, "elapsed_ms": 3},
            },
        ],
    }


def test_format_run_brief_shows_process_and_fallback() -> None:
    out = format_run_brief(_sample_result())
    assert "[记忆] rule_fallback" in out
    assert "boom" in out and "模型说了一堆非法内容" in out  # 记忆回退原因 + 原始输出
    assert "Step 1" in out and "calculator" in out
    assert "Step 2" in out and "note.add" in out
    assert "bad json" in out and "不是JSON" in out  # 步骤回退原因 + 原始输出
    assert "[回答] 目标完成" in out
    assert "第二行" not in out  # 只取答案首行
