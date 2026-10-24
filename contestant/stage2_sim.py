"""第二阶段离线仿真器（按赛事规则 v1.1 的结算顺序实现）。

**它不是裁判**，只做策略对照：同一张图、同一批对手、同一随机种子下，
比较"两套决策代码谁得分高、谁更稳"。用它回答的问题：
  - 这套策略在资源被抢光/被贴身压制时会不会崩；
  - 改动是抬均值还是抬下限；
  - 会不会出现非法动作、卡死、异常。

为什么用真实感知格式喂真实程序：被测对象是 contestant.agent.Agent 本体
（它只吃感知 dict、只吐动作 dict，不依赖 ROS），所以"仿真里跑通"与"上场跑通"
是同一套解析与状态机——这正是 A/B 要保真的那部分。

规则依据（全部来自《赛事规则 v1.1》）：
  §4.2 资源 12×10 + 4×20、收集即移除、本场不刷新；
  §5.2 结算顺序：事件 → 固化动作/防御 → 按移动前位置攻击 → 占位统一移动 → 收资源/任务 → 死亡扣分；
  §6.1 移动失败条件（越界/障碍/目标格被占/多车争格/不能跟进腾空格/不能互换）；
  §6.2 攻击只沿四轴 1–2 格、命中最近存活敌车、保护目标 0 伤害且挡后方；
  §6.3 防御把普通受击 20 → 10；
  §7 阵亡 3 Tick 复活 + 2 Tick 保护、死亡扣 60（最低 0）；
  §8.5 无双 5×5（本仿真支持增益区与驻留判定）；
  §10 计分：30×min(R2/120,1) + 15×min(D/500,1) + 5×M。

刻意没做的：随机事件的时间轴编排（§8 的公告是自然语言，只有 LLM 能理解，
仿真里没有 LLM，硬编码时间表既违规则也无法验证），所以默认 events=None。
"""

from __future__ import annotations

import random
import time

DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
DIR_LIST = ("up", "down", "left", "right")

HP_MAX = 100
ATTACK_DAMAGE = 20
DEFENDED_DAMAGE = 10
DEATH_PENALTY = 60
KILL_REWARD = 60
RESPAWN_WAIT = 3
RESPAWN_PROTECT = 2
VISION_RADIUS = 4
AOE_RADIUS = 2
AOE_DAMAGE = 50
AOE_DEFENDED = 10

MISSIONS = [
    {"id": "collect_two_normal", "type": "normal_resources", "target": 2,
     "text": "累计收集至少 2 个普通资源"},
    {"id": "collect_high_value", "type": "high_resources", "target": 1,
     "text": "累计收集至少 1 个高价值资源"},
    {"id": "reach_target", "type": "reach", "position": [12, 12],
     "text": "在比赛结束前至少一次到达 [12,12]"},
]


# ------------------------------------------------------------------ 地图
class Stage2Map:
    def __init__(self, width, height, obstacles, resources, spawns, reach=(12, 12)):
        self.width = int(width)
        self.height = int(height)
        self.obstacles = {tuple(p) for p in obstacles}
        self.resources = {tuple(item["position"]): int(item["value"]) for item in resources}
        self.spawns = {seat: tuple(pos) for seat, pos in spawns.items()}
        self.reach = tuple(reach)

    @classmethod
    def build(cls, width=24, height=24, obstacles=0, normal=12, high=4, seed=0,
              obstacle_mode="random"):
        rng = random.Random(seed)
        spawns = {"A": (1, 1), "B": (width - 2, 1), "C": (1, height - 2),
                  "D": (width - 2, height - 2)}
        reserved = {tuple(p) for p in spawns.values()}
        cells = [(x, y) for x in range(width) for y in range(height) if (x, y) not in reserved]
        rng.shuffle(cells)

        obstacle_cells = set()
        if obstacle_mode == "walls":
            mid_y = height // 3
            mid_y2 = (2 * height) // 3
            for x in range(2, width - 2):
                if x % 5 == 0:
                    continue
                obstacle_cells.add((x, mid_y))
                obstacle_cells.add((x, mid_y2))
            obstacle_cells = {c for c in obstacle_cells if c not in reserved}
        for cell in cells:
            if len(obstacle_cells) >= obstacles:
                break
            if cell in obstacle_cells:
                continue
            obstacle_cells.add(cell)

        free = [c for c in cells if c not in obstacle_cells]
        rng.shuffle(free)
        chosen = free[:normal + high]
        resources = [{"position": list(p), "value": 10} for p in chosen[:normal]]
        resources += [{"position": list(p), "value": 20} for p in chosen[normal:normal + high]]
        reach = (width // 2, height // 2)
        return cls(width, height, obstacle_cells, resources, spawns, reach=reach)

    @classmethod
    def open_field(cls, width=24, height=24, seed=0):
        return cls.build(width, height, obstacles=0, seed=seed)

    @classmethod
    def dense(cls, width=24, height=24, seed=0):
        return cls.build(width, height, obstacles=40, seed=seed)

    @classmethod
    def maze(cls, width=24, height=24, seed=0):
        return cls.build(width, height, obstacles=0, seed=seed, obstacle_mode="walls")


# ------------------------------------------------------------------ 机器人与状态
class RobotState:
    __slots__ = ("seat", "pos", "hp", "alive", "dead_until", "protected_until",
                 "defending", "last_action", "resource_value", "damage",
                 "normal_count", "high_count", "mission_ids", "deaths",
                 "invalid_actions", "blocked_moves", "attacks", "waits", "moves", "reached",
                 "pending_attack_direction")

    def __init__(self, seat, spawn):
        self.seat = seat
        self.pos = tuple(spawn)
        self.hp = HP_MAX
        self.alive = True
        self.dead_until = -1
        self.protected_until = -1
        self.defending = False
        self.last_action = None
        self.resource_value = 0
        self.damage = 0
        self.normal_count = 0
        self.high_count = 0
        self.mission_ids = []
        self.deaths = 0
        self.invalid_actions = 0
        self.blocked_moves = 0
        self.attacks = 0
        self.waits = 0
        self.moves = 0
        self.reached = False
        self.pending_attack_direction = None


def _relative_string(source, target, size=9):
    """按协议 §4.1 生成 9×9 相对位置字符串（1=自身第 5 行第 5 列，2=该实体）。"""
    rows = []
    for row in range(size):
        line = []
        for col in range(size):
            if row == 4 and col == 4:
                line.append("1")
            elif row == 4 + (target[1] - source[1]) and col == 4 + (target[0] - source[0]):
                line.append("2")
            else:
                line.append("0")
        rows.append("".join(line))
    return "\n".join(rows)


# ------------------------------------------------------------------ 仿真主体
class Stage2Simulation:
    def __init__(self, game_map, brains, ticks=500, vision_radius=VISION_RADIUS,
                 session_id="sim-match", stage=2, measure_time=True):
        """brains: {seat: 带 on_perception(dict) -> dict|None 的对象}"""
        self.map = game_map
        self.ticks = int(ticks)
        self.vision = int(vision_radius)
        self.session_id = session_id
        self.stage = int(stage)
        self.measure_time = measure_time
        self.robots = {seat: RobotState(seat, game_map.spawns[seat]) for seat in brains}
        self.brains = brains
        self.remaining = dict(game_map.resources)
        self.fort = None
        self.fort_buff = {seat: -1 for seat in brains}
        self.errors = []
        self.decision_ms = []

    # -- 感知构造 ----------------------------------------------------
    def _perception(self, seat, tick):
        robot = self.robots[seat]
        visible = []
        reach = self.vision
        for pos, value in self.remaining.items():
            if max(abs(pos[0] - robot.pos[0]), abs(pos[1] - robot.pos[1])) <= reach:
                visible.append({"type": "resource_high" if value >= 20 else "resource_normal",
                                "value": int(value),
                                "position": _relative_string(robot.pos, pos)})
        for cell in self.map.obstacles:
            if max(abs(cell[0] - robot.pos[0]), abs(cell[1] - robot.pos[1])) <= reach:
                visible.append({"type": "obstacle", "position": _relative_string(robot.pos, cell)})
        if robot.alive:
            for other_seat, other in self.robots.items():
                if other_seat == seat or not other.alive:
                    continue
                if max(abs(other.pos[0] - robot.pos[0]),
                       abs(other.pos[1] - robot.pos[1])) <= reach:
                    visible.append({"type": "robot", "hp": other.hp,
                                    "protected": self._is_protected(other, tick),
                                    "defending": bool(other.defending),
                                    "position": _relative_string(robot.pos, other.pos)})
        return {
            "tick": tick,
            "session_id": self.session_id,
            "vision_radius": self.vision,
            "map": {"width": self.map.width, "height": self.map.height},
            "self": {
                "position": [robot.pos[0], robot.pos[1]],
                "hp": robot.hp,
                "alive": robot.alive,
                "protected": self._is_protected(robot, tick),
                "defending": bool(robot.defending),
                "last_action": robot.last_action,
            },
            "visible_entities": visible,
            "score": {
                "resource_value": robot.resource_value,
                "effective_damage": robot.damage,
                "mission_completed": len(robot.mission_ids),
                "mission_completed_ids": list(robot.mission_ids),
            },
            "mission_text": "；".join(m["text"] for m in MISSIONS),
            "missions": MISSIONS,
            "event_text": "",
        }

    def _is_protected(self, robot, tick):
        return robot.alive and robot.protected_until >= 0 and tick <= robot.protected_until

    # -- 主循环 ------------------------------------------------------
    def run(self):
        for tick in range(self.ticks):
            actions = {}
            for seat, brain in self.brains.items():
                data = self._perception(seat, tick)
                started = time.perf_counter()
                try:
                    payload = brain.on_perception(data)
                except Exception as exc:                 # 任何异常都记录，但绝不中断仿真
                    self.errors.append({"seat": seat, "tick": tick, "error": repr(exc)})
                    payload = None
                if self.measure_time:
                    self.decision_ms.append((time.perf_counter() - started) * 1000.0)
                actions[seat] = self._validate(seat, tick, payload)
            self._settle(tick, actions)
        return self.results()

    def _validate(self, seat, tick, payload):
        """裁判侧动作校验（协议 §5.1 / §12.3）。返回规范化动作 dict。"""
        robot = self.robots[seat]
        action = {"action": "wait", "direction": None}
        if not robot.alive:
            if isinstance(payload, dict) and payload.get("action") in ("move", "attack", "defend"):
                robot.invalid_actions += 1
            return action
        if not isinstance(payload, dict):
            return action
        kind = payload.get("action")
        if kind not in ("move", "attack", "defend", "wait"):
            return action
        if payload.get("tick") != tick or payload.get("session_id") != self.session_id:
            robot.invalid_actions += 1
            return action
        if self.stage < 2 and kind in ("attack", "defend"):
            robot.invalid_actions += 1
            return action
        if kind in ("move", "attack"):
            direction = payload.get("direction")
            if direction not in DIRECTIONS:
                robot.invalid_actions += 1
                return action
            return {"action": kind, "direction": direction}
        if kind == "defend":
            if self._is_protected(robot, tick):
                robot.invalid_actions += 1             # §6.3：保护期 DEFEND 无收益
                return {"action": "wait", "direction": None}
            return {"action": "defend", "direction": None}
        return action

    def _settle(self, tick, actions):
        robots = self.robots
        alive_now = [r for r in robots.values() if r.alive]

        for seat, action in actions.items():
            robot = robots[seat]
            robot.defending = False
            if not robot.alive:
                continue
            kind = action["action"]
            robot.pending_attack_direction = action["direction"] if kind == "attack" else None
            if kind == "defend" and not self._is_protected(robot, tick):
                robot.defending = True
            elif kind == "wait":
                robot.waits += 1

        nominal = {r.seat: {} for r in alive_now}
        pre_pos = {r.seat: r.pos for r in alive_now}
        for seat, action in actions.items():
            robot = robots[seat]
            if not robot.alive or action["action"] != "attack":
                continue
            if self._is_protected(robot, tick):
                robot.invalid_actions += 1
                continue
            robot.attacks += 1
            for target_seat, nominal_damage in self._attack_targets(robot, pre_pos, tick):
                nominal[seat][target_seat] = nominal[seat].get(target_seat, 0.0) + nominal_damage

        per_attacker_actual = {r.seat: 0.0 for r in alive_now}
        kill_rewards = {r.seat: 0.0 for r in alive_now}
        deaths = []
        grouped = {}
        for attacker, targets in nominal.items():
            for target_seat, dmg in targets.items():
                grouped.setdefault(target_seat, {})[attacker] = (
                    grouped.setdefault(target_seat, {}).get(attacker, 0.0) + dmg)
        for target_seat, incoming in grouped.items():
            target = robots[target_seat]
            if not target.alive:
                continue
            total = sum(incoming.values())
            if total <= 0:
                continue
            remaining = max(0.0, target.hp)
            applied = min(total, remaining)
            contributors = {seat: dmg for seat, dmg in incoming.items() if dmg > 0}
            share_sum = sum(contributors.values()) or 1.0
            for seat, dmg in contributors.items():
                per_attacker_actual[seat] += applied * (dmg / share_sum)
            target.hp = max(0.0, target.hp - total)
            if target.hp <= 0:
                target.alive = False
                deaths.append(target_seat)
                for seat in contributors:
                    kill_rewards[seat] += KILL_REWARD / max(1, len(contributors))

        for seat in robots:
            robot = robots[seat]
            robot.damage += per_attacker_actual.get(seat, 0.0) + kill_rewards.get(seat, 0.0)

        for seat in deaths:
            robot = robots[seat]
            robot.damage = max(0.0, robot.damage - DEATH_PENALTY)
            robot.hp = 0
            robot.alive = False
            robot.deaths += 1
            robot.dead_until = tick + RESPAWN_WAIT
            robot.defending = False
            robot.last_action = {"tick": tick, "action": "wait", "moved": False,
                                 "error": "E_DEAD"}

        movers = {}
        occupants = {r.pos for r in robots.values() if r.alive}
        for seat, action in actions.items():
            robot = robots[seat]
            if not robot.alive or action["action"] != "move":
                continue
            dx, dy = DIRECTIONS[action["direction"]]
            target = (robot.pos[0] + dx, robot.pos[1] + dy)
            if not (0 <= target[0] < self.map.width and 0 <= target[1] < self.map.height):
                robot.blocked_moves += 1
                robot.last_action = {"tick": tick, "action": "move", "moved": False,
                                     "error": "E_OOB",
                                     "effective_direction": action["direction"]}
                continue
            if target in self.map.obstacles or target in occupants:
                robot.blocked_moves += 1
                robot.last_action = {"tick": tick, "action": "move", "moved": False,
                                     "error": "E_BLOCKED",
                                     "effective_direction": action["direction"]}
                continue
            movers[seat] = (target, action["direction"])

        claims = {}
        for seat, (target, _direction) in movers.items():
            claims.setdefault(target, []).append(seat)
        contested = {target for target, seats in claims.items() if len(seats) > 1}
        for seat in list(movers):
            if movers[seat][0] in contested:
                robot = robots[seat]
                robot.blocked_moves += 1
                robot.last_action = {"tick": tick, "action": "move", "moved": False,
                                     "error": "E_BLOCKED",
                                     "effective_direction": movers[seat][1]}
                movers.pop(seat, None)

        for seat, (target, direction) in movers.items():
            robot = robots[seat]
            robot.pos = target
            robot.moves += 1
            robot.last_action = {"tick": tick, "action": "move", "moved": True,
                                 "effective_direction": direction}
            if self._is_protected(robot, tick) and target != self.map.spawns[seat]:
                robot.protected_until = -1               # §7 成功离开出生点即取消保护

        for seat, action in actions.items():
            robot = robots[seat]
            if robot.last_action is not None and robot.last_action.get("tick") == tick:
                continue
            robot.last_action = {"tick": tick, "action": action["action"], "moved": False}

        for robot in robots.values():
            if not robot.alive:
                continue
            value = self.remaining.pop(robot.pos, None)
            if value is not None:
                robot.resource_value += value
                if value >= 20:
                    robot.high_count += 1
                else:
                    robot.normal_count += 1
            if robot.pos == self.map.reach:
                robot.reached = True
            self._check_missions(robot)

        for robot in robots.values():
            if robot.alive:
                continue
            if tick >= robot.dead_until:
                robot.alive = True
                robot.hp = HP_MAX
                robot.pos = self._respawn_cell(robot)
                robot.protected_until = tick + RESPAWN_PROTECT
                robot.defending = False
                robot.last_action = {"tick": tick, "action": "wait", "moved": False,
                                     "error": None}

    def _respawn_cell(self, robot):
        """Q&A 第 1 问：以出生点为基准的曼哈顿距离，同距离按"上→右→下→左"搜索；
        合法格 = 地图内、无静态障碍、无其他存活车辆占用；资源格可以复活。"""
        origin = self.map.spawns[robot.seat]
        occupied = {r.pos for r in self.robots.values() if r.alive and r is not robot}
        if origin not in self.map.obstacles and origin not in occupied:
            return origin
        candidates = []
        for y in range(self.map.height):
            for x in range(self.map.width):
                cell = (x, y)
                if cell in self.map.obstacles or cell in occupied:
                    continue
                candidates.append((abs(x - origin[0]) + abs(y - origin[1]), y, -x, cell))
        candidates.sort()
        return candidates[0][3] if candidates else origin

    def _attack_targets(self, robot, pre_pos, tick):
        """§6.2：沿指定方向直线 1–2 格内最近的存活敌车；保护目标挡后方且自身 0 伤害。"""
        direction = self._attack_direction(robot)
        dx, dy = DIRECTIONS[direction]
        near = None
        for step in (1, 2):
            cell = (robot.pos[0] + dx * step, robot.pos[1] + dy * step)
            for seat, pos in pre_pos.items():
                if seat == robot.seat or pos != cell:
                    continue
                if not self.robots[seat].alive:
                    continue
                near = (seat, self.robots[seat])
                break
            if near is not None:
                break
        if near is None:
            return []
        aoe = self.fort_buff.get(robot.seat, -1) > tick
        if aoe:
            targets = []
            for ox in range(-AOE_RADIUS, AOE_RADIUS + 1):
                for oy in range(-AOE_RADIUS, AOE_RADIUS + 1):
                    cell = (robot.pos[0] + ox, robot.pos[1] + oy)
                    for seat2, pos2 in pre_pos.items():
                        if seat2 == robot.seat or pos2 != cell:
                            continue
                        victim = self.robots[seat2]
                        if not victim.alive or self._is_protected(victim, tick):
                            continue
                        targets.append((seat2, float(AOE_DEFENDED if victim.defending
                                                     else AOE_DAMAGE)))
            return targets
        seat, other = near
        if self._is_protected(other, tick):
            return [(seat, 0.0)]
        return [(seat, float(DEFENDED_DAMAGE if other.defending else ATTACK_DAMAGE))]

    def _attack_direction(self, robot):
        """攻击方向来自本 Tick 提交的动作，而不是上一 Tick 的回执。"""
        direction = robot.pending_attack_direction
        return direction if direction in DIRECTIONS else "right"

    def _check_missions(self, robot):
        for mission in MISSIONS:
            mid = mission["id"]
            if mid in robot.mission_ids:
                continue
            kind = mission["type"]
            if kind == "normal_resources" and robot.normal_count >= mission["target"]:
                robot.mission_ids.append(mid)
            elif kind == "high_resources" and robot.high_count >= mission["target"]:
                robot.mission_ids.append(mid)
            elif kind == "reach" and robot.reached:
                robot.mission_ids.append(mid)

    def results(self):
        out = {"seats": {}, "errors": list(self.errors),
               "remaining_resources": sum(self.remaining.values())}
        for seat, robot in self.robots.items():
            out["seats"][seat] = {
                "resource_value": robot.resource_value,
                "damage": round(robot.damage, 2),
                "missions": len(robot.mission_ids),
                "mission_ids": list(robot.mission_ids),
                "deaths": robot.deaths,
                "invalid_actions": robot.invalid_actions,
                "blocked_moves": robot.blocked_moves,
                "attacks": robot.attacks,
                "waits": robot.waits,
                "moves": robot.moves,
                "score": _score(robot),
            }
        if self.decision_ms:
            ordered = sorted(self.decision_ms)
            index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.99) - 1))
            out["plan_p99_ms"] = round(ordered[index], 2)
            out["plan_max_ms"] = round(ordered[-1], 2)
        return out


def _score(robot):
    r = 30.0 * min(robot.resource_value / 120.0, 1.0)
    d = 15.0 * min(robot.damage / 500.0, 1.0)
    m = 5.0 * len(robot.mission_ids)
    return round(r + d + m, 3)
