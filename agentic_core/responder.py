from __future__ import annotations

import json
from typing import Any

from .ollama_client import OllamaClient


# Ollama 不可用/返回空时的兜底回复: 说明能力,引导用户,而不是留白。
FALLBACK_REPLY = "我可以帮你计算、记笔记、管理待办,或记住你的长期偏好。你想做什么?"


class LlmResponder:
    """把“闲聊回复”和“任务执行”分开。

    planner 只负责选工具。当本轮没有任何工具要调用(纯闲聊/陈述)时,
    Agent 交给 responder 用自然语言回话,而不是套用任务执行报告模板。

    和项目其它 LLM 组件一样: LLM 不可用或返回空,就回退到一句固定的能力引导语。
    """

    def __init__(self, client: OllamaClient) -> None:
        self.client = client

    def reply(self, goal: str, memory_snapshot: dict[str, Any]) -> str:
        """根据用户这句话 + 长期记忆,生成一句自然语言回复。"""
        try:
            raw = self.client.chat(self._messages(goal, memory_snapshot))
            content = raw.get("message", {}).get("content", "")
            text = str(content).strip()
            return text or FALLBACK_REPLY
        except Exception:
            return FALLBACK_REPLY

    def _messages(self, goal: str, memory_snapshot: dict[str, Any]) -> list[dict[str, str]]:
        memories = [m.get("text", "") for m in memory_snapshot.get("longTermMemories", [])]
        return [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant inside a small learning agent. "
                    "Reply naturally and concisely in the user's language. "
                    "You may use the known long-term memories as context, "
                    "but do not invent facts about the user."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"message": goal, "knownMemories": memories}, ensure_ascii=False
                ),
            },
        ]
