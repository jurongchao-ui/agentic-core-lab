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
