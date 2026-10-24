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
    """可调参数集中在这里，方便在官方裁判上做 A/B（见 scripts/vm/runsweep.sh）。

    strategy: "greedy"（BFS 贪心，默认）/"snake"（原骨架逻辑，用来做基线对照）
    combat_mode:
        "never"          第二阶段也不打人（纯采集）
        "opportunistic"  已经进入直线 1~2 格就出手（默认的旧模式）
        "resource_first" 先采集：资源价值达到 resource_threshold 之前不打人
                        （实测在资源被堵的图上更差，只作为对照保留）
        "finish"         只在能补刀时出手（视野内敌人血量 <= finish_hp 且已进入直线射程）
        "hybrid"        顺路就打；没有资源可捡时改为主动找人（默认；正式节奏实测 45.0，最优）
        "aggressive"     附近（<= aggressive_range 格）有敌人就主动靠过去
    reach_after_tick: 这个 Tick 之后才去"到达指定坐标"的任务点
    resource_threshold: resource_first 模式的开关线（第二阶段资源分 30 分在 120 价值封顶）
    """

    def __init__(self, seat="A", stage=2, max_hp=100, attack_damage=20,
                 defended_damage=10, low_hp=30, reach_after_tick=250,
                 vision_radius=4, strategy="greedy", combat_mode="hybrid",
                 aggressive_range=4, resource_threshold=120, finish_hp=60,
                 ai_heartbeat_tick=3, strategy_detour=0.0, mission_max_distance=8,
                 patrol_when_idle=False, opening_mode="normal", opening_ticks=30):
        self.seat = seat
        self.stage = stage
        self.max_hp = max_hp
        self.attack_damage = attack_damage
        self.defended_damage = defended_damage
        self.low_hp = low_hp
        self.reach_after_tick = reach_after_tick
        self.vision_radius = vision_radius
        self.strategy = strategy
        self.combat_mode = combat_mode
        self.aggressive_range = aggressive_range
        self.resource_threshold = resource_threshold
        self.finish_hp = finish_hp
        # 规则 §10.2：第二阶段每场必须有本场本席位至少一次"真实统一 AI 成功调用"，否则任务分 M=0。
        # 所以不能"等有事件再问"——开局就发一次心跳请求，确保本场有成功记录。
        self.ai_heartbeat_tick = ai_heartbeat_tick
        # 资源取舍：priority = 价值 / (距离 + detour)。0 = 谁近去谁；大 = 谁值钱去谁。
        self.strategy_detour = strategy_detour
        # 任务资源只在这个距离内才专门去追；0 = 不限制（旧行为）。
        # 大地图上"为了 5 分的任务跑去很远"会拖慢开局抢资源（24×24 实测首个资源 26 Tick 才吃到）。
        self.mission_max_distance = mission_max_distance
        # 闲置巡逻：资源抢完、视野里又没敌人时，向巡逻点移动而不是原地 wait。
        # 24×24 实测：等待从 ~300 降到 ~10，但分数没升（47.5 vs 50.0），还多挨打（hp 20），
        # 因为规则里资源"收集后移除且本场不刷新"——巡逻找不到新资源，只会送人头。默认关闭。
        self.patrol_when_idle = patrol_when_idle
        # 开局模式（前 opening_ticks 个 Tick 生效）：
        #   "normal"    现状：任务优先（≤8 格）+ 就近贪心
        #   "nearest"   纯就近：开局先不管任务，谁近去谁
        #   "high_first" 抢高地：优先视野/记忆里价值最高的资源（20 分档）
        self.opening_mode = opening_mode
        self.opening_ticks = opening_ticks


class Agent:
    """一局一个实例。on_perception() 返回要发布的消息，重复/旧帧返回 None。"""

    def __init__(self, cfg: AgentConfig | None = None, client=None, logger=None,
                 trace_path=None):
        self.cfg = cfg or AgentConfig()
        self.client = client
        self._logger = logger
        self.trace_path = trace_path   # 决策轨迹（jsonl），复盘用；None 表示不记
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
        self.ai_confirmed = False      # 本场是否已经有过成功的 AI 调用（规则要求 ≥1）
        self._hint = None              # 最近一次可用的模型提示（含幻术/威胁区）
        self.effects_applied = 0       # 按模型提示改过几次动作（复盘用）
        self._last_hp = None           # 上一帧血量（判断"要不要为躲伤害分心"）
        self._last_ack = None          # 上一帧的动作回执（moved / error）
        self._buff_zone = None         # 模型给出的增益区域（"天下无双"堡垒等）
        self._buff_until = None        # 增益区域存在到哪个 Tick
        self.buff_moves = 0            # 为抢增益走了几步（复盘用）
        self.buff_attacks = 0          # 利用增益的 5×5 攻击次数
        self.reach_moves = 0           # 为完成任务点走了几步
        self.deaths = 0                # 本场阵亡次数（复盘与实验统计用）
        self.mission_moves = 0         # 为完成任务主动走了几步（复盘用）
        self.patrol_moves = 0          # 空闲巡逻走了几步（复盘用）
        self.opening_moves = 0         # 开局抢高地走了几步（实验用）
        self._patrol_index = 0         # 当前巡逻到第几个点
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
        last_action = me.get("last_action")
        if isinstance(last_action, dict):
            self._last_ack = {"moved": last_action.get("moved"),
                              "error": last_action.get("error")}
        if me.get("alive") is False and self.stats.get("was_alive", True):
            self.deaths += 1                 # 从活到死的那一帧记一次
        self.stats["was_alive"] = me.get("alive") is not False
        self._learn_from_last_action(last_action)
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
        self._trace(payload, me, data)
        return payload

    def _trace(self, payload, me, data):
        """每 Tick 一行 jsonl：位置、血量、分数、动作。真实场次出问题时靠它复盘。"""
        if not self.trace_path:
            return
        score = data.get("score") if isinstance(data.get("score"), dict) else {}
        record = {
            "tick": payload.get("tick"),
            "session_id": payload.get("session_id"),
            "pos": list(self.pos) if self.pos else None,
            "hp": me.get("hp"),
            "alive": me.get("alive"),
            "resource_value": score.get("resource_value"),
            "effective_damage": score.get("effective_damage"),
            "missions": score.get("mission_completed"),
            "mission_ids": score.get("mission_completed_ids"),
            "action": payload.get("action"),
            "direction": payload.get("direction"),
            "combat": self.cfg.combat_mode,
            "ai_confirmed": self.ai_confirmed,
            "effects": self.effects_applied,
            "mission_moves": self.mission_moves,
            "deaths": self.deaths,
            "ack": self._last_ack,
            "buff_moves": self.buff_moves,
            "buff_attacks": self.buff_attacks,
        }
        try:
            import json
            with open(self.trace_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._log("warn", f"轨迹写入失败：{exc}")

    def _start_session(self, session_id):
        self.session_id = session_id
        self.last_tick = -1
        self.pos = None
        self.reach_done = False
        self.strategy = None
        self.explored = set()
        self.known_obstacles = set()
        self.last_move = None
        self.ai_confirmed = False
        if self.client is not None:
            from rmtc_client import EventUnderstanding
            self.understanding = EventUnderstanding(self.client, logger=self._logger)
        self._log("info", f"新场次 {session_id}：清空上一场的定位、记忆与在途 AI 请求")

    def _ensure_strategy(self):
        if self.strategy is not None or not (self.width and self.height):
            return
        from contestant.strategy import SnakeStrategy
        if self.cfg.strategy == "snake":
            self.strategy = SnakeStrategy(self.width, self.height)
        else:
            self.strategy = GreedyBfsStrategy(self.width, self.height,
                                              detour=self.cfg.strategy_detour)

    # ---------- 决策 ----------
    def _decide(self, data, me):
        if self.strategy is None or self.pos is None:
            return {"action": "wait"}
        if me.get("alive") is False:
            return {"action": "wait"}      # 阵亡期间只能 wait

        event_text = data.get("event_text")
        mission_text = data.get("mission_text")
        if self.understanding is not None:
            # 本场"AI 资格"：只要客户端有过一次成功调用就算满足（规则 §10.2 / §11.3）。
            if self.client is not None and getattr(self.client, "ok_calls", 0) > 0:
                self.ai_confirmed = True
            if isinstance(event_text, str) and event_text.strip():
                self.understanding.offer(
                    self.session_id, self.last_tick, event_text,
                    mission_text=mission_text if isinstance(mission_text, str) else "",
                    position=self.pos, map_size=(self.width, self.height), seat=self.cfg.seat)
            elif (self.cfg.stage >= 2 and not self.ai_confirmed
                  and self.last_tick >= self.cfg.ai_heartbeat_tick):
                # 没有事件也要问一次：否则本场可能一次成功调用都没有 → 任务分直接 0。
                self.understanding.offer(
                    self.session_id, self.last_tick,
                    "（本场暂无事件公告）请说明：在资源争夺中，采集与对抗应该如何取舍。",
                    mission_text=mission_text if isinstance(mission_text, str) else "",
                    position=self.pos, map_size=(self.width, self.height), seat=self.cfg.seat,
                    event_key="__heartbeat__")

        hint = self.understanding.hint(self.session_id, self.last_tick) if self.understanding else None
        self._hint = hint
        if hint is not None:
            self._learn_buff(hint.get("parsed"))
        if hint is not None:
            # 先按"逐格清单"躲（世界分裂的十字线、火爆辣椒的整行整列都靠它），再退回矩形判断
            avoid = self._avoid_cells_move(hint.get("parsed"))
            if avoid:
                return self._move(avoid)
            escape = self._threat_escape(hint.get("parsed"))
            if escape:
                return self._move(escape)

        reach_target = self._reach_target()
        reach_pending = (reach_target is not None and not self.reach_done
                         and self.pos is not None and self.pos != reach_target)
        enemies = self._enemies(data)
        buff_move = self._buff_camp_move(enemies, data)
        if buff_move is not None:
            return buff_move
        hp = me.get("hp")
        hp = hp if isinstance(hp, (int, float)) else self.cfg.max_hp
        self._last_hp = hp
        if self.cfg.combat_mode == "finish":
            combat_allowed = True      # 出手门槛由"敌人血量"控制（见下）
        elif self.cfg.combat_mode == "resource_first":
            # 资源分（30 分）在 120 价值封顶；对抗分（15 分）要 500 有效伤害（规则 §10.2，v1.1 从 300 改为 500）。
            # 先吃够资源再开打，比一上来对枪划算（官方裁判实测：open 图 35 → 22.5）。
            score = data.get("score") if isinstance(data.get("score"), dict) else {}
            value = score.get("resource_value")
            value = value if isinstance(value, (int, float)) else 0
            combat_allowed = value >= self.cfg.resource_threshold
        else:
            combat_allowed = self.cfg.combat_mode != "never"

        if self.cfg.stage >= 2 and self.cfg.combat_mode != "never":
            # 保命优先：血少且敌人近，先防（这条不受任何策略影响）
            if hp <= self.cfg.low_hp and enemies:
                nearest = min(abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]) for e in enemies)
                if nearest <= 3:
                    return {"action": "defend"}
            # 任务点优先于"追人"：实测镜像局里 reach 任务被追击挤掉，白丢 5 分
            if reach_pending and self.last_tick >= self.cfg.reach_after_tick and hp > 40:
                direction = step_toward(self.pos, reach_target,
                                        self.strategy.blocked | self.known_obstacles,
                                        self.width, self.height)
                if direction:
                    self.reach_moves += 1
                    return self._move(direction)
            if combat_allowed:
                in_range = [e for e in enemies
                            if abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]) <= 2]
                if self.cfg.combat_mode == "finish":
                    in_range = [e for e in in_range
                                if isinstance(e[0].get("hp"), (int, float))
                                and e[0]["hp"] <= self.cfg.finish_hp]
                if in_range:
                    target = min(in_range,
                                 key=lambda e: abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]))
                    # 规则 §6.2：最近目标是出生保护时，它挡住后面的目标且自身伤害为 0 —— 打了纯浪费 Tick。
                    protected_target = target[0].get("protected") is True
                    direction = self._line_direction(target[1])
                    if direction and not me.get("protected") and not protected_target:
                        return {"action": "attack", "direction": direction}   # 直线 1~2 格才打得到
            if combat_allowed and self.cfg.combat_mode == "aggressive" and enemies:
                nearest = min(enemies, key=lambda e: abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]))
                distance = abs(nearest[1][0] - self.pos[0]) + abs(nearest[1][1] - self.pos[1])
                if 2 < distance <= self.cfg.aggressive_range and not me.get("protected"):
                    direction = step_toward(self.pos, nearest[1],
                                            self.strategy.blocked | self.known_obstacles,
                                            self.width, self.height)
                    if direction:
                        return self._move(direction)

        opening = self.last_tick < self.cfg.opening_ticks
        if not (opening and self.cfg.opening_mode == "nearest"):
            mission_move = self._mission_resource_move(data)
            if mission_move:
                return mission_move
        if opening and self.cfg.opening_mode == "high_first":
            target = self._best_value_target(data)
            if target is not None:
                direction = step_toward(self.pos, target,
                                        self.strategy.blocked | self.known_obstacles,
                                        self.width, self.height)
                if direction:
                    self.opening_moves += 1
                    return self._move(direction)

        if reach_pending and self.pos == reach_target:
            self.reach_done = True

        from contestant.sim import View
        resources = self._resources(data)
        view = View(self.pos, resources, self.known_obstacles, self.explored, set(),
                    set(), self.last_tick, self.cfg.vision_radius)
        self.strategy.observe(view)
        action = self.strategy.decide(view)
        if (action.get("action") == "wait" and self.cfg.stage >= 2
                and self.cfg.combat_mode == "hybrid" and not enemies
                and self.cfg.patrol_when_idle):
            patrol = self._patrol_direction()
            if patrol:
                self.patrol_moves += 1
                return self._move(patrol)

        if (action.get("action") == "wait" and self.cfg.stage >= 2
                and self.cfg.combat_mode == "hybrid" and enemies):
            # 资源已经抢完/看不见了：与其原地等到结束，不如去压制对手（可能抢到资源、也有伤害分）
            nearest = min(enemies, key=lambda e: abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]))
            direction = self._line_direction(nearest[1])
            if direction and not me.get("protected"):
                return {"action": "attack", "direction": direction}
            direction = step_toward(self.pos, nearest[1],
                                    self.strategy.blocked | self.known_obstacles,
                                    self.width, self.height)
            if direction:
                return self._move(direction)
        if action.get("action") not in ATTACK_ACTIONS:
            return {"action": "wait"}
        if action.get("action") == "move":
            if action.get("direction") not in DIRECTIONS:
                return {"action": "wait"}
            return self._move(action["direction"])
        return action

    def _mission_resource_move(self, data):
        """任务里的"至少 N 个某类资源"只值 5 分，但只要还没完成、又看得见/记得目标，就该先去。

        规则 §10.1：每个任务 ID 只结算一次，完成后不撤销；任务判定在战斗后，只为存活车辆检查。
        这里只挑"还没完成的那个资源任务"里最近的资源，避免为了 5 分把整局时间耗掉。
        """
        if self.strategy is None or self.pos is None or self.cfg.stage < 2:
            return None
        score = data.get("score") if isinstance(data.get("score"), dict) else {}
        completed = score.get("mission_completed_ids")
        completed = set(completed) if isinstance(completed, list) else set()

        wanted = None
        for mission in self.missions:
            if not isinstance(mission, dict):
                continue
            kind = mission.get("type")
            if kind not in ("high_resources", "normal_resources"):
                continue
            if mission.get("id") in completed:
                continue
            wanted = kind
            break
        if wanted is None:
            return None

        # 记忆 + 本帧可见（任务检查发生在 strategy.observe 之前，所以要合并当前可见资源）
        seen = dict(self.strategy.seen_resources)
        for pos, value in self._resources(data):
            seen[pos] = value

        candidates = []
        for (x, y), value in seen.items():
            is_high = value >= 20
            if wanted == "high_resources" and not is_high:
                continue
            if wanted == "normal_resources" and is_high:
                continue
            candidates.append((abs(x - self.pos[0]) + abs(y - self.pos[1]), (x, y)))
        if not candidates:
            return None
        distance, target = min(candidates)
        # 已经站在目标格上：交给正常策略（这一 Tick 就会采集）
        if target == self.pos or distance == 0:
            return None
        if self.cfg.mission_max_distance and distance > self.cfg.mission_max_distance:
            return None      # 太远：先按普通策略就近抢，别为一个任务跑半张图
        direction = step_toward(self.pos, target, self.strategy.blocked | self.known_obstacles,
                                self.width, self.height)
        if not direction:
            return None
        self.mission_moves += 1
        return self._move(direction)

    def _learn_buff(self, parsed):
        """记录模型给出的增益区域（"天下无双"堡垒这类：占领后攻击变成 5×5、单次 35 伤害）。

        规则 §8.5：连续停留 10 Tick 达标（练习配置是 4 Tick），增益在堡垒结束时统一失效。
        我们只需要"进去、别乱跑、有敌人就 5×5 打"。
        """
        if not isinstance(parsed, dict):
            return
        zone = parsed.get("buff_zone")
        until = parsed.get("buff_until_tick")
        if isinstance(zone, list) and len(zone) == 2:
            try:
                first, second = zone
                x1, y1 = int(first[0]), int(first[1])
                x2, y2 = int(second[0]), int(second[1])
                self._buff_zone = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
                self._buff_until = int(until) if isinstance(until, (int, float)) else None
            except (TypeError, ValueError, IndexError):
                return

    def _buff_active(self):
        if not self._buff_zone:
            return False
        if self._buff_until is not None and self.last_tick > self._buff_until:
            return False
        return True

    def _buff_camp_move(self, enemies, data):
        # 资源优先：附近看得见资源就别去蹲堡垒（资源 30 分 > 对抗 15 分）
        for pos, _value in self._resources(data):
            if abs(pos[0] - self.pos[0]) + abs(pos[1] - self.pos[1]) <= 4:
                return None
        # 伤害已经够拿满对抗分（v1.1：500）就不必再蹲
        score = data.get("score") if isinstance(data.get("score"), dict) else {}
        damage = score.get("effective_damage")
        if isinstance(damage, (int, float)) and damage >= 400:
            return None
        """在增益区内：有敌人且够得着（5×5）就打，没有就待在区里别乱跑。"""
        if not self._buff_active():
            return None
        x1, y1, x2, y2 = self._buff_zone
        inside = x1 <= self.pos[0] <= x2 and y1 <= self.pos[1] <= y2
        near = [(e, max(abs(e[1][0] - self.pos[0]), abs(e[1][1] - self.pos[1])))
                for e in enemies]
        near = [(e, dist) for e, dist in near if dist <= 2]      # 增益下攻击范围是 2 格（5×5）
        if inside and near:
            target = min(near, key=lambda item: item[1])[0]
            direction = self._line_direction(target[1]) or "right"   # 增益下方向不影响命中
            self.buff_attacks += 1
            return {"action": "attack", "direction": direction}
        if not inside:
            center = ((x1 + x2) // 2, (y1 + y2) // 2)
            direction = step_toward(self.pos, center,
                                    self.strategy.blocked | self.known_obstacles,
                                    self.width, self.height)
            if direction:
                self.buff_moves += 1
                return self._move(direction)
        if inside and not near:
            return {"action": "wait"}         # 待在堡垒里攒停留 Tick，别出去
        return None

    def _best_value_target(self, data, max_distance=8):
        """开局抢高地：视野 + 记忆里，选价值最高的资源（同价值取最近的）。"""
        if self.pos is None:
            return None
        seen = dict(self.strategy.seen_resources) if self.strategy else {}
        for pos, value in self._resources(data):
            seen[pos] = value
        best = None
        for (x, y), value in seen.items():
            distance = abs(x - self.pos[0]) + abs(y - self.pos[1])
            if distance == 0 or distance > max_distance:
                continue
            key = (-value, distance)
            if best is None or key < best[0]:
                best = (key, (x, y))
        return best[1] if best else None

    def _patrol_direction(self):
        """空闲时按巡逻点绕着走：中心 → 四个象限，走到就换下一个。"""
        if not self.width or not self.height:
            return None
        cx, cy = self.width // 2, self.height // 2
        points = [(cx, cy), (2, 2), (self.width - 3, 2),
                  (2, self.height - 3), (self.width - 3, self.height - 3)]
        target = points[self._patrol_index % len(points)]
        if self.pos == target:
            self._patrol_index += 1
            target = points[self._patrol_index % len(points)]
        direction = step_toward(self.pos, target,
                                self.strategy.blocked | self.known_obstacles,
                                self.width, self.height)
        return direction

    def _avoid_cells_move(self, parsed):
        """模型给"要避开的格子"时，如果本车正好在里面，就走到最近的格子外。

        世界分裂（整行整列即时击杀）、火爆辣椒（整行整列火焰）用矩形描述不准确，
        所以让模型直接列格子，这里只做"我在不在里面 / 往哪走一步"两件事。
        """
        if not isinstance(parsed, dict) or self.pos is None:
            return None
        cells = parsed.get("avoid_cells")
        if not isinstance(cells, list) or not cells:
            return None
        until = parsed.get("avoid_until_tick")
        if isinstance(until, (int, float)) and self.last_tick > int(until):
            return None
        # 危险等级：fatal 必躲；damage 只在血少时躲；none/缺失不躲。
        # 实测教训：为了躲 30 HP 的辣椒线反复横跳，会被对手抢光资源（一局从 45 掉到 32.5）。
        level = parsed.get("danger_level")
        hp = self._last_hp
        short_hazard = (isinstance(until, (int, float))
                        and 0 <= int(until) - self.last_tick <= 25)   # 短时危险（≤25 Tick）
        if level == "fatal":
            pass
        elif level == "damage" and (not isinstance(hp, (int, float)) or hp <= 60
                                    or short_hazard):
            pass
        else:
            return None
        blocked = set()
        for cell in cells[:80]:
            if isinstance(cell, (list, tuple)) and len(cell) == 2:
                try:
                    x, y = int(cell[0]), int(cell[1])
                except (TypeError, ValueError):
                    continue
                if 0 <= x < max(1, self.width) and 0 <= y < max(1, self.height):
                    blocked.add((x, y))
        if self.pos not in blocked:
            return None
        target = self._nearest_safe_cell(blocked)
        if target is None:
            return None
        direction = step_toward(self.pos, target,
                                self.strategy.blocked | self.known_obstacles,
                                self.width, self.height)
        if direction:
            self.effects_applied += 1
        return direction

    def _nearest_safe_cell(self, blocked):
        """BFS 找最近的、不在危险清单里的可走格（只用于下一步方向）。"""
        from collections import deque
        queue = deque([self.pos])
        seen = {self.pos}
        while queue:
            cell = queue.popleft()
            if cell != self.pos and cell not in blocked:
                return cell
            for dx, dy in DIRECTIONS.values():
                nxt = (cell[0] + dx, cell[1] + dy)
                if not (0 <= nxt[0] < max(1, self.width) and 0 <= nxt[1] < max(1, self.height)):
                    continue
                if nxt in seen or nxt in self.known_obstacles:
                    continue
                seen.add(nxt)
                queue.append(nxt)
        return None

    def _threat_escape(self, parsed):
        """LLM 给的威胁矩形：如果本车在里面，就朝最近的矩形外走一步。

        规则允许"校验模型输出并用于自主规划"；这里只用一个矩形字段，其余字段不信任。
        """
        if not isinstance(parsed, dict) or self.pos is None:
            return None
        zone = parsed.get("threat_zone")
        if not isinstance(zone, list):
            zone = parsed.get("death_zone")
        if isinstance(zone, list) and len(zone) == 2:
            try:
                first, second = zone
                x1, y1 = int(first[0]), int(first[1])
                x2, y2 = int(second[0]), int(second[1])
            except (TypeError, ValueError, IndexError):
                return None
        elif isinstance(zone, list) and len(zone) == 4:
            try:
                x1, y1, x2, y2 = (int(v) for v in zone)
            except (TypeError, ValueError):
                return None
        else:
            return None
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)
        x, y = self.pos
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            return None
        dx = min(x - x1, x2 - x) 
        dy = min(y - y1, y2 - y)
        candidates = []
        if dx <= dy:
            candidates.append((x1 - 1, y) if x - x1 <= x2 - x else (x2 + 1, y))
            candidates.append((x, y1 - 1) if y - y1 <= y2 - y else (x, y2 + 1))
        else:
            candidates.append((x, y1 - 1) if y - y1 <= y2 - y else (x, y2 + 1))
            candidates.append((x1 - 1, y) if x - x1 <= x2 - x else (x2 + 1, y))
        for target in candidates:
            if not (0 <= target[0] < self.width and 0 <= target[1] < self.height):
                continue
            direction = step_toward(self.pos, target,
                                    self.strategy.blocked | self.known_obstacles,
                                    self.width, self.height)
            if direction:
                return direction
        return None

    # ---------- 记忆维护 ----------
    # 规则 §8.3「幻术」：MOVE 的 up/down、left/right 互换，ATTACK 的方向不反转。
    # 这个效果只能靠 LLM 理解公告得出（禁止关键词解析），所以这里消费模型输出。
    _OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}

    def _move(self, direction):
        """记下"这一步从哪走、往哪走"，下一帧用它解释 E_BLOCKED。"""
        direction = self._apply_reversal(direction)
        self.last_move = (self.pos, direction)
        return {"action": "move", "direction": direction}

    def _apply_reversal(self, direction):
        hint = getattr(self, "_hint", None)
        if not hint:
            return direction
        parsed = hint.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("direction_reversed") is not True:
            return direction
        until = parsed.get("reversed_until_tick")
        if isinstance(until, (int, float)) and self.last_tick > int(until):
            return direction          # 已过期
        reversed_direction = self._OPPOSITE.get(direction, direction)
        if reversed_direction != direction:
            self.effects_applied += 1
        return reversed_direction

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
