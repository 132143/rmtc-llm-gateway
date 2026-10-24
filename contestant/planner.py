"""决策内核：把一帧世界状态变成"一个动作"（纯计算，无 I/O、无随机、无时钟）。

它只回答协议允许的那一个问题：这一步 move/attack/defend/wait 往哪走。

分层（与 01 §6.1 的硬约束层一一对应）：
    H1 致死区撤离（由 avoid_cells 给出，本层只负责"不往里走"）
    H2 争格冲突改道（同时行动博弈，规则 §5.2/§6.1）
    H3 状态闸门（阵亡只能 wait；出生保护不能 attack）
    H4 洁身自好期间不攻打防御目标（§8.8：反伤 20）
    H5 幻术反向由 agent 的移动出口统一处理，本层**不重复反转**
    H6 任务硬截止（tick ≥ T_TASK3 时覆盖常规打分）

打分（03 §1）：U = 期望得分 × 成功率 / (到达 tick + 风险溢价)，五类目标各一条公式：
    资源 P-R / 任务 P-M / 治疗 P-H / 堡垒 P-F / 战斗 P-C。

工程约束：
  - 每次决策只做一次 BFS（顺带拿到"第一步方向"），最多再为每台敌车做一次；
  - 网格 24×24、资源 ≤16、敌车 ≤3，单次决策远低于 24 ms（P-D-04 的上限是 120 ms）。
"""

from __future__ import annotations

from collections import deque

from contestant import tactics

DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
DIR_LIST = ("up", "down", "left", "right")


class WorldView:
    """planner 需要的全部输入。agent 负责填充，planner 只读。

    约定：所有坐标都是绝对坐标；resources / enemies 里带 last_seen（tick），
    用来做记忆衰减与"视野外不参与竞争折扣"。
    """

    __slots__ = ("pos", "tick", "hp", "alive", "protected", "defending", "width", "height",
                 "blocked", "resources", "enemies", "reach_target", "reach_done",
                 "avoid_cells", "danger_level", "heal_points", "fort_zone", "fort_until",
                 "dry_ticks", "resource_value", "damage", "missions_completed",
                 "combat_allowed", "params", "deaths", "war_phase")

    def __init__(self, pos, tick, hp, width, height, alive=True, protected=False,
                 defending=False, blocked=None, resources=None, enemies=None,
                 reach_target=None, reach_done=False, avoid_cells=None, danger_level="none",
                 heal_points=None, fort_zone=None, fort_until=None, dry_ticks=0,
                 resource_value=0, damage=0, missions_completed=(), combat_allowed=True,
                 params=None, deaths=0, war_phase=False):
        self.pos = pos
        self.tick = tick
        self.hp = hp
        self.alive = alive
        self.protected = protected
        self.defending = defending
        self.width = width
        self.height = height
        self.blocked = blocked or set()
        self.resources = resources or []
        self.enemies = enemies or []
        self.reach_target = reach_target
        self.reach_done = reach_done
        self.avoid_cells = avoid_cells or set()
        self.danger_level = danger_level or "none"
        self.heal_points = heal_points or []
        self.fort_zone = fort_zone
        self.fort_until = fort_until
        self.dry_ticks = dry_ticks
        self.resource_value = resource_value
        self.damage = damage
        self.missions_completed = list(missions_completed)
        self.combat_allowed = combat_allowed
        self.params = params or tactics.Params()
        self.deaths = deaths
        self.war_phase = bool(war_phase)


class DistField:
    """一次 BFS 的结果：到每个格子的步数与"朝哪个方向迈第一步"。"""

    __slots__ = ("dist", "first")

    def __init__(self, dist, first):
        self.dist = dist
        self.first = first

    def d(self, cell):
        return self.dist.get(tuple(cell))

    def step(self, cell):
        return self.first.get(tuple(cell))


def build_field(start, blocked, width, height):
    """从 start 做一次 BFS；返回 DistField。不可达的格子不在 dist 里。"""
    start = tuple(start)
    dist = {start: 0}
    first = {}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        base = dist[cell]
        for name in DIR_LIST:
            dx, dy = DIRECTIONS[name]
            nxt = (cell[0] + dx, cell[1] + dy)
            if not (0 <= nxt[0] < width and 0 <= nxt[1] < height):
                continue
            if nxt in blocked or nxt in dist:
                continue
            dist[nxt] = base + 1
            first[nxt] = name if cell == start else first[cell]
            queue.append(nxt)
    return DistField(dist, first)


def _axis_direction(source, target):
    """同排/同列才给攻击方向；否则 None（规则 §6.2 只沿四轴 1–2 格）。"""
    dx = target[0] - source[0]
    dy = target[1] - source[1]
    if dx == 0 and dy != 0:
        return "down" if dy > 0 else "up"
    if dy == 0 and dx != 0:
        return "right" if dx > 0 else "left"
    return None


def _manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


class Planner:
    """收益表决策器。所有输入来自 WorldView，输出一个动作字典或 None。

    返回 None 表示"我不该管这个 Tick"，由 agent 的常规路径（探索/兜底）接手。
    """

    def __init__(self, params: tactics.Params | None = None):
        self.params = params or tactics.Params()
        self.last_reason = ""
        self.last_utility = 0.0
        self._field = None
        self._enemy_cache = []
        self._tick_now = 0

    # ------------------------------------------------------------ 对外入口
    def plan(self, view: WorldView) -> dict | None:
        if view.pos is None or not view.width or not view.height:
            return None
        # H3 状态闸门：阵亡只能 wait（规则 §7 / §12.3 E_STATE）
        if not view.alive:
            return {"action": "wait", "reason": "dead"}
        p = view.params

        self._field = build_field(view.pos, view.blocked, view.width, view.height)
        # 敌车快照：位置、最后确认 tick、上一 Tick 的实际移动方向（用于争格预测）
        self._enemy_cache = [(entity, tuple(epos), int(last_seen or view.tick), direction)
                             for entity, epos, last_seen, direction in view.enemies]
        self._tick_now = view.tick
        candidates = []

        # H6 任务硬截止：越过 T_TASK3 还没到过 [12,12]，插队覆盖打分
        if view.reach_target and not view.reach_done:
            dist = self._field.d(view.reach_target)
            # 到达任务只值 5 分，专程跑 20 多格不划算（03 §5.1：要"顺路"）。
            # 只在三种情况给全额：已进战争期（资源没了，机会成本≈0）、临近硬截止、或本来就近。
            near = dist is not None and dist <= p.mission_max_distance
            deadline = view.tick >= p.t_task3 - 60
            if dist is not None and dist > 0 and (view.war_phase or deadline or near):
                urgent = view.tick >= p.t_task3
                u = tactics.mission_utility(dist, view.tick, p)
                candidates.append(("mission", u * (3.0 if urgent else 1.0), view.reach_target,
                                   {"urgent": urgent}))

        # 治疗点
        for point, until in view.heal_points:
            if until is not None and view.tick > until:
                continue
            dist = self._field.d(point)
            if dist is None or dist == 0:
                continue
            candidates.append(("heal", tactics.heal_utility(view.hp, dist, p), tuple(point), {}))

        # 堡垒（天下无双）
        if view.fort_zone and (view.fort_until is None or view.tick <= view.fort_until):
            center = self._zone_center(view.fort_zone)
            dist = self._field.d(center)
            if dist is not None and dist > 0:
                near = sum(1 for e in view.enemies if _manhattan(e[1], center) <= 3)
                candidates.append(("fort", tactics.fort_utility(dist, near, p), center, {}))

        # 资源
        for pos, value, seen_tick in view.resources:
            pos = tuple(pos)
            if pos == view.pos:
                continue
            age = max(0, view.tick - int(seen_tick))
            if age > p.mem_forget:
                continue
            dist = self._field.d(pos)
            if dist is None:
                continue
            race = self._race_discount(pos)
            route = self._route_bonus(view, pos)
            decay = tactics.mem_decay(age) if p.use_decay else 1.0
            headroom = tactics.resource_headroom(view.resource_value, p)
            candidates.append(("resource", tactics.resource_utility(
                value, dist, race, route, decay, p, headroom), pos,
                {"value": value, "race": race, "headroom": headroom}))

        weighted = self._renormalize_and_pick(candidates, view)
        combat = self._combat_candidate(view)
        if weighted is None:
            # 没有经济目标时也要评估战斗，否则"资源抢光后不会打人"
            if combat is not None:
                return self._do_combat(view, combat[2], combat[3], combat[1])
            return self._fallback(view)
        kind, utility, target, meta = weighted
        self.last_utility = utility

        # 战斗与五类经济目标直接比 U（打得到、值得打才参与）
        if combat is not None and combat[1] > utility:
            kind, utility, target, meta = combat
            self.last_utility = utility

        if kind == "combat":
            return self._do_combat(view, target, meta, utility)
        return self._do_move(view, kind, target, utility)

    # ------------------------------------------------------------ 竞争折扣
    def _race_discount(self, goal):
        """Δ = 敌方最近距离 − 我方距离。视野外的敌车按"最后已知位置 + 可能位移"估计。"""
        if not self.params.use_race:
            return 1.0
        mine = self._field.d(goal)
        if mine is None:
            return 0.2
        best = None
        for entity, epos, last_seen, _eff in self._enemies():
            # 记忆中的敌车用曼哈顿估计（BFS 太贵且信息不真）
            enemy_dist = _manhattan(epos, goal)
            travel = max(0, self._tick_now - int(last_seen))
            enemy_dist = max(1, enemy_dist - min(travel, 3))
            if best is None or enemy_dist < best:
                best = enemy_dist
        if best is None:
            return 1.0
        raw = tactics.race_discount(best - mine)
        strength = max(0.0, min(1.0, float(self.params.race_strength)))
        return 1.0 - strength * (1.0 - raw)

    def _route_bonus(self, view, goal):
        """打包加成：目标在通往"当前主锚点"的方向通道内（转角 ≤45°）时 ×1.15。"""
        if not self.params.use_route:
            return 1.0
        anchor = view.reach_target
        if not anchor or view.pos is None:
            return 1.0
        ax = anchor[0] - view.pos[0]
        ay = anchor[1] - view.pos[1]
        gx = goal[0] - view.pos[0]
        gy = goal[1] - view.pos[1]
        if (ax == 0 and ay == 0) or (gx == 0 and gy == 0):
            return 1.0
        # 同象限或同轴 → 视为顺路
        if (ax > 0) == (gx >= 0) and (ay > 0) == (gy >= 0):
            return self.params.route_bonus
        return 1.0

    # ------------------------------------------------------------ 战斗评估
    def _combat_candidate(self, view):
        p = view.params
        if not view.combat_allowed or view.protected:
            return None
        best = None
        for entity, epos, last_seen, direction in self._enemies():
            dist = _manhattan(view.pos, epos)
            protected = bool(entity.get("protected"))
            if protected:
                continue                      # 规则 §6.2：0 伤害且挡后方
            defending = bool(entity.get("defending"))
            if view.danger_level == "self_defense" and defending:
                continue                      # H4（洁身自好）
            in_line = _axis_direction(view.pos, epos) is not None and dist <= 2
            situation = "in_aoe" if self._in_aoe(view, epos) else (
                "entering_point" if self._entering_point(view, epos) else "axis_free")
            if not in_line and not self._in_aoe(view, epos):
                # 不在射程：只有开了追击/伏击且确实值得，才允许"靠近一轮"
                if p.pursue_r <= 0 or dist > p.pursue_r:
                    continue
                situation = "pinned" if self._is_pinned(view, epos) else situation
            escape = tactics.escape_probability(situation)
            if self._is_pinned(view, epos):
                escape = tactics.escape_probability("pinned")
            nominal = tactics.AOE_DAMAGE if self._in_aoe(view, epos) else (
                tactics.DEFENDED_DAMAGE if defending else tactics.ATTACK_DAMAGE)
            hp_enemy = entity.get("hp")
            hp_enemy = hp_enemy if isinstance(hp_enemy, (int, float)) else tactics.HP_MAX
            # 一轮交火能打出几发：跑不掉的（夹击/必经点/无双范围）能连打，自由逃逸的只够一发
            hits = 1.0 + 2.0 * (1.0 - escape)
            expected = min(nominal * hits, max(0.0, hp_enemy))
            if expected <= 0:
                continue
            expected *= tactics.damage_headroom(view.damage, p)
            p_death = tactics.death_probability(
                view.hp, self._enemies_adjacent(view), vulnerable=False, in_heal=bool(view.heal_points))
            opp = self._opportunity_cost(view)
            u = tactics.combat_utility(expected, escape, p_death, opp, p)
            if view.deaths >= p.max_death:
                u = u / 1.5                    # P-C-12：死亡达上限，开战阈值 ×1.5
            if u < p.engage_th:
                continue
            if best is None or u > best[1]:
                best = ("combat", u, epos,
                        {"entity": entity, "in_line": in_line, "escape": escape,
                         "expected": expected, "situation": situation})
        return best

    def _enemies(self):
        return self._enemy_cache

    def _enemies_adjacent(self, view):
        n = 0
        for _e, epos, _t, _d in self._enemy_cache:
            if _manhattan(view.pos, epos) <= 2:
                n += 1
        return max(1, n)

    def _in_aoe(self, view, epos):
        if not view.fort_zone:
            return False
        if view.fort_until is not None and view.tick > view.fort_until:
            return False
        return max(abs(view.pos[0] - epos[0]), abs(view.pos[1] - epos[1])) <= tactics.AOE_RADIUS

    def _entering_point(self, view, epos):
        for pos, _value, _seen in view.resources:
            if _manhattan(epos, tuple(pos)) <= 1:
                return True
        if view.reach_target and _manhattan(epos, view.reach_target) <= 1:
            return True
        return False

    def _is_pinned(self, view, epos):
        """目标四邻可退格 ≤1 → 地形夹击，逃逸概率 0.15（03 §7.3）。"""
        exits = 0
        for dx, dy in DIRECTIONS.values():
            cell = (epos[0] + dx, epos[1] + dy)
            if not (0 <= cell[0] < view.width and 0 <= cell[1] < view.height):
                continue
            if cell in view.blocked:
                continue
            exits += 1
        return exits <= 1

    def _opportunity_cost(self, view):
        """放弃的采集机会：最佳资源收益 × 预计战斗 tick 数（03 §7.1 的 OPP_COST）。"""
        best = 0.0
        for pos, value, seen in view.resources:
            dist = self._field.d(tuple(pos))
            if dist is None:
                continue
            u = tactics.resource_utility(value, dist, 1.0, 1.0,
                                         tactics.mem_decay(max(0, view.tick - int(seen))),
                                         view.params,
                                         tactics.resource_headroom(view.resource_value, view.params))
            best = max(best, u)
        return best * 2.0

    # ------------------------------------------------------------ 动作落地
    def _do_combat(self, view, target, meta, utility):
        """能打就打（直线 1–2 格，或无双 5×5），打不到才靠近。

        规则 §6.2 的攻击只沿四轴 1–2 格：不在轴线上就 ATTACK 等于把一个 Tick 扔进空处
        （乱给方向也打不到任何人）。所以"打不到"时退回"靠近"，而不是硬发一个方向。
        """
        direction = _axis_direction(view.pos, target)
        in_aoe = self._in_aoe(view, target) and view.fort_zone is not None
        if direction is not None or in_aoe:
            self.last_reason = f"combat:{meta.get('situation')}:u={utility:.2f}"
            return {"action": "attack", "direction": direction or "right",
                    "reason": self.last_reason}
        step = self._safe_step(view, tuple(target))
        if step is None:
            return None
        self.last_reason = f"approach:u={utility:.2f}"
        return {"action": "move", "direction": step, "reason": self.last_reason}

    def _do_move(self, view, kind, target, utility):
        if tuple(target) == tuple(view.pos):
            return None
        step = self._safe_step(view, target)
        if step is None:
            return self._fallback(view)
        self.last_reason = f"{kind}:u={utility:.2f}:to={target}"
        return {"action": "move", "direction": step, "reason": self.last_reason}

    def _safe_step(self, view, target):
        """取"朝目标的第一步"，必要时改道避开 H1 致死区与 H2 争格。"""
        step = self._field.step(target)
        if step is None:
            return None
        if self._step_is_safe(view, step):
            return step
        # 改道：四个方向里挑一个既安全、又能缩短到目标距离的
        best = None
        goal_dist = self._field.d(target) or 0
        for name in DIR_LIST:
            dx, dy = DIRECTIONS[name]
            cell = (view.pos[0] + dx, view.pos[1] + dy)
            if not (0 <= cell[0] < view.width and 0 <= cell[1] < view.height):
                continue
            if cell in view.blocked or not self._step_is_safe(view, name):
                continue
            d = abs(cell[0] - target[0]) + abs(cell[1] - target[1])
            if best is None or d < best[0]:
                best = (d, name)
        if best is not None and best[0] <= goal_dist + 1:
            return best[1]
        return step

    def _step_is_safe(self, view, direction):
        dx, dy = DIRECTIONS[direction]
        cell = (view.pos[0] + dx, view.pos[1] + dy)
        if cell in view.avoid_cells:                 # H1 致死区
            return False
        if view.params.use_contention:
            predicted = self._predicted_enemy_cells(view)
            if cell in predicted:                    # H2 争格/占位：进去必然 E_BLOCKED
                return False
        return True

    def _predicted_enemy_cells(self, view):
        """用 last_action.effective_direction 推断敌车下一 Tick 的目标格（01 §6.3）。"""
        out = set()
        for _entity, epos, _seen, direction in self._enemy_cache:
            if direction in DIRECTIONS:
                dx, dy = DIRECTIONS[direction]
                out.add((epos[0] + dx, epos[1] + dy))
            out.add(tuple(epos))                     # 也可能原地不动/打死目标后不动
        return out

    def _zone_center(self, zone):
        x1, y1, x2, y2 = zone
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    # ------------------------------------------------------------ 兜底
    # 战争期巡逻点（中心 + 四角内缩一格）：资源没了以后，机会来自"遇到对手"。
    WAR_HOTSPOTS = ((0, 0), (2, 2), (-3, 2), (2, -3), (-3, -3))

    def _fallback(self, view):
        """没有可打分目标时怎么办。

        经济期：返回 None，把探索交给 agent（它按视野维护 explored，探索质量比这里硬编码强）。
        战争期：资源已经见底，原地 WAIT 等于白扔 Tick（实测融合版在战争期 271/500 Tick 在等），
                所以主动去"热区/最后一个已知敌车位置"找人——这是规则允许的自主战斗，
                也是原版 hybrid 模式真正拿到 1000+ 伤害的来源。
        """
        if view.war_phase:
            step = self._safe_step(view, self._hunt_target(view))
            if step is not None:
                self.last_reason = "hunt"
                return {"action": "move", "direction": step, "reason": "hunt"}
        self.last_reason = "fallback:none"
        return None

    def _hunt_target(self, view):
        """优先去最后一个已知敌车位置，否则按 Tick 轮换热区（确定性，不用随机数）。"""
        best = None
        for _entity, epos, last_seen, _direction in self._enemy_cache:
            dist = _manhattan(view.pos, epos)
            if best is None or dist < best[0]:
                best = (dist, epos)
        if best is not None:
            return best[1]
        index = (view.tick // 40) % len(self.WAR_HOTSPOTS)
        ox, oy = self.WAR_HOTSPOTS[index]
        x = ox if ox >= 0 else view.width + ox
        y = oy if oy >= 0 else view.height + oy
        return (max(0, min(view.width - 1, x)), max(0, min(view.height - 1, y)))

    # ------------------------------------------------------------ 打分归一
    def _renormalize_and_pick(self, candidates, view):
        if not candidates:
            return None
        """P-R-05 高价值距离容忍比：仅当 高价值距离 / 普通距离 > HIGH_PRIORITY 时才选普通。

        与"价值/距离"排序的关系：20/d_h > 10/d_n ⟺ d_h < 2·d_n，两条规则在阈值取 2.0 时
        完全一致，所以这里只做"取到更远的高价值资源"的显式判断，避免多个资源并列时抖动。
        """
        ranked = sorted(candidates, key=lambda item: -item[1])
        top = ranked[0]
        if top[0] != "resource":
            return top
        high = [cand for cand in ranked if cand[0] == "resource" and cand[3].get("value", 0) >= 20]
        normal = [cand for cand in ranked if cand[0] == "resource" and cand[3].get("value", 0) < 20]
        if not high or not normal:
            return top
        best_high = max(high, key=lambda item: item[1])
        best_normal = max(normal, key=lambda item: item[1])
        d_high = self._field.d(best_high[2]) or 1
        d_normal = self._field.d(best_normal[2]) or 1
        if d_high / max(1, d_normal) > self.params.high_priority:
            return best_normal
        return best_high


def make_planner(decision_type: str) -> Planner:
    """按已登记枚举造一个决策器（参数增量来自 02 §3）。"""
    return Planner(tactics.params_for(decision_type))
