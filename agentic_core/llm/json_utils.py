"""json_utils — 从(可能带杂质的)模型输出里抠出 JSON 对象。

功能:
  - extract_json_object(content): 容忍模型在 JSON 外面裹解释文字/markdown,
    用正则取出第一个 {...}; 取不到就抛 ValueError。
  - 放共享模块,避免 planner 和 memory_policy 这两个 sibling 互相依赖。

调用关系图:
  HermesPlanner._parse_action / LlmMemoryPolicy._parse_decision /
  LlmSafetyPolicy(解析模型输出处)  ─▶ extract_json_object(content) ─▶ JSON 字符串
"""

from __future__ import annotations

import re


def extract_json_object(content: str) -> str:
    """从模型输出里取出 JSON object 字符串。

    理想情况:
        content == '{"type":"final","answer":"..."}'

    现实情况:
        模型可能输出 'Here is JSON: {...}'
        所以这里用正则尽量取出第一个 {...}。

    planner 和 memory_policy 都要解析模型输出,所以这个工具放在共享模块里,
    避免两个 sibling 模块互相依赖。
    """
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    match = re.search(r"\{.*\}", stripped, re.S)
    if not match:
        raise ValueError("model did not return a JSON object")
    return match.group(0)
