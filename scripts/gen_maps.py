"""生成第一阶段的测验地图（喂给官方练习裁判 --config）。

两组：
  docs/sweep_maps/       7 张常规图：16 资源、总价值 200，与官方 stage1 同配方，只是布局不同
  docs/sweep_maps_hard/  2 张硬图：大到跑不完 / 障碍密到难绕，用来看策略的天花板

用法：python3 scripts/gen_maps.py
"""

from __future__ import annotations

import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BASE_CONFIG = os.path.join(ROOT, "docs", "practice_stage1.json")
NORMAL_DIR = os.path.join(ROOT, "docs", "sweep_maps")
HARD_DIR = os.path.join(ROOT, "docs", "sweep_maps_hard")


def load_base():
    with open(BASE_CONFIG, encoding="utf-8") as handle:
        return json.load(handle)


def make_config(base, name, obstacles, resources, width=18, height=18):
    config = json.loads(json.dumps(base))
    config["map"] = {"width": width, "height": height,
                     "obstacles": [list(p) for p in obstacles],
                     "resources": [{"id": f"{name}_{i}", "position": list(p), "value": v}
                                   for i, (v, p) in enumerate(resources)]}
    config["robots"] = [{"team": "A", "spawn": [1, 1]}]
    return config


def free_grid(width=18, height=18, blocked=()):
    blocked = {tuple(p) for p in blocked} | {(1, 1)}
    return [(x, y) for x in range(width) for y in range(height) if (x, y) not in blocked]


def random_layout(seed, obstacle_count, width=18, height=18):
    rng = random.Random(seed)
    cells = free_grid(width, height)
    rng.shuffle(cells)
    obstacles = cells[:obstacle_count]
    leftovers = cells[obstacle_count:]
    return obstacles, [(10, p) for p in leftovers[:12]] + [(20, p) for p in leftovers[12:16]]


def corners_layout():
    spots = [(2, 2), (3, 2), (2, 3), (15, 2), (14, 2), (15, 3),
             (2, 15), (3, 14), (2, 14), (15, 15), (14, 15), (15, 14)]
    return [], [(10, p) for p in spots] + [(20, p) for p in [(5, 5), (12, 5), (5, 12), (12, 12)]]


def maze_layout():
    obstacles = [(6, y) for y in range(1, 14)] + [(11, y) for y in range(4, 17)]
    free = free_grid(blocked=obstacles)
    normal = free[:: max(1, len(free) // 14)][:12]
    return obstacles, [(10, p) for p in normal] + [(20, p) for p in [(5, 1), (12, 16), (3, 9), (16, 9)]]


def far_half_layout():
    free = [(x, y) for x in range(10, 18) for y in range(1, 17)]
    rng = random.Random(11)
    rng.shuffle(free)
    return [], [(10, p) for p in free[:12]] + [(20, p) for p in free[12:16]]


def big_layout():
    """24×24：同样是 200 价值，但路远得多，100 Tick 跑不完全场。"""
    rng = random.Random(21)
    cells = free_grid(24, 24)
    rng.shuffle(cells)
    obstacles = cells[:16]
    leftovers = cells[16:]
    return obstacles, [(10, p) for p in leftovers[:12]] + [(20, p) for p in leftovers[12:16]]


def dense_layout():
    """18×18 + 40 个障碍：绕障压力测试。"""
    rng = random.Random(31)
    cells = free_grid(18, 18)
    rng.shuffle(cells)
    obstacles = cells[:40]
    leftovers = cells[40:]
    return obstacles, [(10, p) for p in leftovers[:12]] + [(20, p) for p in leftovers[12:16]]


def write_group(base, out_dir, configs):
    os.makedirs(out_dir, exist_ok=True)
    for name, (obstacles, resources, size) in configs.items():
        config = make_config(base, name, obstacles, resources, size[0], size[1])
        path = os.path.join(out_dir, name + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
        total = sum(item["value"] for item in config["map"]["resources"])
        print(f"{os.path.basename(out_dir)}/{name:10s} 障碍 {len(obstacles):2d} 资源 "
              f"{len(resources):2d} 总价值 {total} 地图 {size[0]}x{size[1]}")


def main():
    base = load_base()
    normal = {
        "official": ([list(p) for p in base["map"]["obstacles"]],
                     [(item["value"], tuple(item["position"])) for item in base["map"]["resources"]],
                     (18, 18)),
        "corners": (*corners_layout(), (18, 18)),
        "maze": (*maze_layout(), (18, 18)),
        "far_half": (*far_half_layout(), (18, 18)),
    }
    for seed, name in ((1, "random1"), (2, "random2"), (3, "random3")):
        normal[name] = (*random_layout(seed, 12), (18, 18))
    write_group(base, NORMAL_DIR, normal)

    hard = {
        "big24": (*big_layout(), (24, 24)),
        "dense40": (*dense_layout(), (18, 18)),
    }
    write_group(base, HARD_DIR, hard)


if __name__ == "__main__":
    main()
