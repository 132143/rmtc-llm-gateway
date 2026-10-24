"""录制真实模型对"本场公告"的理解结果，供离线仿真回放（一次录制、零费用复现）。

为什么不在推理时直连模型：仿真要跑几十局，每局几十次调用既慢又贵；而且我们要的是
"同一份真实输出在同样的 prompt 下反复使用"，这正是规则允许的做法（模型判断一次，
程序可复核、可解释）。

用法（在能访问主办方网关/自备服务的机器上跑）：
    export RMTC_AI_URL='<网关>/v1/chat/completions'
    export RMTC_TEAM_TOKEN='<令牌>'
    export RMTC_MODEL='deepseek-chat'
    python3 -m scripts.record_llm_hints --out docs/events_cache.json

输出：{ sha1(最后一条公告) : 模型原始输出 }，回放时由 contestant/llm_replay.py 取用。
"""

from __future__ import annotations

import argparse
import os
import sys

from contestant.events import default_script
from contestant.llm_replay import recording_plan, save_cache
from contestant.stage2_sim import Stage2Map
from rmtc_client.json_extract import extract_first_json_object
from rmtc_client.llm_client import ClientConfig, LlmGatewayClient


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/events_cache.json")
    parser.add_argument("--url", default=os.environ.get("RMTC_AI_URL", ""))
    parser.add_argument("--token", default=os.environ.get("RMTC_TEAM_TOKEN", ""))
    parser.add_argument("--model", default=os.environ.get("RMTC_MODEL", ""))
    parser.add_argument("--seat", default="A")
    args = parser.parse_args(argv)

    if not args.url or not args.token:
        print("缺少 RMTC_AI_URL / RMTC_TEAM_TOKEN，无法录制")
        return 2

    game_map = Stage2Map.open_field(24, 24, seed=0)
    script = default_script(game_map)

    client = LlmGatewayClient(ClientConfig(url=args.url, token=args.token, model=args.model,
                                           timeout_s=30.0, max_calls=0, min_interval_s=0.0,
                                           json_mode=True, max_tokens=400),
                              logger=lambda level, text: print("[{0}] {1}".format(level, text)))
    entries = {}
    for key, prompt, announcement in recording_plan(game_map, script, seat=args.seat):
        result = client.ask(prompt)
        if result.ok:
            entries[key] = result.text
            parsed = extract_first_json_object(result.text) or {}
            print("OK  {0}  target_kind={1} conf={2}  公告={3}".format(
                key, parsed.get("target_kind"), parsed.get("target_confidence"),
                announcement[:36]))
        else:
            print("MISS {0}  code={1}  {2}".format(key, result.code, announcement[:36]))
        sys.stdout.flush()

    if not entries:
        print("一条都没录到，检查网关地址/令牌/模型名")
        return 1
    path = save_cache(args.out, entries, meta={
        "url": args.url, "model": args.model or "(default)",
        "count": len(entries), "source": "scripts.record_llm_hints",
    })
    print("已写入 {0}（{1} 条），客户端统计 {2}".format(path, len(entries), client.stats()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
