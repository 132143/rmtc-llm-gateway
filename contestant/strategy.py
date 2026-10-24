"""策略：纯 Python，不依赖 ROS，所以能直接放在模拟器里跑比赛。

约定：策略每 Tick 只回答一个问题——"这一步怎么走/打/防/等"。
它拿到的信息严格限于协议允许的公开感知（本车坐标、视野内实体、地图尺寸）。
"""

from __future__ import annotations

from collections import deque

DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def step_toward(source, target, blocked, width, height):
    """BFS 找一步；被挡就退化成曼哈顿方向的第一个可走方向。"""
    if source == target:
        return None
    queue = deque([(source, None)])
    seen = {source}
    while queue:
        (x, y), first = queue.popleft()
        for name, (dx, dy) in DIRECTIONS.items():
            nxt = (x + dx, y + dy)
            if not (0 <= nxt[0] < width and 0 <= nxt[1] < height):
                continue
            if nxt in blocked or nxt in seen:
                continue
            seen.add(nxt)
            step = first or name
            if nxt == target:
                return step
            queue.append((nxt, step))
    # 目标不可达：朝差值大的轴硬走一步，撞了下一 Tick 自然会学到"这里堵"
    dx = target[0] - source[0]
    dy = target[1] - source[1]
    if abs(dx) >= abs(dy) and dx:
        return "right" if dx > 0 else "left"
    if dy:
        return "down" if dy > 0 else "up"
    return None


class BaseStrategy:
    name = "base"

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.blocked = set()          # 撞过的格子（协议允许记住自己的失败）
        self.seen_resources = {}      # 记忆：位置 -> 价值

    def observe(self, view):
        for pos, value in view.visible_resources:
            self.seen_resources[pos] = value
        for pos in view.collected:
            self.seen_resources.pop(pos, None)
        # 走到哪就是采到了哪（协议：进入资源格即采集），记忆里这一格要清掉，
        # 否则会一直把"脚下这块"当目标，原地来回抖。
        self.seen_resources.pop(tuple(view.pos), None)
        self.blocked.update(view.blocked_now)

    def move(self, direction):
        return {"action": "move", "direction": direction}

    def wait(self):
        return {"action": "wait"}

    def decide(self, view):
        raise NotImplementedError


class SnakeStrategy(BaseStrategy):
    """骨架程序原本的策略：看得见就朝最近资源走，看不见就蛇形扫图。"""

    name = "snake（原骨架）"

    def __init__(self, width, height):
        super().__init__(width, height)
        self.sweep_dir = 1

    def decide(self, view):
        if view.visible_resources:
            target = min(view.visible_resources,
                         key=lambda item: abs(item[0][0] - view.pos[0]) + abs(item[0][1] - view.pos[1]))
            direction = step_toward(view.pos, target[0], set(), self.width, self.height)
            return self.move(direction) if direction else self.wait()
        x, y = view.pos
        if self.sweep_dir > 0:
            if x < self.width - 2:
                return self.move("right")
            self.sweep_dir = -1
            return self.move("down") if y < self.height - 2 else self.move("up")
        if x > 1:
            return self.move("left")
        self.sweep_dir = 1
        return self.move("down") if y < self.height - 2 else self.move("up")


class GreedyBfsStrategy(BaseStrategy):
    """有记忆的贪心 + BFS 绕障。

    排序用 priority = value / (dist + detour)：
      detour 小 → 谁近去谁（省 Tick）；
      detour 大 → 谁值钱去谁（远的高价值也认）。
    注意别写成 value × 常数 / dist —— 乘常数不改变顺序，测出来会"毫无区别"（踩过）。
    """

    name = "greedy_bfs"

    def __init__(self, width, height, detour=0.0, explore_bias="near"):
        super().__init__(width, height)
        self.detour = detour
        # 探索偏置：near = 最近的未探明格（原版）；far = 最远的未探明格。
        # 用途只在"任务缺稀缺资源、需要更快铺开视野"时临时切到 far。
        self.explore_bias = explore_bias

    def decide(self, view):
        targets = dict(self.seen_resources)
        for pos, value in view.visible_resources:
            targets[pos] = value
        if targets:
            def rank(item):
                pos, value = item
                dist = abs(pos[0] - view.pos[0]) + abs(pos[1] - view.pos[1])
                return (-value / (dist + self.detour), dist)
            target, _value = sorted(targets.items(), key=rank)[0]
            direction = step_toward(view.pos, target, self.blocked | view.obstacles, self.width, self.height)
            if direction:
                return self.move(direction)
        return self.explore(view)

    def explore(self, view):
        """没资源记忆：去一个未探明的格子。

        默认去最近的（原版）；explore_bias="far" 时去最远的——用于"缺稀缺资源"时
        更快铺开视野（最近优先容易在出生点附近反复打转，实测会把整块区域留到很晚才看）。
        """
        best = None
        far = str(getattr(self, "explore_bias", "near")) == "far"
        for y in range(self.height):
            for x in range(self.width):
                if (x, y) in view.explored or (x, y) in (self.blocked | view.obstacles):
                    continue
                dist = abs(x - view.pos[0]) + abs(y - view.pos[1])
                if best is None or (dist > best[0] if far else dist < best[0]):
                    best = (dist, (x, y))
        if best is None:
            return self.wait()
        direction = step_toward(view.pos, best[1], self.blocked | view.obstacles, self.width, self.height)
        return self.move(direction) if direction else self.wait()


class MissionFirstStrategy(GreedyBfsStrategy):
    """第二阶段用：任务坐标优先（5 分很便宜），资源其次。"""

    name = "mission_first"

    def __init__(self, width, height, detour=2.0, mission_targets=()):
        super().__init__(width, height, detour)
        self.mission_targets = list(mission_targets)
        self.done_missions = set()

    def decide(self, view):
        for target in self.mission_targets:
            if target in self.done_missions:
                continue
            if tuple(view.pos) == tuple(target):
                self.done_missions.add(target)
                continue
            direction = step_toward(view.pos, tuple(target),
                                    self.blocked | view.obstacles, self.width, self.height)
            if direction:
                return self.move(direction)
        return super().decide(view)
