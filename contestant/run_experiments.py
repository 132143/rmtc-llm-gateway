"""策略对比实验：同一张官方练习地图 + 200 张等密度随机地图。

用法：
    python3 -m contestant.run_experiments                # 默认 200 张随机图
    python3 -m contestant.run_experiments --maps 500 --ticks 100

输出的是第一阶段模拟得分（满分 40）。数字来自本仓库的模拟器，不是官方裁判。
"""

from __future__ import annotations

import argparse
import json
import os

from contestant.sim import Stage1Map, run_stage1
from contestant.strategy import GreedyBfsStrategy, SnakeStrategy


def load_practice_map():
    """官方练习包 stage1.json（从练习包 tar.gz 里取出来的那份）。"""
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "docs", "practice_stage1.json"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "practice_stage1.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return Stage1Map.from_config(json.load(handle))
    return None


VARIANTS = {
    "snake（原骨架）": lambda w, h: SnakeStrategy(w, h),
    "bfs detour=0（抢最近）": lambda w, h: GreedyBfsStrategy(w, h, detour=0.0),
    "bfs detour=2": lambda w, h: GreedyBfsStrategy(w, h, detour=2.0),
    "bfs detour=6": lambda w, h: GreedyBfsStrategy(w, h, detour=6.0),
    "bfs detour=20（抢高价值）": lambda w, h: GreedyBfsStrategy(w, h, detour=20.0),
}


def evaluate(game_map, factory, ticks, vision_radius):
    strategy = factory(game_map.width, game_map.height)
    return run_stage1(game_map, strategy, ticks=ticks, vision_radius=vision_radius)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--maps", type=int, default=200)
    parser.add_argument("--ticks", type=int, default=100)
    parser.add_argument("--vision", type=int, default=4)
    args = parser.parse_args(argv)

    practice = load_practice_map()
    print("== 官方练习地图（stage1.json）==")
    if practice is None:
        print("（没找到 practice_stage1.json，跳过这一节）")
    else:
        for name, factory in VARIANTS.items():
            result = evaluate(practice, factory, args.ticks, args.vision)
            print(f"{name:22s} 得分 {result['score']:5.1f}/40  资源 {result['value']:3d}"
                  f"/{result['total']}  撞墙 {result['blocked_moves']:3d}")

    print(f"\n== {args.maps} 张等密度随机地图（均值 / 最低）==")
    rows = {name: [] for name in VARIANTS}
    for seed in range(args.maps):
        game_map = Stage1Map.random(seed=seed)
        for name, factory in VARIANTS.items():
            rows[name].append(evaluate(game_map, factory, args.ticks, args.vision)["score"])
    for name, scores in rows.items():
        mean = sum(scores) / len(scores)
        print(f"{name:22s} 平均 {mean:5.2f}/40   最低 {min(scores):5.1f}   满分场次 "
              f"{sum(1 for s in scores if s >= 40.0):3d}/{len(scores)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
