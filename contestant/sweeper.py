"""扫图型假想敌（SweeperBot）：移动优先、扫遍全图、拿最近资源、躲危险、打一下就跑。

为什么要造它：我们自己那套（guarded）偏经济、会等、会算收益；实战里最难受的对手不是"猎人"，
而是**一直在动、把图扫光、顺手就用最近资源占你便宜**的机器人。用它当假想敌，
才能回答"我们的策略在资源被系统性榨干时还稳不稳"。

行为优先级（与交付文档里的"四步"一一对应）：
  1. 活着且一直在动：除非四邻全堵，否则**绝不 wait**（这是它和我们的最大区别）；
  2. 危险格（模型给的 avoid_cells）→ 立刻出去；
  3. 打一下就跑：射程内（四轴 1–2 格）有可打目标 → 打一发，随后 5 片内远离它；
  4. 看得见/记得的资源里挑**最近**的一个（不看价值权重，纯贪距离）；
  5. 否则按蛇形扫图路径推进（尽量覆盖更多格子）；
  6. 连扫图目标都堵住了 → 随便找一个能走的方向，保证还在动。

刻意不做的：不追人（规则 §6.1 追不上）、不蹲堡垒、不算收益表——它就是"无脑但不停"。
"""

from __future__ import annotations

from contestant.strategy import DIRECTIONS, step_toward

ORDER = ("up", "down", "left", "right")


def _dist(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _axis_direction(source, target):
    dx, dy = target[0] - source[0], target[1] - source[1]
    if dx == 0 and dy != 0:
        return "down" if dy > 0 else "up"
    if dy == 0 and dx != 0:
        return "right" if dx > 0 else "left"
    return None


class SweeperBrain:
    """纯函数式决策（带一点内部状态），不碰 ROS / 网络。"""

    FLEE_TICKS = 5          # 打完就跑：这几片内远离刚打的那个敌人
    COVER_RADIUS = 3        # 走过就算"覆盖过"的视野半径（用于扫图去重）
    ATTACK_RANGE = 2        # 规则 §6.2：四轴 1–2 格

    def __init__(self, width, height):
        self.width = int(width)
        self.height = int(height)
        self.covered = set()            # 已覆盖的格子（自己走过 + 附近）
        self.flee_until = -1
        self.flee_from = None
        self.last_reason = ""
        self.attacks = 0
        self.flees = 0

    # ------------------------------------------------------------ 主入口
    def plan(self, view):
        if view.pos is None or not view.width or not view.height:
            return None
        if not view.alive:
            return {"action": "wait", "reason": "dead"}
        self._mark_covered(view.pos)

        step = self._escape_danger(view)
        if step:
            self.last_reason = "escape"
            return {"action": "move", "direction": step, "reason": "escape"}

        if view.tick < self.flee_until:
            step = self._step_away(view, self.flee_from)
            if step:
                self.last_reason = "flee"
                return {"action": "move", "direction": step, "reason": "flee"}

        shot = self._attack(view)
        if shot is not None:
            return shot

        target = self._nearest_resource(view)
        kind = "resource"
        if target is None:
            target = self._sweep_target(view)
            kind = "sweep"
        if target is not None:
            step = step_toward(view.pos, target, view.blocked, self.width, self.height)
            if step and not self._is_dangerous(view, step):
                self.last_reason = kind
                return {"action": "move", "direction": step, "reason": kind}

        step = self._any_free_step(view)
        if step:
            self.last_reason = "roam"
            return {"action": "move", "direction": step, "reason": "roam"}
        self.last_reason = "stuck"
        return {"action": "wait", "reason": "stuck"}

    # ------------------------------------------------------------ 1) 危险格
    def _escape_danger(self, view):
        if not view.avoid_cells or view.pos not in view.avoid_cells:
            return None
        best = None
        for name in ORDER:
            dx, dy = DIRECTIONS[name]
            cell = (view.pos[0] + dx, view.pos[1] + dy)
            if not (0 <= cell[0] < self.width and 0 <= cell[1] < self.height):
                continue
            if cell in view.blocked or cell in view.avoid_cells:
                continue
            gap = min(_dist(cell, danger) for danger in view.avoid_cells) if view.avoid_cells else 0
            if best is None or gap > best[0]:
                best = (gap, name)
        return best[1] if best else None

    # ------------------------------------------------------------ 2) 打完就跑
    def _step_away(self, view, from_cell):
        if from_cell is None:
            return None
        best = None
        for name in ORDER:
            dx, dy = DIRECTIONS[name]
            cell = (view.pos[0] + dx, view.pos[1] + dy)
            if not (0 <= cell[0] < self.width and 0 <= cell[1] < self.height):
                continue
            if cell in view.blocked or self._is_dangerous(view, name):
                continue
            score = _dist(cell, from_cell)
            if best is None or score > best[0]:
                best = (score, name)
        return best[1] if best else None

    # ------------------------------------------------------------ 3) 打一下就跑
    def _attack(self, view):
        if not view.combat_allowed or view.protected:
            return None
        best = None
        for entity, epos, _last_seen, _direction in view.enemies:
            if entity.get("protected") is True:
                continue                      # 规则 §6.2：打保护目标 0 伤害
            gap = _dist(view.pos, epos)
            direction = _axis_direction(view.pos, epos)
            if direction is None or gap > self.ATTACK_RANGE or gap == 0:
                continue
            if best is None or gap < best[0]:
                best = (gap, direction, epos)
        if best is None:
            return None
        self.flee_until = view.tick + self.FLEE_TICKS
        self.flee_from = tuple(best[2])
        self.attacks += 1
        self.last_reason = "hit_and_run"
        return {"action": "attack", "direction": best[1], "reason": "hit_and_run"}

    # ------------------------------------------------------------ 4) 最近的资源
    def _nearest_resource(self, view):
        best = None
        for pos, _value, _seen in view.resources:
            pos = tuple(pos)
            gap = _dist(view.pos, pos)
            if gap == 0:
                continue
            if pos in view.avoid_cells:
                continue                      # 资源压在危险格里：不拿
            if best is None or gap < best[0]:
                best = (gap, pos)
        return best[1] if best else None

    # ------------------------------------------------------------ 5) 扫图目标
    def _sweep_target(self, view):
        """蛇形推进：按 y 行、交替左右，优先"附近未覆盖"的格子。"""
        radius = 6
        while radius <= max(self.width, self.height) * 2:
            best = None
            for y in range(max(0, view.pos[1] - radius), min(self.height, view.pos[1] + radius + 1)):
                for x in range(max(0, view.pos[0] - radius), min(self.width, view.pos[0] + radius + 1)):
                    cell = (x, y)
                    if cell in self.covered or cell in view.blocked:
                        continue
                    if cell in view.avoid_cells:
                        continue
                    # 蛇形偏好：按与当前行进方向一致的列优先，减少来回横跳
                    rank = (_dist(view.pos, cell), 0 if y % 2 == 0 else 1)
                    if best is None or rank < best[0]:
                        best = (rank, cell)
            if best is not None:
                return best[1]
            radius += 6
        return None

    # ------------------------------------------------------------ 6) 随便走
    def _any_free_step(self, view):
        for name in ORDER:
            dx, dy = DIRECTIONS[name]
            cell = (view.pos[0] + dx, view.pos[1] + dy)
            if not (0 <= cell[0] < self.width and 0 <= cell[1] < self.height):
                continue
            if cell in view.blocked or self._is_dangerous(view, name):
                continue
            return name
        return None

    # ------------------------------------------------------------ 工具
    def _is_dangerous(self, view, direction):
        dx, dy = DIRECTIONS.get(direction, (0, 0))
        cell = (view.pos[0] + dx, view.pos[1] + dy)
        return cell in view.avoid_cells

    def _mark_covered(self, pos):
        pos = tuple(pos)
        r = self.COVER_RADIUS
        for y in range(max(0, pos[1] - r), min(self.height, pos[1] + r + 1)):
            for x in range(max(0, pos[0] - r), min(self.width, pos[0] + r + 1)):
                self.covered.add((x, y))

    def covered_ratio(self):
        total = max(1, self.width * self.height)
        return len(self.covered) / total
