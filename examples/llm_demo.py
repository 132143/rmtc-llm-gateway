#!/usr/bin/env python3
"""不用 ROS 也能跑的演示：先问一次网关，再演示"公告 → 异步理解 → 拿提示"。

用法：
    python3 -m rmtc_gateway --port 8000            # 终端 1
    python3 examples/llm_demo.py                    # 终端 2
    python3 examples/llm_demo.py --event "你的公告"  # 换一条公告

注意：默认网关是本地模拟上游，它只证明链路通，不做真实语义理解。
真实理解要连主办方的网关（--url 换成现场地址、--token 换成现场令牌）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# 让"直接 python3 examples/llm_demo.py"也能 import 到仓库里的 rmtc_client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rmtc_client import ClientConfig, EventUnderstanding, LlmGatewayClient  # noqa: E402

DEMO_EVENT = ("第 50 Tick：随机事件预告：25 Tick 后（Tick 75），左上角 [4,4]、"
              "右下角 [9,9] 的矩形区域将发生爆炸，范围内所有存活战车扣除 80 HP；"
              "出生保护免疫。")


def log(level: str, text: str) -> None:
    print(f"[{level}] {text}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/v1/chat/completions")
    parser.add_argument("--token", default="practice-A")
    parser.add_argument("--event", default=DEMO_EVENT)
    parser.add_argument("--timeout", type=float, default=1.5)
    args = parser.parse_args(argv)

    cfg = ClientConfig(url=args.url, token=args.token, timeout_s=args.timeout,
                       min_interval_s=1.0)
    client = LlmGatewayClient(cfg, logger=log)

    print("== 1. 同步调用一次 ==", flush=True)
    result = client.ask("用一句话说明：你的比赛目标是什么？", max_tokens=120)
    print(result, flush=True)

    print("\n== 2. 异步事件理解（不阻塞动作窗）==", flush=True)
    print("（客户端自带 1 秒冷却：两次调用不能贴在一起，省额度也省动作窗）", flush=True)
    time.sleep(1.1)
    understanding = EventUnderstanding(client, max_age_ticks=20, logger=log)
    tick = 50
    session = "demo-match"
    understanding.offer(session, tick, args.event, mission_text="至少收集两个普通资源",
                        position=(8, 8), map_size=(24, 24), seat="A")
    for step in range(40):
        hint = understanding.hint(session, tick)
        if hint:
            print(f"拿到提示（Tick {hint['tick']}）：{hint['advice']}", flush=True)
            if hint["parsed"]:
                print(f"结构化字段：{hint['parsed']}", flush=True)
            break
        time.sleep(0.1)
    else:
        print("等不到结果：本地策略继续跑（这就是降级路径）", flush=True)
    print("\n统计：", understanding.stats(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
