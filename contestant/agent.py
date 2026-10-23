"""把感知变成动作的那一层（纯 Python，不依赖 ROS）。

为什么单独一层：比赛最容易丢分的地方不是"策略不够聪明"，而是
"字段缺了崩掉 / 同一 Tick 重复决策 / 上一场的缓存没清"。
这些都在这里用纯函数解决，于是可以直接写测试，不用开 ROS。

协议对照：
- §3 感知：self.position 是绝对坐标；visible_entities[].position 是 9×9 相对位置字符串；
- §3.1 任务：missions[] 里的 reach 任务给 position，不得当可重复得分次数；
- §4 动作：move/attack/defend/wait，direction 只能是 up/down/left/right；
  tick 与 session_id 必须原样回填；阵亡只能 wait；同一 Tick 重发不重复决策。
"""

from __future__ import annotations

from contestant.strategy import GreedyBfsStrategy, step_toward

DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
ATTACK_ACTIONS = ("move", "attack", "defend", "wait")


def decode_relative(text):
    """9×9 相对位置字符串 → (dx, dy)。'1' 是自己（第 5 行第 5 列），'2' 是目标。"""
    if not isinstance(text, str):
        return None
    rows = text.split("\n")
    if len(rows) != 9:
        return None
    for row_index, row in enumerate(rows):
        column = row.find("2")
        if column >= 0:
            return (column - 4, row_index - 4)
    return (0, 0)


class AgentConfig:
    def __init__(self, seat="A", stage=2, max_hp=100, attack_damage=20,
                 defended_damage=10, low_hp=30, reach_after_tick=250,
                 vision_radius=4):
        self.seat = seat
        self.stage = stage
        self.max_hp = max_hp
        self.attack_damage = attack_damage
        self.defended_damage = defended_damage
        self.low_hp = low_hp
        self.reach_after_tick = reach_after_tick
        self.vision_radius = vision_radius


class Agent:
    """一局一个实例。on_perception() 返回要发布的消息，重复/旧帧返回 None。"""

    def __init__(self, cfg: AgentConfig | None = None, client=None, logger=None):
        self.cfg = cfg or AgentConfig()
        self.client = client
        self._logger = logger
        self.strategy = None
        self.understanding = None
        self.session_id = ""
        self.last_tick = -1
        self.pos = None
        self.width = 0
        self.height = 0
        self.missions = []
        self.reach_done = False
        self.explored = set()          # 已经"看过"的格子（视野窗口覆盖过的）
        self.known_obstacles = set()   # 见过的障碍（静态，可以永久记住）
        self.last_move = None          # (出发点, 方向)：用来解释下一帧的 E_BLOCKED
        self.stats = {"perceptions": 0, "actions": 0, "skipped": 0}

    # ---------- 日志 ----------
    def _log(self, level, text):
        if self._logger is not None:
            try:
                self._logger(level, text)
            except Exception:
                pass

    # ---------- 主体 ----------
    def on_perception(self, data):
        """data 是感知 JSON 解析后的 dict；返回动作 dict（含 tick/session_id）或 None。"""
        if not isinstance(data, dict):
            self.stats["skipped"] += 1
            return None
        tick = data.get("tick")
        session_id = data.get("session_id")
        if not isinstance(tick, int) or isinstance(tick, bool) or not isinstance(session_id, str):
            self.stats["skipped"] += 1
            self._log("warn", "感知缺 tick/session_id，跳过")
            return None

        if session_id != self.session_id:
            self._start_session(session_id)
        if tick <= self.last_tick:
            # 同一 Tick 重发 / 旧帧：不重复推进策略（协议明确要求）
            self.stats["skipped"] += 1
            return None
        self.last_tick = tick
        self.stats["perceptions"] += 1

        me = data.get("self") if isinstance(data.get("self"), dict) else {}
        self._learn_from_last_action(me.get("last_action"))
        position = me.get("position")
        if isinstance(position, (list, tuple)) and len(position) == 2:
            try:
                self.pos = (int(position[0]), int(position[1]))
            except (TypeError, ValueError):
                pass

        map_info = data.get("map") if isinstance(data.get("map"), dict) else {}
        for key, attr in (("width", "width"), ("height", "height")):
            value = map_info.get(key)
            if isinstance(value, int) and value > 0:
                setattr(self, attr, value)
        self._ensure_strategy()

        missions = data.get("missions")
        if isinstance(missions, list):
            self.missions = [m for m in missions if isinstance(m, dict)]

        self._mark_explored()
        for entity in data.get("visible_entities") or []:
            if isinstance(entity, dict) and entity.get("type") == "obstacle":
                relative = decode_relative(entity.get("position"))
                if relative is not None and self.pos is not None:
                    self.known_obstacles.add((self.pos[0] + relative[0], self.pos[1] + relative[1]))

        action = self._decide(data, me)
        if action is None:
            action = {"action": "wait"}
        payload = {"tick": tick, "session_id": session_id}
        payload.update(action)
        self.stats["actions"] += 1
        return payload

    def _start_session(self, session_id):
        self.session_id = session_id
        self.last_tick = -1
        self.pos = None
        self.reach_done = False
        self.strategy = None
        self.explored = set()
        self.known_obstacles = set()
        self.last_move = None
        if self.client is not None:
            from rmtc_client import EventUnderstanding
            self.understanding = EventUnderstanding(self.client, logger=self._logger)
        self._log("info", f"新场次 {session_id}：清空上一场的定位、记忆与在途 AI 请求")

    def _ensure_strategy(self):
        if self.strategy is None and self.width and self.height:
            self.strategy = GreedyBfsStrategy(self.width, self.height, detour=0.0)

    # ---------- 决策 ----------
    def _decide(self, data, me):
        if self.strategy is None or self.pos is None:
            return {"action": "wait"}
        if me.get("alive") is False:
            return {"action": "wait"}      # 阵亡期间只能 wait

        event_text = data.get("event_text")
        mission_text = data.get("mission_text")
        if self.understanding is not None and isinstance(event_text, str) and event_text.strip():
            self.understanding.offer(
                self.session_id, self.last_tick, event_text,
                mission_text=mission_text if isinstance(mission_text, str) else "",
                position=self.pos, map_size=(self.width, self.height), seat=self.cfg.seat)

        enemies = self._enemies(data)
        hp = me.get("hp")
        hp = hp if isinstance(hp, (int, float)) else self.cfg.max_hp

        if self.cfg.stage >= 2:
            adjacent = [e for e in enemies if abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]) <= 2]
            if adjacent:
                target = min(adjacent, key=lambda e: abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]))
                direction = self._line_direction(target[1])
                if direction and not me.get("protected"):
                    return {"action": "attack", "direction": direction}   # 直线 1~2 格才打得到
            if hp <= self.cfg.low_hp and enemies:
                nearest = min(abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]) for e in enemies)
                if nearest <= 3:
                    return {"action": "defend"}

        reach_target = self._reach_target()
        if reach_target is not None and self.cfg.stage >= 2:
            if self.pos == reach_target:
                self.reach_done = True
            elif not self.reach_done and self.last_tick >= self.cfg.reach_after_tick:
                direction = step_toward(self.pos, reach_target,
                                        self.strategy.blocked | self.known_obstacles,
                                        self.width, self.height)
                if direction:
                    return self._move(direction)

        from contestant.sim import View
        resources = self._resources(data)
        view = View(self.pos, resources, self.known_obstacles, self.explored, set(),
                    set(), self.last_tick, self.cfg.vision_radius)
        self.strategy.observe(view)
        action = self.strategy.decide(view)
        if action.get("action") not in ATTACK_ACTIONS:
            return {"action": "wait"}
        if action.get("action") == "move":
            if action.get("direction") not in DIRECTIONS:
                return {"action": "wait"}
            return self._move(action["direction"])
        return action

    # ---------- 记忆维护 ----------
    def _move(self, direction):
        """记下"这一步从哪走、往哪走"，下一帧用它解释 E_BLOCKED。"""
        self.last_move = (self.pos, direction)
        return {"action": "move", "direction": direction}

    def _learn_from_last_action(self, last_action):
        """上一 Tick 撞墙/越界 → 把那格记成障碍（协议允许记住自己的失败）。"""
        attempt = self.last_move
        self.last_move = None
        if not isinstance(last_action, dict) or attempt is None:
            return
        if last_action.get("moved") is not False:
            return
        if last_action.get("error") not in ("E_BLOCKED", "E_OOB"):
            return
        origin, direction = attempt
        dx, dy = DIRECTIONS.get(direction, (0, 0))
        blocked = (origin[0] + dx, origin[1] + dy)
        self.known_obstacles.add(blocked)
        if self.strategy is not None:
            self.strategy.blocked.add(blocked)
            self.strategy.seen_resources.pop(blocked, None)

    def _mark_explored(self):
        """视野窗口覆盖过的格子就是"看过"了；不然探索会退化成原地附近乱走。"""
        if self.pos is None or not self.width or not self.height:
            return
        radius = self.cfg.vision_radius
        for x in range(max(0, self.pos[0] - radius), min(self.width, self.pos[0] + radius + 1)):
            for y in range(max(0, self.pos[1] - radius), min(self.height, self.pos[1] + radius + 1)):
                self.explored.add((x, y))

    # ---------- 感知解析 ----------
    def _resources(self, data):
        out = []
        for entity in data.get("visible_entities") or []:
            if not isinstance(entity, dict):
                continue
            kind = entity.get("type")
            if kind not in ("resource_normal", "resource_high"):
                continue
            relative = decode_relative(entity.get("position"))
            if relative is None:
                continue
            value = entity.get("value")
            if not isinstance(value, int):
                value = 20 if kind == "resource_high" else 10
            out.append(((self.pos[0] + relative[0], self.pos[1] + relative[1]), value))
        return out

    def _enemies(self, data):
        out = []
        for entity in data.get("visible_entities") or []:
            if not isinstance(entity, dict) or entity.get("type") != "robot":
                continue
            relative = decode_relative(entity.get("position"))
            if relative is None:
                continue
            out.append((entity, (self.pos[0] + relative[0], self.pos[1] + relative[1])))
        return out

    def _line_direction(self, target):
        """攻击只能直线 1~2 格：同一行或同一列才给方向。"""
        dx = target[0] - self.pos[0]
        dy = target[1] - self.pos[1]
        if dx == 0 and dy != 0:
            return "down" if dy > 0 else "up"
        if dy == 0 and dx != 0:
            return "right" if dx > 0 else "left"
        return None

    def _reach_target(self):
        for mission in self.missions:
            if mission.get("type") == "reach":
                position = mission.get("position")
                if isinstance(position, (list, tuple)) and len(position) == 2:
                    try:
                        return (int(position[0]), int(position[1]))
                    except (TypeError, ValueError):
                        return None
        return None
