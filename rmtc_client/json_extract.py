"""从模型输出里安全地取出第一个 JSON 对象。

模型很爱加代码围栏、前缀说明、结尾客套话；比赛时解析失败不能崩，只能降级。
字符串感知保证了 "内容里带花括号" 的情况也不会截错（例如公告原文含 {}）。
"""

from __future__ import annotations

import json
import re


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


def repair_json_object(text: str) -> dict | None:
    """对常见"模型输出坏 JSON"做修复（思路借鉴开源项目 outputguard 的修复策略集）。

    只做确定安全的修复，修不好就返回 None —— 绝不猜内容：
      1. 去掉 Markdown 代码围栏
      2. 去掉 // 与 /* */ 注释
      3. 单引号当字符串引号
      4. 去掉对象/数组尾部的多余逗号
      5. Python 风格的 True/False/None
      6. 截断的 JSON：补齐缺失的 } 和 ]
    """
    if not isinstance(text, str) or not text.strip():
        return None
    source = strip_code_fence(text)
    start = source.find("{")
    if start < 0:
        return None
    body = source[start:]

    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = body.replace("'", '"') if body.count("'") > 0 and body.count('"') == 0 else body
    body = re.sub(r"\bTrue\b", "true", body)
    body = re.sub(r"\bFalse\b", "false", body)
    body = re.sub(r"\bNone\b", "null", body)
    body = re.sub(r",\s*([}\]])", r"\1", body)

    # 补全被截断的括号
    depth_object = body.count("{") - body.count("}")
    depth_array = body.count("[") - body.count("]")
    candidate = body + ("]" * max(0, depth_array)) + ("}" * max(0, depth_object))
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    for attempt in (body, candidate):
        end = attempt.rfind("}")
        if end < 0:
            continue
        try:
            value = json.loads(attempt[: end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


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
    # 正规解析失败时，再用修复策略试一次（模型偶尔会吐注释/单引号/截断）
    return repair_json_object(text)
