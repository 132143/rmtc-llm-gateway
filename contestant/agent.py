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

import time

from contestant import tactics
from contestant.planner import Planner, WorldView
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
                 patrol_when_idle=False, opening_mode="normal", opening_ticks=30,
                 planner="guarded", decision_type="balanced", engage_th=None,
                 pursue_r=None, ambush_on=None, param_overrides=None,
                 memory_falsify=None):
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
        # 决策内核（封存默认 guarded，见 docs/FUSION_REPORT.md 的对照实验结论）：
        #   legacy  = 原版"最近资源 + 进射程就打"（对照基线，保留用来复现 A/B）
        #   guarded = 原版决策路径不动 + 三项已验证增量：资源记忆证伪、对抗分封顶感知、
        #             战争期不再空等（batchB：36.60 vs 33.69，前二率 0.625 vs 0.531）
        #   fused   = 收益表内核（tactics/planner 全量收益打分；batchB 35.29，未超过 guarded）
        # 三者都不动 IO、调度、状态闸门这些已稳定的模块。
        self.planner = planner
        # decision_type 决定参数增量（02 §3），现场只能改这个字符串，不能改数字。
        self.decision_type = decision_type
        self.params = tactics.params_for(decision_type)
        overrides = {}
        if isinstance(engage_th, (int, float)):
            overrides["engage_th"] = float(engage_th)
        if isinstance(pursue_r, (int, float)):
            overrides["pursue_r"] = int(pursue_r)
        if isinstance(ambush_on, bool):
            overrides["ambush_on"] = ambush_on
        if isinstance(param_overrides, dict):
            overrides.update(param_overrides)      # 对照实验用；封存版本不传这个参数
        # 资源记忆"证伪"（看过一眼就不再追幻影）。默认跟随内核：
        # legacy 保持原版行为（原版就是靠"记住很久以前的资源"持续移动的），fused/guarded 打开。
        if memory_falsify is None:
            memory_falsify = planner in ("fused", "guarded")
        self.memory_falsify = bool(memory_falsify)
        if overrides:
            self.params = self.params.with_deltas(overrides)


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
        self.known_obstacles = set()   # 见过的障碍（按"最后看到"计时，见 obstacle_seen）
        # 障碍记忆：位置 -> 最后看到它的 Tick。
        # 事件会生成**临时**墙（世界分裂的十字线只存在 30 Tick），把它当永久障碍会让
        # 寻路绕一整片空地（实测撞墙 19 → 451）。所以障碍也做"证伪/过期"，与资源记忆同源。
        self.obstacle_seen = {}
        self.boundary_obstacles = set()   # E_OOB 推出来的地图边界：永久有效
        self.transient_blocked = {}    # 临时被堵的格子 -> 过期 Tick（别的车占位会走开）
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
        self.reach_clear_attacks = 0   # 为清开任务点占位者而攻击的次数
        self.reach_blocked = 0         # 连续撞任务点的次数
        self.reach_cooldown_until = -1 # 撞不开就先去做别的，过一会儿再来
        self._last_defend_tick = -99   # 上次防御的 Tick（防止连续防御把自己锁死）
        self.first_tick = None         # 本场第一条感知的 tick
        self.late_join = False         # 开局晚于 tick 5：开局资源大概率已被抢光
        self.resources_ever_seen = 0   # 本场累计见过多少个资源（判断"是不是真没资源了"）
        self.deaths = 0                # 本场阵亡次数（复盘与实验统计用）
        self.mission_moves = 0         # 为完成任务主动走了几步（复盘用）
        self.patrol_moves = 0          # 空闲巡逻走了几步（复盘用）
        self.opening_moves = 0         # 开局抢高地走了几步（实验用）
        self._patrol_index = 0         # 当前巡逻到第几个点
        self.stats = {"perceptions": 0, "actions": 0, "skipped": 0}
        # ---- fused 内核（收益表决策）的运行状态；planner="legacy" 时全程不参与 ----
        self._planner = None
        self.enemy_memory = []          # [{"pos","tick","dir","entity"}] 视野外敌车用最后已知位置估值
        self.resource_memory = {}       # 资源格 -> 最后确认 tick（记忆衰减 P-R-04）
        self.dry_ticks = 0              # 连续多少 Tick 没有新发现资源（P-T-02）
        self._new_resource_cells = set()
        self.war_phase = False          # 资源见底判定（P-T-01/§9.1）
        self.vision_seen = None         # 感知里报告的 vision_radius（协议 §4.1：0–4）
        self.fused_actions = 0
        self.fused_reasons = {}
        self.plan_ms_max = 0.0
        # 模型给出的"高层意图"（规则 §11.1 允许 AI 做高层决策、本地执行）。
        # 只按白名单 + 置信度阈值采信，并且**限时**（≤40 Tick），过期自动失效。
        self.llm_intent = "none"
        self.llm_intent_until = -1
        self.llm_intent_log = []

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
        if self.first_tick is None:
            self.first_tick = tick
            # 晚 join（实测晚 26 Tick 就会丢掉全部开局资源）：改为"抢不到就吃任务+对抗"
            self.late_join = tick > 5
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

        vision = data.get("vision_radius")
        if isinstance(vision, int) and not isinstance(vision, bool) and 0 <= vision <= 8:
            self.vision_seen = vision          # 协议 §4.1：视野半径由感知给出，不能写死 4
        self._mark_explored()
        self._update_memory(data, me)
        for entity in data.get("visible_entities") or []:
            if isinstance(entity, dict) and entity.get("type") == "obstacle":
                relative = decode_relative(entity.get("position"))
                if relative is not None and self.pos is not None:
                    cell = (self.pos[0] + relative[0], self.pos[1] + relative[1])
                    self.known_obstacles.add(cell)
                    self.obstacle_seen[cell] = self.last_tick

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
        self.obstacle_seen = {}
        self.boundary_obstacles = set()
        self.last_move = None
        self.ai_confirmed = False
        self._planner = None
        self.enemy_memory = []
        self.resource_memory = {}
        self.dry_ticks = 0
        self._new_resource_cells = set()
        self.war_phase = False
        self.vision_seen = None
        self.fused_actions = 0
        self.fused_reasons = {}
        self.llm_intent = "none"
        self.llm_intent_until = -1
        self.llm_intent_log = []
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

    def _damage_worth_attacking(self, data):
        """这一 Tick 要不要为攻击放弃采集。

        §10.2：对抗分 D=500 封顶、资源分 R2=120 封顶。所以：
          - D < 400：白白让路，照打（实测封顶感知单开会掉分，就是因为太早收手）；
          - D ≥ 400：只有"本 Tick 真有个资源在够得着的范围内"才让路，否则继续打。
        关键是"够得着"而不是"理论上还有资源"——否则 dense24 上 D 会卡在 400 出头，
        白丢 2~3 分（batchB 里 guarded 在 dense24 上 44.5 < 原版 45.6 就是这个原因）。
        """
        if self._llm_intent_active("flee"):
            return False                     # 模型判断"该脱离接触"：这一 Tick 不为攻击让路
        if self._llm_intent_active("engage"):
            return True                      # 模型判断"该打"：即使 D 已近封顶也不再让路
        if self.cfg.planner not in ("fused", "guarded"):
            return True
        score = data.get("score") if isinstance(data.get("score"), dict) else {}
        damage = score.get("effective_damage")
        damage = damage if isinstance(damage, (int, float)) else 0
        if tactics.damage_headroom(damage, self.cfg.params) > 0.2:
            return True                      # D < 400：还没近封顶，照打
        return self._reachable_resource_distance(data) is None

    def _reachable_resource_distance(self, data, radius=4):
        """最近的可采集资源步数（视野 + 记忆），超过 radius 视为"够不着"。"""
        if self.pos is None:
            return None
        candidates = list(self._resources(data))
        if self.strategy is not None:
            candidates.extend(self.strategy.seen_resources.items())
        best = None
        for pos, _value in candidates:
            distance = abs(pos[0] - self.pos[0]) + abs(pos[1] - self.pos[1])
            if distance == 0 or distance > radius:
                continue
            if best is None or distance < best:
                best = distance
        return best

    # ---------- fused 内核：记忆维护与收益表调用 ----------
    def _update_memory(self, data, me):
        """维护"敌车最后已知位置 / 资源最后确认 tick / 新发现计数 / 战争期判定"。

        依据：03 §4.2（视野外敌车用最后已知位置估距离）、§4.3（记忆衰减）、
              §9.1（资源见底判据只数"新发现"，不需要全图）。
        """
        now = self.last_tick
        forget = self.cfg.params.enemy_mem_forget
        pool = list(self.enemy_memory)
        kept = []
        for entity, pos in self._enemies(data):
            last = entity.get("last_action") if isinstance(entity, dict) else None
            direction = last.get("effective_direction") if isinstance(last, dict) else None
            for index, old in enumerate(pool):
                if abs(old["pos"][0] - pos[0]) + abs(old["pos"][1] - pos[1]) <= 2:
                    pool.pop(index)          # 同一台车的轨迹：用最新位置覆盖旧记录
                    break
            kept.append({"pos": tuple(pos), "tick": now,
                         "dir": direction if direction in DIRECTIONS else None,
                         "entity": entity if isinstance(entity, dict) else {}})
        for old in pool:
            if now - int(old.get("tick", now)) <= forget:
                kept.append(old)
        self.enemy_memory = kept

        visible = self._resources(data)
        fresh = 0
        for pos, _value in visible:
            if pos not in self._new_resource_cells:
                self._new_resource_cells.add(pos)
                fresh += 1
            self.resource_memory[pos] = now
        # 判据（P-T-02 的稳健化）：视野里还有资源就说明"没见底"，不累计干旱。
        # 原文档口径是"没有新发现的资源"，但那会把"站在同一堆资源旁边"误判成见底。
        self.dry_ticks = 0 if (fresh or visible) else self.dry_ticks + 1
        if len(self.obstacle_seen) > 4000:      # 兜底：长期局也不会让记忆无限增长
            keep = self.OBSTACLE_TTL * 4
            self.obstacle_seen = {cell: seen for cell, seen in self.obstacle_seen.items()
                                  if now - int(seen) <= keep}

        # 资源记忆的"证伪"：视野覆盖到的格子如果已经不显示资源，说明被别人拿走了。
        # 原版只按"自己采到"删记忆，于是会一直朝一个早就不存在的资源跑（实测抢完资源后
        # 仍在追幻影，整局因此没进入"该打人"的阶段）。规则允许用合法感知做这种推断。
        radius = self.vision_seen if isinstance(self.vision_seen, int) else self.cfg.vision_radius
        visible_cells = {pos for pos, _value in visible}
        if self.cfg.memory_falsify:
            for pos in list(self.resource_memory):
                if pos in visible_cells:
                    continue
                if max(abs(pos[0] - self.pos[0]), abs(pos[1] - self.pos[1])) <= radius:
                    self.resource_memory.pop(pos, None)
                    if self.strategy is not None:
                        self.strategy.seen_resources.pop(pos, None)

        limit = self.cfg.params.mem_forget
        self.resource_memory = {pos: tick for pos, tick in self.resource_memory.items()
                                if now - tick <= limit}
        # 记忆与策略层保持同步：决策可能由 fused 内核做出，那时代码不会再走到
        # strategy.observe()；不同步的话，任务判断与"无资源"判断会永远看到空记忆。
        # 注意：只在开启"证伪"时同步，否则会破坏原版路径上 strategy 自己累积的记忆
        # （原版允许记住很久以前见过的资源，那是它"一直在动"的来源）。
        #   · 只把本帧可见的资源补进去；删除只发生在"证伪"命中时（下面那段），
        #     不按 resource_memory 做全量对齐——那会把策略层更早的记忆一起抹掉。
        if self.strategy is not None:
            for pos, value in visible:
                self.strategy.seen_resources[pos] = value
            self.strategy.seen_resources.pop(tuple(self.pos), None)
        self.resources_ever_seen += fresh

        score = data.get("score") if isinstance(data.get("score"), dict) else {}
        value = score.get("resource_value")
        value = value if isinstance(value, (int, float)) else 0
        # 额外两个闸门，防止"探索得差"被误判成"资源见底"：
        #   1) 本场必须真的见过资源（否则只是还没找到，不是没了）；
        #   2) 当前视野里确实一个资源都没有。
        self.war_phase = (tactics.is_war_phase(now, self.dry_ticks, self.cfg.params, value)
                          and self.resources_ever_seen > 0 and not visible)

    # 白名单与时限：模型只能说这几个词，且判断只在有限时间内有效。
    LLM_INTENTS = ("resource", "mission", "engage", "flee", "none")
    LLM_INTENT_TTL = 40          # Tick；超过就自动失效，防止一条旧判断绑架整局
    LLM_INTENT_MIN_CONF = 0.60   # P-L-11 置信度阈值

    def _apply_llm_intent(self, parsed):
        """把模型的高层意图过校验后落地（本地只负责合法性与时限）。

        规则 §11.1：允许 AI 做高层决策、由本地程序执行；程序须自行校验模型输出。
        所以这里做四件事：白名单 → 置信度阈值 → 时限 → 只影响"偏好"，不影响硬约束
        （致死区、状态闸门、争格仍然优先于它）。
        """
        if not isinstance(parsed, dict):
            return
        kind = parsed.get("target_kind")
        if kind not in self.LLM_INTENTS or kind == "none":
            return
        confidence = parsed.get("target_confidence")
        confidence = confidence if isinstance(confidence, (int, float)) else 0.0
        if float(confidence) < self.LLM_INTENT_MIN_CONF:
            self._log("info", "模型意图 {0} 置信度 {1:.2f} 低于阈值，忽略".format(kind, float(confidence)))
            return
        self.llm_intent = kind
        self.llm_intent_until = self.last_tick + self.LLM_INTENT_TTL
        # 只记"意图变化"，否则同一条公告在新鲜期内每 Tick 都会写一行日志
        if not self.llm_intent_log or self.llm_intent_log[-1][1] != kind:
            self.llm_intent_log.append((self.last_tick, kind, round(float(confidence), 2)))

    def _llm_intent_active(self, intent):
        return self.llm_intent == intent and self.last_tick <= self.llm_intent_until

    def _danger_cells(self, parsed):
        """把模型输出的 avoid_cells 校验成合法格子集合（H1 用）。

        危险等级的处理与 _avoid_cells_move 完全一致：fatal 必躲，damage 只在血少或短时危险时躲。
        满血为躲辣椒线反复横跳会丢资源（实测 45 → 32.5），所以这层抑制是必需的。
        """
        if not isinstance(parsed, dict):
            return set()
        cells = parsed.get("avoid_cells")
        if not isinstance(cells, list) or not cells:
            return set()
        until = parsed.get("avoid_until_tick")
        if isinstance(until, (int, float)) and self.last_tick > int(until):
            return set()
        level = parsed.get("danger_level")
        hp = self._last_hp
        short = isinstance(until, (int, float)) and 0 <= int(until) - self.last_tick <= 25
        if level != "fatal" and not (level == "damage"
                                     and (not isinstance(hp, (int, float)) or hp <= 60 or short)):
            return set()
        out = set()
        for cell in cells[:80]:
            if not (isinstance(cell, (list, tuple)) and len(cell) == 2):
                continue
            try:
                x, y = int(cell[0]), int(cell[1])
            except (TypeError, ValueError):
                continue
            if 0 <= x < max(1, self.width) and 0 <= y < max(1, self.height):
                out.add((x, y))
        return out

    def _heal_points(self, parsed):
        """模型给出的治疗点矩形 → 一个点位（P-H）。只认矩形中心，不做语义猜测。"""
        if not isinstance(parsed, dict):
            return []
        zone = parsed.get("heal_zone")
        if not (isinstance(zone, list) and len(zone) == 2):
            return []
        try:
            x1, y1 = int(zone[0][0]), int(zone[0][1])
            x2, y2 = int(zone[1][0]), int(zone[1][1])
        except (TypeError, ValueError, IndexError):
            return []
        point = ((min(x1, x2) + max(x1, x2)) // 2, (min(y1, y2) + max(y1, y2)) // 2)
        if not (0 <= point[0] < max(1, self.width) and 0 <= point[1] < max(1, self.height)):
            return []
        return [(point, None)]

    def _build_world(self, data, me, enemies, hp, combat_allowed):
        """把一帧感知整理成 planner 的输入（只做整理与校验，不做决策）。"""
        params = self.cfg.params
        resources = []
        if self.strategy is not None:
            for pos, value in self.strategy.seen_resources.items():
                pos = tuple(pos)
                seen = int(self.resource_memory.get(pos, self.last_tick))
                if self.last_tick - seen <= params.mem_forget:
                    resources.append((pos, value, seen))
        for pos, value in self._resources(data):
            for index, (known_pos, _value, _seen) in enumerate(resources):
                if known_pos == pos:
                    resources[index] = (pos, value, self.last_tick)
                    break
            else:
                resources.append((pos, value, self.last_tick))

        score = data.get("score") if isinstance(data.get("score"), dict) else {}
        value = score.get("resource_value")
        damage = score.get("effective_damage")
        completed = score.get("mission_completed_ids")
        hint = self._hint.get("parsed") if isinstance(self._hint, dict) else None
        danger_level = "none"
        if isinstance(hint, dict):
            if hint.get("no_attack_defender") is True:
                danger_level = "self_defense"          # H4：洁身自好期间不攻打防御目标
            elif isinstance(hint.get("danger_level"), str):
                danger_level = hint["danger_level"]
        return WorldView(
            pos=self.pos, tick=self.last_tick, hp=hp,
            width=self.width, height=self.height,
            alive=me.get("alive") is not False, protected=me.get("protected") is True,
            defending=me.get("defending") is True,
            blocked=self._blocked_now(),
            resources=resources,
            enemies=[(entry.get("entity") or {}, entry["pos"], entry["tick"], entry.get("dir"))
                     for entry in self.enemy_memory],
            reach_target=self._reach_target(), reach_done=self.reach_done,
            avoid_cells=self._danger_cells(hint), danger_level=danger_level,
            heal_points=self._heal_points(hint),
            fort_zone=self._buff_zone if self._buff_active() else None,
            fort_until=self._buff_until,
            dry_ticks=self.dry_ticks,
            resource_value=value if isinstance(value, (int, float)) else 0,
            damage=damage if isinstance(damage, (int, float)) else 0,
            missions_completed=completed if isinstance(completed, list) else [],
            combat_allowed=combat_allowed, params=params, deaths=self.deaths,
            war_phase=self.war_phase)

    def _fused_action(self, data, me, enemies, hp, combat_allowed):
        """收益表内核。返回动作 dict，或 None（交回原路径，绝不返回非法动作）。"""
        if self._planner is None:
            self._planner = Planner(self.cfg.params)
        self._planner.params = self.cfg.params
        target = self._reach_target()
        if target is not None and self.pos == target:
            self.reach_done = True          # 站到任务点就算完成（规则 §10.1：到达即判定）
        view = self._build_world(data, me, enemies, hp, combat_allowed)
        started = time.perf_counter()
        try:
            intent = self._planner.plan(view)
        except Exception as exc:                     # 规则要求：任何异常都不能让进程退出
            self._log("error", f"决策内核异常（本 Tick 回落原路径）：{exc}")
            self._planner = Planner(self.cfg.params)
            return None
        elapsed = (time.perf_counter() - started) * 1000.0
        if elapsed > self.plan_ms_max:
            self.plan_ms_max = elapsed
        if not isinstance(intent, dict):
            return None
        action = intent.get("action")
        reason = str(intent.get("reason") or "")
        key = reason.split(":")[0] or "unknown"
        self.fused_reasons[key] = self.fused_reasons.get(key, 0) + 1
        if action == "attack":
            direction = intent.get("direction")
            if direction not in DIRECTIONS or me.get("protected"):
                return None
            self.fused_actions += 1
            return {"action": "attack", "direction": direction}
        if action == "move":
            direction = intent.get("direction")
            if direction not in DIRECTIONS:
                return None
            self.fused_actions += 1
            return self._move(direction)             # H5：幻术反转只在单一移动出口做一次
        return None

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
                    # 这段文案必须含 "json"：真机实测 DeepSeek 在 response_format=json_object
                    # 下要求 prompt 里出现该词，否则 400 —— 那会把 AI 资格（15 分）直接打掉。
                    "（本场暂无事件公告）请用一个严格 JSON 对象回答："
                    "在资源争夺中，采集与对抗应该如何取舍。",
                    mission_text=mission_text if isinstance(mission_text, str) else "",
                    position=self.pos, map_size=(self.width, self.height), seat=self.cfg.seat,
                    event_key="__heartbeat__")

        hint = self.understanding.hint(self.session_id, self.last_tick) if self.understanding else None
        self._hint = hint
        if hint is not None:
            self._learn_buff(hint.get("parsed"))
            self._apply_llm_intent(hint.get("parsed"))
        if hint is not None:
            # 先按"逐格清单"躲（世界分裂的十字线、火爆辣椒的整行整列都靠它），再退回矩形判断
            avoid = self._avoid_cells_move(hint.get("parsed"))
            if avoid:
                return self._move(avoid)
            escape = self._threat_escape(hint.get("parsed"))
            if escape:
                return self._move(escape)

        reach_target = self._reach_target()
        if reach_target is not None and self.pos is not None and self.pos == reach_target:
            # 站到任务点就算完成（§10.1 到达即判定）。原版把这行写在函数末尾，
            # 但守卫/收益表内核会在它之前 return，于是"到了还不算完成"，在目标格与相邻格
            # 之间来回抖（实测 494-499 Tick 在一个格子上反复上下）。
            self.reach_done = True
        reach_pending = (reach_target is not None and not self.reach_done
                         and self.pos is not None and self.pos != reach_target)
        enemies = self._enemies(data)
        # fused 内核自己给堡垒打分（P-F）；legacy 保留原有的"蹲堡垒"专用模块。
        buff_move = None if self.cfg.planner == "fused" else self._buff_camp_move(enemies, data)
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

        # fused 内核即使 combat_mode=never 也要接管资源挑选（收益表就是这样定义的）；
        # legacy 路径下 never 保持原状——不进入战斗分支，也就不会防御。
        if self.cfg.stage >= 2 and (self.cfg.combat_mode != "never"
                                    or self.cfg.planner == "fused"):
            # 保命：血少且敌人近时防御，但**最多隔一帧防一次**。
            # 真机踩过：血量长期停在 20 时，程序连续 251 个 Tick 都在防御，
            # 既没去任务点也没输出伤害（镜像局因此丢 5 分任务）。防御只是减伤，不是免伤。
            if hp <= self.cfg.low_hp and enemies:
                nearest = min(abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]) for e in enemies)
                if nearest <= 3 and (self.last_tick - self._last_defend_tick) >= 2:
                    self._last_defend_tick = self.last_tick
                    return {"action": "defend"}
            # 任务点优先于"追人"：实测镜像局里 reach 任务被追击挤掉，白丢 5 分
            # 只有"确实见过资源、现在一个都没有了"才算没得捡；
            # 开局本来就没记忆，不能据此提前放弃搜索（实测会让分数掉到 3 分）。
            nothing_to_collect = (self._is_large_map() and self.last_tick >= 150
                                  and self.resources_ever_seen > 0
                                  and not self.strategy.seen_resources
                                  and not self._resources(data))
            # 注意：晚开局**不**提前冲任务点——四席环境里晚 5~30 Tick 是常态，
            # 提前改去任务点会让整局抢不到资源（实测 rv=0、只剩 1 个任务）。
            go_reach_now = (self.last_tick >= self.cfg.reach_after_tick or nothing_to_collect
                            or self._llm_intent_active("mission"))
            if self._is_large_map() and self.last_tick < self.reach_cooldown_until:
                pass            # 大图：刚撞不开，先按普通策略走（抢资源/打人），过会儿再来
            elif reach_pending and go_reach_now and hp > 15:
                direction = step_toward(self.pos, reach_target,
                                        self._blocked_now(),
                                        self.width, self.height)
                # 目标格被别的车占着（实测卡在相邻格 90+ 帧、E_BLOCKED 50+ 次）：
                # 阵亡的车不占格，所以占位者是活的——同排同列时先把它打掉再进。
                if direction and self._enemy_at(direction, enemies) and not me.get("protected"):
                    self.reach_clear_attacks += 1
                    return {"action": "attack", "direction": direction}
                if direction and self._last_ack and self._last_ack.get("error") == "E_BLOCKED":
                    self.reach_blocked += 1
                    if self.reach_blocked >= 4:
                        # 反复撞不开（占位者可能是斜向/打不到）：先去做别的 15 Tick
                        self.reach_cooldown_until = self.last_tick + 15
                        self.reach_blocked = 0
                if direction and self.last_tick >= self.reach_cooldown_until:
                    self.reach_moves += 1
                    return self._move(direction)
            if combat_allowed and self.cfg.combat_mode != "evaluate":
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
                    if (direction and not me.get("protected") and not protected_target
                            and self._damage_worth_attacking(data)):
                        return {"action": "attack", "direction": direction}   # 直线 1~2 格才打得到
            if self.cfg.planner == "fused":
                # 收益表内核：资源/任务/治疗/堡垒/战斗统一打分后取 argmax。
                # 位置说明：排在防御（H3）、任务点（H6）、进射程就打之后——
                # 前三条是已经用真机数据验证过的稳定模块，这里只接管"选哪个经济目标"。
                fused = self._fused_action(data, me, enemies, hp, combat_allowed)
                if fused is not None:
                    return fused
            if combat_allowed and self.cfg.combat_mode == "aggressive" and enemies:
                nearest = min(enemies, key=lambda e: abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]))
                distance = abs(nearest[1][0] - self.pos[0]) + abs(nearest[1][1] - self.pos[1])
                if 2 < distance <= self.cfg.aggressive_range and not me.get("protected"):
                    direction = step_toward(self.pos, nearest[1],
                                            self._blocked_now(),
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
                                        self._blocked_now(),
                                        self.width, self.height)
                if direction:
                    self.opening_moves += 1
                    return self._move(direction)

        if reach_pending and self.pos == reach_target:
            self.reach_done = True

        from contestant.sim import View
        resources = self._resources(data)
        view = View(self.pos, resources, self._blocked_now(), self.explored, set(),
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
                and self.cfg.planner == "guarded" and self.war_phase):
            # 战争期原地 WAIT = 白扔 Tick。原版在"看不见敌人"时就是这么等的（实测最多 271/500）。
            # 这里只加一条：没敌人就往热区走一步，其余决策完全走原路径。
            hot = self._war_hotspot()
            direction = step_toward(self.pos, hot, self._blocked_now(), self.width, self.height)
            if direction:
                # 巡逻也要避开"本 Tick 必被占"的格子：小图上盲目走会撞出 E_BLOCKED
                # （实测 practice18 上 patrol 把撞墙数抬到原版的 4 倍）。
                direction = self._contention_detour(direction)
                self.patrol_moves += 1
                return self._move(direction)
        if (action.get("action") == "wait" and self.cfg.stage >= 2
                and self.cfg.combat_mode == "hybrid" and enemies):
            # 资源已经抢完/看不见了：与其原地等到结束，不如去压制对手（可能抢到资源、也有伤害分）
            nearest = min(enemies, key=lambda e: abs(e[1][0] - self.pos[0]) + abs(e[1][1] - self.pos[1]))
            direction = self._line_direction(nearest[1])
            if direction and not me.get("protected"):
                return {"action": "attack", "direction": direction}
            direction = step_toward(self.pos, nearest[1],
                                    self._blocked_now(),
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
        limit = self.cfg.mission_max_distance
        if limit and distance > limit:
            return None      # 太远：先按普通策略就近抢，别为一个任务跑半张图
        direction = step_toward(self.pos, target, self._blocked_now(),
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
                                    self._blocked_now(),
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

    def _war_hotspot(self):
        """战争期巡逻点：中心 + 四角内缩，按 Tick 轮换（确定性，不用随机数）。"""
        points = ((self.width // 2, self.height // 2), (2, 2),
                  (self.width - 3, 2), (2, self.height - 3),
                  (self.width - 3, self.height - 3))
        return points[(self.last_tick // 40) % len(points)]

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
                                self._blocked_now(),
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
                                self._blocked_now(),
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
                if nxt in seen or nxt in self._blocked_now():
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
                                    self._blocked_now(),
                                    self.width, self.height)
            if direction:
                return direction
        return None

    # ---------- 记忆维护 ----------
    # 规则 §8.3「幻术」：MOVE 的 up/down、left/right 互换，ATTACK 的方向不反转。
    # 这个效果只能靠 LLM 理解公告得出（禁止关键词解析），所以这里消费模型输出。
    _OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}

    def _move(self, direction):
        """记下"这一步从哪走、往哪走"，下一帧用它解释 E_BLOCKED。

        H5：幻术反转在这里做且只做一次（ATTACK 的方向不反转，所以不在动作出口统一处理）。
        H2：融合版在这里再做一次"争格避让"——目标格若被敌车这一 Tick 占着/正在被抢，
             规则里它们一起失败（§5.2 多人争格全部失败），所以先换成侧向的可用方向。
        """
        direction = self._apply_reversal(direction)
        # 争格避让只在 fused 内核里做（那里知道目标格，能保证"换方向仍在靠近目标"）。
        # 在动作出口盲换方向实测是负收益（batchB3：39.02 < 39.97），所以 guarded 不用它。
        if self.cfg.planner == "fused" and self.cfg.params.use_contention:
            direction = self._contention_detour(direction)
        self.last_move = (self.pos, direction)
        return {"action": "move", "direction": direction}

    def _contention_detour(self, direction):
        """目标格被判为"这一 Tick 必被占"时改走侧向；没有更好的方向就保持原样（绝不原地卡死）。

        预测来源只有两样合法信息：视野内的敌车位置 + 上一帧回执里的 effective_direction
        （协议 §4 允许用 last_action 解释上一 Tick，也允许记忆与推断）。
        """
        if self.pos is None or not self.enemy_memory:
            return direction
        dx, dy = DIRECTIONS.get(direction, (0, 0))
        target = (self.pos[0] + dx, self.pos[1] + dy)
        if not self._cell_may_be_occupied(target):
            return direction
        alternatives = [name for name in ("up", "down", "left", "right") if name != direction]
        # 侧向优先（保持与原方向的垂直分量），再考虑反向
        def rank(name):
            ndx, ndy = DIRECTIONS[name]
            perpendicular = (ndx == 0 and dy == 0) or (ndy == 0 and dx == 0)
            return 0 if perpendicular else 1
        for name in sorted(alternatives, key=rank):
            ndx, ndy = DIRECTIONS[name]
            cell = (self.pos[0] + ndx, self.pos[1] + ndy)
            if not (0 <= cell[0] < max(1, self.width) and 0 <= cell[1] < max(1, self.height)):
                continue
            if cell in self._blocked_now():
                continue
            if self._cell_may_be_occupied(cell):
                continue
            return name
        return direction

    def _cell_may_be_occupied(self, cell):
        """这一 Tick 有没有可能被别的车占着（当前占位，或按上一帧方向推断的下一格）。"""
        cell = tuple(cell)
        for entry in self.enemy_memory:
            pos = entry["pos"]
            if pos == cell:
                return True
            direction = entry.get("dir")
            if direction in DIRECTIONS:
                ex, ey = DIRECTIONS[direction]
                if (pos[0] + ex, pos[1] + ey) == cell:
                    return True
        return False

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
        error = last_action.get("error")
        if error not in ("E_BLOCKED", "E_OOB"):
            return
        origin, direction = attempt
        dx, dy = DIRECTIONS.get(direction, (0, 0))
        blocked = (origin[0] + dx, origin[1] + dy)
        if error == "E_OOB":
            # 越界是地图边界，永久有效（不参与过期）
            self.boundary_obstacles.add(blocked)
            self.known_obstacles.add(blocked)
        else:
            # E_BLOCKED 既可能是静态障碍，也可能是**别的车占位/争格失败**。
            # 当成永久障碍会让我们再也不去那一格（实测卡在任务点相邻格进不去）。
            self.transient_blocked[blocked] = self.last_tick + 8
        if self.strategy is not None:
            self.strategy.seen_resources.pop(blocked, None)

    def _enemy_at(self, direction, enemies):
        """下一步的格子上有没有敌方战车。"""
        dx, dy = DIRECTIONS.get(direction, (0, 0))
        nxt = (self.pos[0] + dx, self.pos[1] + dy)
        return any(epos == nxt for _entity, epos in enemies)

    def _is_large_map(self):
        """规则 §4.1：正式第二阶段是 24×24（第一阶段 20×20），练习包是 18×18。

        "没资源就提前去任务点 / 撞不开就冷却" 这类行为只在大地图上有意义：
        18×18 上资源 20 Tick 就被分完，提前离开争夺区反而亏（实测均值 43.5 → 39.2）。
        """
        return self.width * self.height >= 400

    OBSTACLE_TTL = 40   # Tick：这么久没再看到，就认为它可能是临时墙，先当空地处理

    def _blocked_now(self):
        """当前有效的障碍 = 近期见过的障碍（含临时墙） + 地图边界 + 还没过期的临时堵塞。"""
        now = self.last_tick
        if self.transient_blocked:
            self.transient_blocked = {cell: until for cell, until in self.transient_blocked.items()
                                      if until >= now}
        fresh = {cell for cell, seen in self.obstacle_seen.items()
                 if now - int(seen) <= self.OBSTACLE_TTL}
        return fresh | self.boundary_obstacles | set(self.transient_blocked)

    def _mark_explored(self):
        """视野窗口覆盖过的格子就是"看过"了；不然探索会退化成原地附近乱走。"""
        if self.pos is None or not self.width or not self.height:
            return
        # 协议 §4.1：视野半径由感知的 vision_radius 给出（0–4），配置值只做兜底。
        radius = self.vision_seen if isinstance(self.vision_seen, int) else self.cfg.vision_radius
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
