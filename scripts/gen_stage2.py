"""生成第二阶段测验配置（4 车）+ 一个加速的场次参数。

第二阶段官方模板是 500 Tick × 500 ms（4 分 10 秒），做 A/B 太贵。
裁判 API 接受 tick_ms / max_ticks / bots / config，所以压到 200 Tick × 100 ms（20 秒）；
规则不变，只是节奏变快（练习端允许，正式赛以公布为准）。

用法：python3 scripts/gen_stage2.py
"""

from __future__ import annotations

import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "docs", "sweep_stage2")
BASE = os.path.join(ROOT, "practicekit", "stage2.json")

BASE_OFFICIAL = os.path.join(ROOT, "docs", "practice_stage1.json")


def load_stage2_base():
    """官方 stage2 模板（从练习包里取；仓库里也放一份 docs/practice_stage2.json）。"""
    for path in (os.path.join(ROOT, "docs", "practice_stage2.json"), BASE):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
    raise SystemExit("找不到 stage2 模板：需要 docs/practice_stage2.json")


def variant(base, name, width, height, obstacles, resources):
    config = json.loads(json.dumps(base))
    config["map"] = {"width": width, "height": height,
                     "obstacles": [list(p) for p in obstacles],
                     "resources": [{"id": f"{name}_{i}", "position": list(p), "value": v}
                                   for i, (v, p) in enumerate(resources)]}
    config["robots"] = [{"team": "A", "spawn": [1, 1]}, {"team": "B", "spawn": [width - 2, 1]},
                        {"team": "C", "spawn": [1, height - 2]},
                        {"team": "D", "spawn": [width - 2, height - 2]}]
    config["missions"] = [{"id": "collect_two_normal", "type": "normal_resources", "target": 2},
                          {"id": "collect_high_value", "type": "high_resources", "target": 1},
                          {"id": "reach_target", "type": "reach",
                           "position": [width // 2, height // 2]}]
    return config


def free_cells(width, height, obstacles, spawns):
    blocked = {tuple(p) for p in obstacles} | {tuple(p) for p in spawns}
    return [(x, y) for x in range(width) for y in range(height) if (x, y) not in blocked]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    base = load_stage2_base()
    spawns18 = [[1, 1], [16, 1], [1, 16], [16, 16]]

    configs = {"official": json.loads(json.dumps(base))}
    configs["official"]["robots"] = [{"team": t, "spawn": s}
                                     for t, s in zip("ABCD", spawns18)]

    # 空旷图：资源集中在中心，考察"抢资源 vs 打人"的取舍
    center = [(9, 8), (8, 9), (9, 9), (10, 9), (9, 10), (8, 8), (10, 8), (8, 10),
              (10, 10), (9, 7), (9, 11), (7, 9)]
    high = [(6, 6), (12, 6), (6, 12), (12, 12)]
    configs["open"] = variant(base, "open", 18, 18, [],
                              [(10, p) for p in center] + [(20, p) for p in high])

    # 24×24：路远，资源分散
    rng = random.Random(41)
    wide = free_cells(24, 24, [], [[1, 1], [22, 1], [1, 22], [22, 22]])
    rng.shuffle(wide)
    obstacles = wide[:16]
    leftovers = wide[16:]
    configs["big24"] = variant(base, "big24", 24, 24, obstacles,
                               [(10, p) for p in leftovers[:12]] + [(20, p) for p in leftovers[12:16]])

    for name, config in configs.items():
        path = os.path.join(OUT_DIR, name + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
        print(f"{name:9s} {config['map']['width']}x{config['map']['height']} "
              f"障碍 {len(config['map']['obstacles'])} 资源 {len(config['map']['resources'])} "
              f"-> {path}")


if __name__ == "__main__":
    main()
