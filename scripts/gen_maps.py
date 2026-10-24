"""生成第一阶段的测验地图（喂给官方练习裁判 --config）。

全部保持"16 个资源、总价值 200"（与官方 stage1 一致），只是布局不同，
这样得分可以直接横向比较。用法：
    python3 scripts/gen_maps.py            # 写到 docs/sweep_maps/
"""

from __future__ import annotations

import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "docs", "sweep_maps")
BASE_CONFIG = os.path.join(ROOT, "docs", "practice_stage1.json")


def load_base():
    with open(BASE_CONFIG, encoding="utf-8") as handle:
        return json.load(handle)


def with_map(base, name, obstacles, resources):
    config = json.loads(json.dumps(base))
    config["map"] = {"width": 18, "height": 18, "obstacles": [list(p) for p in obstacles],
                     "resources": [{"id": f"{name}_{i}", "position": list(p), "value": v}
                                   for i, (v, p) in enumerate(resources)]}
    config["robots"] = [{"team": "A", "spawn": [1, 1]}]
    return config


def free_grid(width=18, height=18, blocked=()):
    blocked = {tuple(p) for p in blocked} | {(1, 1)}
    return [(x, y) for x in range(width) for y in range(height) if (x, y) not in blocked]


def spread_random(seed, obstacle_count, width=18, height=18):
    """等密度随机：先放障碍，再在剩余格子里放 12 个普通 + 4 个高价值资源。"""
    rng = random.Random(seed)
    cells = free_grid(width, height)
    rng.shuffle(cells)
    obstacles = cells[:obstacle_count]
    leftovers = cells[obstacle_count:]
    normal = leftovers[:12]
    high = leftovers[12:16]
    resources = [(10, p) for p in normal] + [(20, p) for p in high]
    return obstacles, resources


def corners_map():
    """无遮挡，资源贴着四角：考察"跑远近"的取舍。"""
    spots = [(2, 2), (3, 2), (2, 3), (15, 2), (14, 2), (15, 3),
             (2, 15), (3, 14), (2, 14), (15, 15), (14, 15), (15, 14)]
    high = [(5, 5), (12, 5), (5, 12), (12, 12)]
    resources = [(10, p) for p in spots] + [(20, p) for p in high]
    return [], resources


def wall_maze():
    """两条长墙 + 中间夹道：考察绕障。"""
    obstacles = [(6, y) for y in range(1, 14)] + [(11, y) for y in range(4, 17)]
    free = free_grid(blocked=obstacles)
    normal = free[::max(1, len(free) // 14)][:12]
    high = [(5, 1), (12, 16), (3, 9), (16, 9)]
    resources = [(10, p) for p in normal] + [(20, p) for p in high]
    return obstacles, resources


def far_half():
    """资源全在右半场：出生点离资源很远，考察"少走空步"的价值。"""
    free = [(x, y) for x in range(10, 18) for y in range(1, 17)]
    rng = random.Random(11)
    rng.shuffle(free)
    normal = free[:12]
    high = free[12:16]
    return [], [(10, p) for p in normal] + [(20, p) for p in high]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    base = load_base()
    maps = {"official": base["map"], }

    configs = {}
    # 官方地图原样
    official = json.loads(json.dumps(base))
    official["map"] = base["map"]
    official["robots"] = [{"team": "A", "spawn": [1, 1]}]
    configs["official"] = official

    obstacles, resources = corners_map()
    configs["corners"] = with_map(base, "corners", obstacles, resources)

    obstacles, resources = wall_maze()
    configs["maze"] = with_map(base, "maze", obstacles, resources)

    obstacles, resources = far_half()
    configs["far_half"] = with_map(base, "far_half", obstacles, resources)

    for seed, name in ((1, "random1"), (2, "random2"), (3, "random3")):
        obstacles, resources = spread_random(seed, 12)
        configs[name] = with_map(base, name, obstacles, resources)

    for name, config in configs.items():
        path = os.path.join(OUT_DIR, name + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
        total = sum(item["value"] for item in config["map"]["resources"])
        print(f"{name:10s} 障碍 {len(config['map']['obstacles']):2d}  资源 {len(config['map']['resources']):2d}"
              f"  总价值 {total}  -> {path}")


if __name__ == "__main__":
    main()
