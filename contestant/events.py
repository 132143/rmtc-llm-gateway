"""随机事件注入：让离线仿真也能跑"公告 → LLM 理解 → 本地执行"这条链。

为什么需要它：第一轮评测台完全没有事件，于是"躲火线/幻术反向/洁身自好反伤"这些
只有真机才验证过；一旦场上出现事件，我们没有任何离线数据说明反应是对的。

设计上有一条红线：**评测台不能把结构化事件参数交给被测程序**（规则 §8.1/§11 要求
事件语义必须由大模型从自然语言里读出）。所以这里分成两半：
  · 裁判侧（本文件）：知道事件的真实效果，负责公告文本 + 结算；
  · 选手侧（contestant/llm_replay.py）：只从公告文本出发，用**录好的真实模型输出**
    走完 EventUnderstanding 的校验与消费路径，绝不读本文件的结构化参数。
"""

from __future__ import annotations

import hashlib

# 事件类型的标准文本模板（自然语言，不含任何结构化参数）。
TEXT = {
    "explosion": "第 {ann} Tick：随机事件预告：{start} Tick 时矩形 [{x1},{y1}] 至 [{x2},{y2}] "
                 "将发生爆炸，范围内所有存活战车扣除 80 HP；出生保护免疫。",
    "illusion": "第 {ann} Tick：随机事件预告：{start} Tick 起矩形 [{x1},{y1}] 至 [{x2},{y2}] 内的战车"
                "中幻术，接下来 50 Tick 移动的上下、左右会被互换；攻击方向不受影响。",
    "world_split": "第 {ann} Tick：随机事件预告：{start} Tick 时以 [{x},{y}] 为中心生成贯穿整行整列的"
                   "十字障碍，存在 30 Tick；激活瞬间在该十字线上的存活战车立即阵亡并满血复活，"
                   "出生保护无效，本 Tick 只能等待。",
    "pepper": "第 {ann} Tick：随机事件预告：{start} Tick 起 [{x},{y}] 出现火爆辣椒点位，存在 30 Tick。"
              "每次成功移动进入点位会在本 Tick 移动后触发整行整列十字火线，线上其他战车受到 50 HP "
              "环境伤害；交点上的战车不受该条火线伤害。",
    "fort": "第 {ann} Tick：随机事件预告：{start} Tick 起地图中心生成 3×3 堡垒，存在 50 Tick。"
            "连续驻留 10 Tick 可获得增益：之后主动攻击会命中自身横纵各 2 格内全部敌人。",
    "vulnerability": "第 {ann} Tick：随机事件预告：{start} Tick 起 30 Tick 内，前 20 Tick 实际成功移动的"
                     "战车获得易伤，受到的普通攻击与无双伤害翻倍。",
    "heal": "第 {ann} Tick：随机事件预告：{start} Tick 起 [{x},{y}] 出现天使恩泽点位，存在 20 Tick；"
            "成功进入可获得 10 Tick、每 Tick 回复 10 HP 的效果。",
    "clean_hands": "第 {ann} Tick：随机事件预告：{start} Tick 起 20 Tick 内，攻击正在防御且无出生保护的"
                   "敌人时，该目标不受伤害，攻击者自身承受 20 HP 反伤。",
}


class Event:
    """裁判侧事件：既有公告文本，也有真实几何与数值。"""

    def __init__(self, kind, announce, start, end, cells=None, anchor=None, **extra):
        self.kind = kind
        self.announce = int(announce)
        self.start = int(start)
        self.end = int(end)                     # 到期 Tick（不含）
        self.cells = frozenset(tuple(c) for c in (cells or ()))
        self.anchor = tuple(anchor) if anchor else None
        self.extra = extra
        self.text = ""

    def render(self, width, height):
        span = {"ann": self.announce, "start": self.start, "end": self.end}
        if self.cells:
            xs = [c[0] for c in self.cells]
            ys = [c[1] for c in self.cells]
            span.update({"x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)})
        if self.anchor:
            span.update({"x": self.anchor[0], "y": self.anchor[1]})
        template = TEXT.get(self.kind, "{kind}")
        try:
            self.text = template.format(**span)
        except KeyError:
            self.text = "第 {0} Tick：随机事件预告（{1}）。".format(self.announce, self.kind)
        return self.text

    def key(self):
        """公告文本的稳定指纹：录制/回放 LLM 输出时用它当键。"""
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:16]


def rect_cells(x1, y1, x2, y2):
    return frozenset((x, y) for x in range(min(x1, x2), max(x1, x2) + 1)
                     for y in range(min(y1, y2), max(y1, y2) + 1))


def cross_cells(x, y, width, height):
    return frozenset([(xx, y) for xx in range(width)] + [(x, yy) for yy in range(height)])


def default_script(game_map, span=500):
    """一场标准排期：8 类事件各一次，提前 5 Tick 公告。

    数值按规则 §8 的标准配置，排期只是练习用（规则 §8.1 明确不得假设固定排期）。
    """
    w, h = game_map.width, game_map.height
    cx, cy = w // 2, h // 2
    quarter = max(60, span // 5)
    events = [
        Event("explosion", 60, 65, 66, cells=rect_cells(2, 2, min(6, w - 2), min(6, h - 2))),
        Event("illusion", 120, 125, 175, cells=rect_cells(cx - 3, cy - 3, cx + 3, cy + 3)),
        Event("world_split", 190, 195, 225, anchor=(cx, cy - 4),
              cells=cross_cells(cx, cy - 4, w, h)),
        Event("pepper", 240, 245, 275, anchor=(max(1, cx - 4), max(1, cy - 4)),
              cells={(max(1, cx - 4), max(1, cy - 4))}),
        Event("fort", 270, 275, 325, anchor=(cx, cy),
              cells=frozenset((x, y) for x in range(cx - 1, cx + 2)
                              for y in range(cy - 1, cy + 2))),
        Event("vulnerability", 330, 335, 365),
        Event("heal", 380, 385, 405, anchor=(max(1, cx + 3), max(1, cy + 3)),
              cells={(max(1, cx + 3), max(1, cy + 3))}),
        Event("clean_hands", 430, 435, 455),
    ]
    for event in events:
        event.render(w, h)
    return EventScript(events, span=span)


class EventScript:
    """事件脚本：给程序看的是自然语言，给自己用的是结构化真值。"""

    def __init__(self, events, span=500):
        self.events = sorted(events, key=lambda e: e.announce)
        self.span = span

    # ---------- 选手侧：只有文本 ----------
    def announced(self, tick):
        """到 tick 为止已经公告的事件（文本），按时间升序。"""
        return [e for e in self.events if e.announce <= tick]

    def transcript(self, tick, limit=800):
        parts = [e.text for e in self.announced(tick)]
        text = "\n".join(parts)
        return text[-limit:] if len(text) > limit else text

    def announcement_keys(self):
        return [e.key() for e in self.events]

    # ---------- 裁判侧：真值 ----------
    def active(self, tick):
        return [e for e in self.events if e.start <= tick < e.end]

    def by_kind(self, kind, tick):
        for event in self.active(tick):
            if event.kind == kind:
                return event
        return None


# ---------------------------------------------------------------- 结算
class EventEngine:
    """按规则 §5.2 的十步顺序，把事件效果注入到仿真里。

    顺序：① 移动前（爆炸/世界分裂/幻术/HUD）② 移动后（辣椒火线/堡垒驻留/治疗/易伤计龄）。
    """

    def __init__(self, script):
        self.script = script
        self.vulnerable_until = {}      # seat -> tick（潘潘的神）
        self.reversed_until = {}        # seat -> tick（幻术）
        self.heal_until = {}            # seat -> tick（天使恩泽）
        self.fort_buff_until = {}       # seat -> tick（无双增益）
        self.fort_dwell = {}            # seat -> 连续驻留计数
        self.clean_hands_until = 0
        self.temporary_walls = set()
        self.wall_until = 0
        self.pepper_armed = set()       # 本轮已触发火线的点位
        self.log = []                   # [(tick, kind, 事实)] 供复盘

    # -- 移动前 --
    def pre_move(self, sim, tick):
        for event in self.script.active(tick):
            if event.kind == "explosion" and tick == event.start:
                self._explosion(sim, tick, event)
            elif event.kind == "world_split" and tick == event.start:
                self._world_split(sim, tick, event)
            elif event.kind == "illusion" and tick == event.start:
                for robot in sim.robots.values():
                    if robot.pos in event.cells:
                        self.reversed_until[robot.seat] = tick + 50
                sim.map.obstacles |= set()      # 幻术不改地形
            elif event.kind == "clean_hands" and tick == event.start:
                self.clean_hands_until = tick + 20
        if tick >= self.wall_until:
            self.temporary_walls = set()
            sim.map.obstacles -= getattr(self, "_walls_applied", set())
            self._walls_applied = set()

    def _explosion(self, sim, tick, event):
        dead = []
        for robot in sim.robots.values():
            if not robot.alive or robot.pos not in event.cells:
                continue
            if sim._is_protected(robot, tick):
                continue
            robot.hp = max(0.0, robot.hp - 80)
            if robot.hp <= 0:
                dead.append(robot)
        for robot in dead:
            robot.alive = False
            robot.damage = max(0.0, robot.damage - 60)   # §8.2：只扣死亡惩罚，不给击杀奖励
            robot.deaths += 1
            robot.dead_until = tick + 3
            self.log.append((tick, "explosion", "seat {0} 被炸毁".format(robot.seat)))

    def _world_split(self, sim, tick, event):
        walls = set(event.cells)
        sim.map.obstacles |= walls
        self._walls_applied = walls
        self.temporary_walls = walls
        self.wall_until = tick + 30
        for robot in sim.robots.values():
            if not robot.alive or robot.pos not in event.cells:
                continue
            # 立即满血复活 + 本 Tick 强制 WAIT + 扣 60
            robot.damage = max(0.0, robot.damage - 60)
            robot.deaths += 1
            robot.hp = 100.0
            robot.pos = sim._respawn_cell(robot)
            robot.protected_until = tick + 2
            robot.forced_wait = True
            self.log.append((tick, "world_split", "seat {0} 被十字线处死并立即复活".format(robot.seat)))

    # -- 移动后 --
    def post_move(self, sim, tick, actions):
        self._pepper(sim, tick, actions)
        self._fort(sim, tick)
        self._heal(sim, tick)
        if tick >= self.clean_hands_until:
            self.clean_hands_until = 0

    def _pepper(self, sim, tick, actions):
        event = self.script.by_kind("pepper", tick)
        if event is None:
            return
        triggers = []
        for seat, action in actions.items():
            robot = sim.robots.get(seat)
            if robot is None or not robot.alive or action["action"] != "move":
                continue
            if robot.pos == event.anchor:      # 本 Tick 成功移动进入点位
                triggers.append(robot)
        if not triggers:
            return
        victims = {}
        for robot in triggers:
            line = set(cross_cells(robot.pos[0], robot.pos[1], sim.map.width, sim.map.height))
            for other in sim.robots.values():
                if not other.alive or other.pos not in line or other.pos == robot.pos:
                    continue
                if sim._is_protected(other, tick):
                    continue
                victims.setdefault(other.seat, []).append(robot)
        for seat, hitter_list in victims.items():
            target = sim.robots[seat]
            target.hp = max(0.0, target.hp - 50)
            if target.hp <= 0:
                target.alive = False
                target.damage = max(0.0, target.damage - 60)
                target.deaths += 1
                target.dead_until = tick + 3
                share = 60.0 / len(hitter_list)
                for hitter in hitter_list:
                    hitter.damage += share               # §8.7 击杀奖励归触发者平分
                self.log.append((tick, "pepper", "seat {0} 被火线击毁".format(seat)))

    def _fort(self, sim, tick):
        event = self.script.by_kind("fort", tick)
        if event is None:
            return
        for robot in sim.robots.values():
            if not robot.alive or robot.pos not in event.cells:
                self.fort_dwell[robot.seat] = 0
                continue
            self.fort_dwell[robot.seat] = self.fort_dwell.get(robot.seat, 0) + 1
            if self.fort_dwell[robot.seat] >= 10 and robot.seat not in self.fort_buff_until:
                self.fort_buff_until[robot.seat] = event.end
                sim.fort_buff[robot.seat] = event.end
                self.log.append((tick, "fort", "seat {0} 获得无双增益".format(robot.seat)))
        for seat, until in list(self.fort_buff_until.items()):
            if tick >= until:
                sim.fort_buff[seat] = -1
                self.fort_buff_until.pop(seat, None)

    def _heal(self, sim, tick):
        event = self.script.by_kind("heal", tick)
        if event is not None:
            for robot in sim.robots.values():
                if robot.alive and robot.pos == event.anchor:
                    self.heal_until[robot.seat] = tick + 10
        for seat, until in list(self.heal_until.items()):
            robot = sim.robots.get(seat)
            if robot is None:
                self.heal_until.pop(seat, None)
                continue
            if tick >= until:
                self.heal_until.pop(seat, None)
                continue
            if robot.alive:
                robot.hp = min(100.0, robot.hp + 10)

    # -- 查询（供结算时用）--
    def reversed(self, seat, tick):
        return self.reversed_until.get(seat, -1) > tick

    def vulnerable(self, seat, tick):
        return self.vulnerable_until.get(seat, -1) > tick

    def mark_vulnerable_if_moved(self, sim, tick, moved_seats):
        event = self.script.by_kind("vulnerability", tick)
        if event is None:
            return
        if not (event.start <= tick < event.start + 20):
            return
        for seat in moved_seats:
            self.vulnerable_until[seat] = event.start + 30

    def clean_hands_active(self, tick):
        return tick < self.clean_hands_until
