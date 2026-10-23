"""从模型输出里安全地取出第一个 JSON 对象。

模型很爱加代码围栏、前缀说明、结尾客套话；比赛时解析失败不能崩，只能降级。
字符串感知保证了 "内容里带花括号" 的情况也不会截错（例如公告原文含 {}）。
"""

from __future__ import annotations

import json


def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 这类围栏。"""
    out = text.strip()
    if not out.startswith("```"):
        return out
    body = out[3:]
    newline = body.find("\n")
    if newline >= 0:
        body = body[newline + 1:]
    end = body.rfind("```")
    if end >= 0:
        body = body[:end]
    return body.strip()


def extract_first_json_object(text: str) -> dict | None:
    """返回第一个能被 json.loads 接受的 {...}；找不到或不是对象时返回 None。"""
    if not isinstance(text, str):
        return None
    source = strip_code_fence(text)
    start = source.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(source)):
            char = source[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(source[start:index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
        start = source.find("{", start + 1)
    return None
