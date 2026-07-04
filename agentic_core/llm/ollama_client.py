"""ollama_client — 极简 Ollama HTTP 客户端(结构化满足 contracts.LlmClient)。

功能:
  - chat(messages, format_json): 把 messages POST 到本地 Ollama /api/chat 拿回响应。
    format_json=True 传 {"format":"json"} 逼模型只吐 JSON(不是安全边界,仍需上层解析校验,
    但能大幅减少本地小模型输出 markdown/解释文字导致的 fallback)。
  - 只用标准库 urllib(零第三方依赖); 禁用系统代理; Ollama 不可达时包成 RuntimeError。

调用关系图:
  HermesPlanner / LlmMemoryPolicy / LlmResponder / LlmSafetyPolicy(各 LLM 组件)
      └─▶ OllamaClient.chat(messages, format_json) ─▶ Ollama /api/chat
            (测试里用 FakeClient 替身,同样满足 LlmClient 协议)
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class OllamaClient:
    """极简 Ollama HTTP 客户端。

    Ollama 本地服务默认地址是:
        http://localhost:11434

    这个类只封装一个能力:
        把 messages 发到 /api/chat,拿回模型响应。

    这里故意不用第三方 requests 包,只用 Python 标准库 urllib,
    这样项目没有额外依赖,适合学习核心链路。
    """

    def __init__(
        self,
        model: str = "openhermes:latest",
        base_url: str = "http://localhost:11434",
        timeout: float = 30.0,
    ) -> None:
        # 模型名,例如 openhermes:latest 或 llama3.2:latest。
        self.model = model

        # base_url.rstrip("/") 可以避免用户传入 "http://.../" 时拼出双斜杠。
        self.base_url = base_url.rstrip("/")

        # HTTP 请求超时时间,避免 Ollama 卡住时程序无限等待。
        self.timeout = timeout

        # 禁用系统代理。
        # 你的本机之前 curl localhost 时有代理干扰,这里显式让 urllib 不走代理。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def chat(
        self,
        messages: list[dict[str, str]],
        format_json: bool = False,
    ) -> dict[str, Any]:
        """调用 Ollama /api/chat。

        messages 示例:
            [
                {"role": "system", "content": "Return JSON only"},
                {"role": "user", "content": "帮我计算 128 * 7"}
            ]

        format_json=True 时会给 Ollama 传 {"format": "json"}。
        这不是安全边界,仍然要由 Planner/MemoryPolicy 解析和校验,
        但能显著减少本地模型输出 markdown/解释文字导致的 fallback。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if format_json:
            payload["format"] = "json"

        # urllib 需要我们手动构造 Request。
        # data 必须是 bytes,所以 json.dumps 后要 encode("utf-8")。
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # with 会在请求结束后自动关闭 response。
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as error:
            # 把底层网络错误包装成更容易理解的 RuntimeError。
            raise RuntimeError(f"Ollama is unavailable: {error}") from error
