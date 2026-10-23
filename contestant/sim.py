"""第一阶段离线模拟器（按官方练习包参数）。

来源：练习包 config/stage1.json —— 18×18、6 个障碍、16 个资源（12×10 + 4×20）、
出生点 (1,1)、100 Tick、视野半径 4、复活等 3 Tick、出生保护 2 Tick。

它只模拟第一阶段"捡资源"，用来公平比较策略；第二阶段对抗需要官方裁判，
本文件不假装能替代它（见 README 的说明）。
"""

from __future__ import annotations

import random

DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


class Stage1Map:
    def __init__(self, width, height, obstacles, resources, spawn=(1, 1)):
        self.width = width
        self.height = height
        self.obstacles = {tuple(p) for p in obstacles}
        self.resources = {tuple(item["position"]): int(item["value"]) for item in resources}
        self.spawn = tuple(spawn)

    @classmethod
    def from_config(cls, cfg):
        robot = cfg["robots"][0]
        return cls(cfg["map"]["width"], cfg["map"]["height"], cfg["map"]["obstacles"],
                   cfg["map"]["resources"], tuple(robot["spawn"]))

    @classmethod
    def random(cls, seed=None, width=18, height=18, normal=12, high=4, obstacles=6):
        """随机地图：资源/障碍密度和官方 stage1 一致，避免只在一个地图上调参。"""
        rng = random.Random(seed)
        cells = [(x, y) for x in range(width) for y in range(height)]
        cells.remove((1, 1))
        rng.shuffle(cells)
        obstacle_cells = cells[:obstacles]
        free = cells[obstacles:]
        resource_cells = free[:normal + high]
        resources = [{"position": list(pos), "value": 10} for pos in resource_cells[:normal]]
        resources += [{"position": list(pos), "value": 20} for pos in resource_cells[normal:normal + high]]
        return cls(width, height, obstacle_cells, resources)


class View:
    """一帧感知里策略能看到的全部信息（严格对应协议字段）。"""

    __slots__ = ("pos", "visible_resources", "obstacles", "explored", "collected",
                 "blocked_now", "tick", "vision_radius")

    def __init__(self, pos, visible_resources, obstacles, explored, collected,
                 blocked_now, tick, vision_radius):
        self.pos = pos
        self.visible_resources = visible_resources
        self.obstacles = obstacles
        self.explored = explored
        self.collected = collected
        self.blocked_now = blocked_now
        self.tick = tick
        self.vision_radius = vision_radius


def run_stage1(game_map, strategy, ticks=100, vision_radius=4):
    """跑一局，返回 {value, score, collected, blocked_moves, ticks}。"""
    pos = game_map.spawn
    obstacles = set(game_map.obstacles)
    remaining = dict(game_map.resources)
    explored = {pos}
    value = 0
    collected = 0
    blocked_moves = 0
    last_blocked = set()

    for tick in range(ticks):
        collected_now = set()
        visible = []
        for res_pos, res_value in remaining.items():
            if abs(res_pos[0] - pos[0]) <= vision_radius and abs(res_pos[1] - pos[1]) <= vision_radius:
                visible.append((res_pos, res_value))
        local_obstacles = {p for p in obstacles
                           if abs(p[0] - pos[0]) <= vision_radius and abs(p[1] - pos[1]) <= vision_radius}
        view = View(pos, visible, local_obstacles, explored, collected_now,
                    last_blocked, tick, vision_radius)
        strategy.observe(view)
        action = strategy.decide(view)
        last_blocked = set()

        if action.get("action") == "move":
            dx, dy = DIRECTIONS.get(action.get("direction"), (0, 0))
            nxt = (pos[0] + dx, pos[1] + dy)
            if 0 <= nxt[0] < game_map.width and 0 <= nxt[1] < game_map.height \
                    and nxt not in obstacles:
                pos = nxt
                explored.add(pos)
            else:
                blocked_moves += 1
                last_blocked.add(nxt)
        if pos in remaining:
            gained = remaining.pop(pos)
            value += gained
            collected += 1
            collected_now.add(pos)
            explored.add(pos)
        if not remaining:
            break

    total = sum(game_map.resources.values()) or 1
    score = 40.0 * min(1.0, value / total)
    return {"value": value, "score": round(score, 2), "collected": collected,
            "blocked_moves": blocked_moves, "total": total}
