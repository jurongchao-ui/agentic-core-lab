from __future__ import annotations

import json
from typing import Any

from agentic_core.memory_policy import LlmMemoryPolicy


class FakeClient:
    """假 Ollama client: 返回预设内容或抛异常,匹配 OllamaClient.chat 的返回形状。"""

    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self._content = content
        self._error = error

    def chat(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {"message": {"content": self._content}}


def client_returning(payload: dict[str, Any]) -> FakeClient:
    return FakeClient(content=json.dumps(payload, ensure_ascii=False))


def test_extracts_preference() -> None:
    policy = LlmMemoryPolicy(
        client_returning(
            {
                "save": True,
                "type": "preference",
                "text": "用户偏好: 学习任务每次控制在30分钟以内",
                "sensitive": False,
                "confidence": 90,
                "reason": "稳定偏好",
            }
        )
    )
    decision = policy.evaluate("以后安排学习任务时，每次控制在30分钟以内")
    assert decision.save is True
    assert decision.memory_type == "preference"


def test_program_vetoes_sensitive_text() -> None:
    """核心控制测试: 模型说可存且未标记敏感,程序正则仍否决。安全不依赖模型。"""
    policy = LlmMemoryPolicy(
        client_returning(
            {
                "save": True,
                "type": "user_profile",
                "text": "用户密码",
                "sensitive": False,
                "confidence": 99,
                "reason": "model wrongly wants to save",
            }
        )
    )
    decision = policy.evaluate("我的密码是 abcd1234")
    assert decision.save is False
    assert decision.memory_type == "none"


def test_model_sensitive_flag_vetoes() -> None:
    policy = LlmMemoryPolicy(
        client_returning(
            {
                "save": True,
                "type": "user_profile",
                "text": "some secret",
                "sensitive": True,
                "confidence": 95,
                "reason": "flagged",
            }
        )
    )
    decision = policy.evaluate("这是我的一段私密信息")
    assert decision.save is False


def test_sensitive_rejection_sets_stable_signal() -> None:
    """LLM 路径的敏感拒绝要写入结构化 sensitivity_risk 信号,供 ResponsePolicy safety 档使用。"""
    # 模型说 sensitive=false 且想保存,但程序侧正则命中 -> 一票否决,并写信号。
    policy = LlmMemoryPolicy(
        client_returning(
            {
                "save": True,
                "type": "user_profile",
                "text": "用户密码",
                "sensitive": False,
                "confidence": 99,
                "reason": "model wrongly wants to save",
            }
        )
    )
    decision = policy.evaluate("我的密码是 abcd1234")
    assert decision.save is False
    assert decision.scores.get("sensitivity_risk", 0) >= 3


def test_low_confidence_not_saved() -> None:
    policy = LlmMemoryPolicy(
        client_returning(
            {
                "save": True,
                "type": "preference",
                "text": "也许是偏好",
                "sensitive": False,
                "confidence": 30,
                "reason": "not sure",
            }
        )
    )
    decision = policy.evaluate("我可能喜欢短一点的任务")
    assert decision.save is False


def test_malformed_confidence_does_not_crash() -> None:
    """confidence 为 null / 非数字 / 0-1 小数时,不能抛异常拖进规则兜底。"""
    for bad_confidence in (None, "high", 0.9):
        policy = LlmMemoryPolicy(
            client_returning(
                {
                    "save": True,
                    "type": "user_profile",
                    "text": "前端开发工程师，会 Node.js 和 React",
                    "sensitive": False,
                    "confidence": bad_confidence,
                    "reason": "user profile",
                }
            )
        )
        decision = policy.evaluate("我是一名前端开发 会node react")
        assert decision.save is True, bad_confidence
        assert "fallback" not in decision.reason, bad_confidence


def test_captures_raw_output_on_success() -> None:
    content = json.dumps(
        {"save": True, "type": "preference", "text": "x", "confidence": 90}, ensure_ascii=False
    )
    policy = LlmMemoryPolicy(FakeClient(content=content))
    decision = policy.evaluate("以后每次学习控制在30分钟")
    assert decision.metadata["source"] == "llm"
    assert decision.metadata["rawModelOutput"] == content


def test_captures_fallback_metadata_on_error() -> None:
    policy = LlmMemoryPolicy(FakeClient(error=RuntimeError("Ollama is unavailable")))
    decision = policy.evaluate("以后安排学习任务时，每次控制在30分钟以内")
    assert decision.metadata["source"] == "rule_fallback"
    assert "Ollama is unavailable" in decision.metadata["error"]


def test_fallback_on_error() -> None:
    policy = LlmMemoryPolicy(FakeClient(error=RuntimeError("Ollama is unavailable")))
    decision = policy.evaluate("以后安排学习任务时，每次控制在30分钟以内")
    assert decision.save is True  # 由规则版兜底判定
    assert "fallback" in decision.reason


def test_fallback_on_bad_json() -> None:
    policy = LlmMemoryPolicy(FakeClient(content="这不是 JSON"))
    decision = policy.evaluate("以后安排学习任务时，每次控制在30分钟以内")
    assert decision.save is True
    assert "fallback" in decision.reason
