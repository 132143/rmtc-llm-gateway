"""从网关审计日志估算 token 花费。

用法：
    python3 scripts/cost.py gateway-audit.jsonl
    python3 scripts/cost.py audit.jsonl --in-price 2 --out-price 8    # 元/百万 token

默认单价按公开牌价的保守值：输入 2 元/M、输出 8 元/M（实际以账单为准）。
只统计带 usage 的记录（上游返回了 usage 的那些）。
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="网关审计日志（jsonl）")
    parser.add_argument("--in-price", type=float, default=2.0, help="输入 元/百万 token")
    parser.add_argument("--out-price", type=float, default=8.0, help="输出 元/百万 token")
    args = parser.parse_args(argv)

    calls = 0
    prompt = completion = 0
    models = {}
    for line in open(args.path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        p = record.get("prompt_tokens")
        c = record.get("completion_tokens")
        if not isinstance(p, int) or not isinstance(c, int):
            continue
        calls += 1
        prompt += p
        completion += c
        model = record.get("model") or "?"
        models[model] = models.get(model, 0) + 1

    cost = prompt / 1e6 * args.in_price + completion / 1e6 * args.out_price
    print(f"带 usage 的调用：{calls} 次")
    print(f"输入 token：{prompt:,}　输出 token：{completion:,}　合计：{prompt + completion:,}")
    print("模型分布：" + ("，".join(f"{k} {v} 次" for k, v in models.items()) or "无"))
    print(f"估算花费：¥{cost:.4f}（按 输入 {args.in_price} 元/M、输出 {args.out_price} 元/M）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
