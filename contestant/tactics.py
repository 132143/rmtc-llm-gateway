"""策略参数层与收益函数（纯函数，无 I/O、无 ROS、无随机）。

来源：交付包 03_收益表参数表.md（编号 P-*）与 02_decision_type规格.md 第 3 节增量矩阵。
本模块只做两件事：
  1. 把"封存参数"集中成一份不可变 Params，任何决策类型的差异都用增量倍数表达；
  2. 把 03 号文件里的收益公式写成可单测的函数（资源/任务/治疗/堡垒/战斗五类）。

设计约束（对应规则条款）：
  - 参数不得散落在决策代码里（规则 §2.3：现场只允许改 decision_type，不得夹带新增参数）；
  - 公式里的数值全部来自规则只读值或 03 号表，禁止在这里拍新数字（P-C-xx / P-F-xx 除外，已登记）；
  - 纯函数：同样的入参必得同样的结果，便于离线对照实验复现。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# ---------------------------------------------------------------- 规则只读值
# 规则 §4.3 / §6.2 / §8.5 / §8.9，这些数字属于裁判规定，程序只读不改。
HP_MAX = 100
ATTACK_DAMAGE = 20
DEFENDED_DAMAGE = 10
VULN_MULTIPLIER = 2.0
DEATH_PENALTY = 60          # §7 每次死亡扣 60 有效伤害，最低 0
KILL_REWARD = 60            # §10.2 每次击毁另有 60 点击杀奖励
AOE_RADIUS = 2              # §8.5 无心 5×5 = 横纵各 ±2
AOE_DAMAGE = 50
AOE_DEFENDED = 10
STAGE2_R_FULL = 120.0       # §10.2 资源满分所需价值
STAGE2_D_FULL = 500.0       # §10.2 对抗满分所需有效伤害（v1.1 由 300 改为 500）
MISSION_POINTS = 5.0
STAGE2_MAX = 60.0
STAGE1_MAX = 40.0
STAGE1_R_FULL = 200.0

# 单位分值（03 §1）：期望得分 = 价值 × 0.25（资源）/ 5（任务）/ 0.03（伤害）
SCORE_PER_RESOURCE_VALUE = 30.0 / STAGE2_R_FULL   # 0.25
SCORE_PER_DAMAGE = 15.0 / STAGE2_D_FULL           # 0.03


# 每个参数"到底在哪里生效"。封存答辩时裁判可能逐条问，这张表就是答案；
# 标 INACTIVE 的参数在 03 号文件里登记过，但当前代码没有消费它——
# 保留是为了让"参数编号 ↔ 代码字段"一一对应，不允许它们改变任何行为。
PARAM_CONSUMPTION = {
    "w_res": "planner.resource_utility",
    "w_mis": "planner.mission_utility",
    "w_dmg": "planner.combat_utility",
    "w_heal": "planner.heal_utility",
    "w_fort": "planner.fort_utility",
    "k_tie": "planner.resource_utility",
    "route_bonus": "planner._route_bonus",
    "mem_forget": "agent._update_memory / planner（记忆窗口）",
    "enemy_mem_forget": "agent._update_memory（敌车记忆窗口）",
    "high_priority": "planner._renormalize_and_pick（P-R-05）",
    "t_task3": "planner（任务硬截止 H6）",
    "mission_max_distance": "agent._mission_resource_move（顺路半径）",
    "heal_per_tick": "INACTIVE（规则只读值 10，仿真器直接按规则结算）",
    "fort_value": "planner.fort_utility",
    "fort_dwell": "planner.fort_utility（成本项）",
    "fort_contested_scale": "planner.fort_utility（周围敌车 ≥2 时折价）",
    "engage_th": "planner._combat_candidate（仅在 fused 内核生效）",
    "flee_hp": "INACTIVE（脱战由 agent.low_hp / 防御闸门控制）",
    "pursue_r": "planner._combat_candidate（追击，实测负收益，默认 0）",
    "pursue_max_ticks": "INACTIVE（追击已判定无效，未接入）",
    "p_death_base": "planner → tactics.death_probability",
    "max_death": "planner._combat_candidate（P-C-12 阈值放宽 1.5×）",
    "ambush_on": "agent / planner（伏击点位，默认关）",
    "fireline_on": "INACTIVE（踩火线属裁判侧结算，选手侧不主动触发）",
    "dry_ticks": "agent._update_memory → tactics.is_war_phase",
    "dry_min_tick": "tactics.is_war_phase",
    "t_wind_down": "INACTIVE（收尾期只由 reach_after_tick 与战争期控制）",
    "t_final_hold": "INACTIVE（同上）",
    "lambda_death": "INACTIVE（风险溢价的死亡项由 combat_utility 直接算）",
    "danger_unparsed": "INACTIVE（未解析公告的保守避让尚未接入收益表）",
    "lethal_margin": "INACTIVE（撤离致死区由 planner._step_is_safe 直接判格）",
    "use_race": "planner._race_discount（消融开关）",
    "race_strength": "planner._race_discount（折扣强度）",
    "use_decay": "planner（记忆衰减开关）",
    "use_route": "planner._route_bonus（消融开关）",
    "use_contention": "planner._step_is_safe / agent._contention_detour",
    "use_headroom": "tactics.resource_headroom / damage_headroom",
    "tie_residual": "tactics.resource_headroom / damage_headroom",
}


@dataclass(frozen=True)
class Params:
    """封存参数（03 号文件基线值）。字段注释里给编号，便于和文档逐条对照。"""

    # P-G 全局权重
    w_res: float = 1.00            # P-G-01
    w_mis: float = 1.20            # P-G-02
    w_dmg: float = 0.80            # P-G-03
    w_heal: float = 1.00           # P-G-04
    w_fort: float = 1.00           # P-G-05

    # P-R 资源
    k_tie: float = 0.50            # P-R-01
    route_bonus: float = 1.15      # P-R-03
    mem_forget: int = 160          # P-R-06
    enemy_mem_forget: int = 60     # P-R-07
    high_priority: float = 2.0     # P-R-05

    # P-M 任务
    t_task3: int = 300             # P-M-01
    mission_max_distance: int = 8  # 任务资源"顺路"半径（实测 24×24 上 45→52.5）

    # P-H / P-F 治疗与堡垒
    heal_per_tick: int = 10        # P-H-01（只读）
    fort_value: float = 12.0       # P-F-01
    fort_dwell: int = 10           # P-F-02（只读）
    fort_contested_scale: float = 0.5   # 周围敌车 ≥2 时 V_FORT ×0.5

    # P-C 战斗
    engage_th: float = 0.35        # P-C-01
    flee_hp: int = 40              # P-C-02
    pursue_r: int = 0              # P-C-03
    pursue_max_ticks: int = 6      # P-C-06
    p_death_base: float = 0.10     # P-C-07
    max_death: int = 2             # P-C-12
    ambush_on: bool = False        # P-C-13
    fireline_on: bool = False      # P-C-05

    # P-T 时间阶段
    dry_ticks: int = 25            # P-T-02
    dry_min_tick: int = 120        # P-T-03
    t_wind_down: int = 450         # P-T-08
    t_final_hold: int = 490        # P-T-09

    # 消融开关（对照实验用；封存默认全部开启）
    use_race: bool = True          # 竞争折扣 C_RACE（P-R-02）
    race_strength: float = 1.0     # 折扣强度：0=不折扣，1=按 03 §4.2 全表（标定用）
    use_decay: bool = True         # 记忆衰减 D_MEM（P-R-04）
    use_route: bool = True         # 打包加成 B_ROUTE（P-R-03）
    use_contention: bool = True    # 争格预测（H2）
    use_headroom: bool = True      # 封顶感知（§10.2 资源 120 / 伤害 500 之后边际收益趋零）
    tie_residual: float = 0.05     # 封顶后残留权重：同分比较仍看资源与伤害（§3）

    # P-X 风险
    lambda_death: float = 1.5      # P-X-01
    danger_unparsed: float = 0.5   # P-X-04
    lethal_margin: int = 1         # P-X-06

    def with_deltas(self, deltas: dict) -> "Params":
        """按 02 §3 的增量矩阵生成派生参数；未知键直接忽略（不许现场夹带新参数）。"""
        allowed = {f for f in self.__dataclass_fields__}
        applied = {k: v for k, v in (deltas or {}).items() if k in allowed and isinstance(v, (int, float))}
        return replace(self, **applied) if applied else self


# 02 §3 的参数增量矩阵。文档口径是"乘在基线参数上的倍数"，但矩阵里同时列了
# 权重（该乘）与阈值（P-C-01/02/03、P-R-05 这类整数值，乘上去会得到荒谬结果，
# 例如 engage_th 0.35×0.35=0.12）。这里按物理含义拆成两张表，并写清楚：
#   权重 → 乘数；阈值/开关 → 绝对值。两张表的数值与 02 号文件的行一一对应。
WEIGHT_MULT = {
    "s1_tour": {},
    "s1_sweep": {},
    "economy": {"w_res": 1.00, "w_mis": 1.20, "w_dmg": 0.80, "w_heal": 1.00, "w_fort": 1.00},
    "pressure": {"w_res": 0.75, "w_mis": 1.20, "w_dmg": 1.90, "w_heal": 0.80, "w_fort": 0.90},
    "turtle": {"w_res": 0.90, "w_mis": 1.50, "w_dmg": 0.25, "w_heal": 1.50, "w_fort": 1.00},
    "center_grab": {"w_res": 0.85, "w_mis": 1.30, "w_dmg": 1.00, "w_heal": 1.20,
                    "w_fort": 2.00},
}
ABSOLUTE_DELTAS = {
    "s1_tour": {},
    "s1_sweep": {},
    "economy": {"engage_th": 0.35, "flee_hp": 40, "pursue_r": 0, "ambush_on": False,
                "high_priority": 2.0},
    "pressure": {"engage_th": 0.15, "flee_hp": 25, "pursue_r": 3, "ambush_on": True,
                 "fireline_on": True, "high_priority": 2.5},
    "turtle": {"engage_th": 0.80, "flee_hp": 60, "pursue_r": 0, "ambush_on": False,
               "high_priority": 1.5},
    "center_grab": {"engage_th": 0.30, "flee_hp": 40, "pursue_r": 0, "ambush_on": True,
                    "high_priority": 1.8},
}
# 旧登记名 → 新枚举。旧名继续可用，避免封存枚举断代（登记表同时列出两套即可）。
LEGACY_ALIASES = {"collect": "turtle", "balanced": "economy", "combat": "pressure"}


def params_for(decision_type: str) -> Params:
    """按已登记枚举生成参数：权重相乘、阈值/开关取绝对值。"""
    base = Params()
    key = LEGACY_ALIASES.get(decision_type, decision_type)
    weights = WEIGHT_MULT.get(key, {})
    absolute = ABSOLUTE_DELTAS.get(key, {})
    merged = {}
    for name, mult in weights.items():
        merged[name] = round(getattr(base, name) * float(mult), 6)
    merged.update(absolute)
    return base.with_deltas(merged)


# ---------------------------------------------------------------- 竞争折扣 P-R-02
def race_discount(delta) -> float:
    """Δ = 敌方最近 BFS 距离 − 我方距离。表见 03 §4.2。

    本赛是同时行动占位博弈：同点竞速可能双输，所以 Δ=0 只给 0.50；
    明显更远（Δ≥+2）也不给 1.0，因为视野外可能还有车（信息边界）。
    """
    if delta is None:
        return 1.0            # 视野内没有敌人：按 1.0 计（不奖励也不惩罚）
    if delta >= 2:
        return 0.95
    if delta == 1:
        return 0.85
    if delta == 0:
        return 0.50
    if delta == -1:
        return 0.35
    return 0.15


# ---------------------------------------------------------------- 记忆衰减 P-R-04
def mem_decay(age: int) -> float:
    if age <= 40:
        return 1.00
    if age <= 80:
        return 0.85
    return 0.70


# ---------------------------------------------------------------- 紧急度 P-M-02
def urgency(tick: int) -> float:
    if tick < 200:
        return 1.0
    if tick < 250:
        return 1.3
    if tick < 300:
        return 2.0
    return 3.5


# ---------------------------------------------------------------- 治疗 P-H
def heal_factor(hp: float) -> float:
    if hp < 40:
        return 2.0
    if hp < 70:
        return 1.2
    return 0.6


# ---------------------------------------------------------------- 战斗 P-C
def hp_death_multiplier(hp: float) -> float:
    """P-C-08 M_hp。血越少越容易死，直接乘在基础死亡概率上。"""
    if hp < 40:
        return 3.0
    if hp < 70:
        return 1.4
    return 1.0


def death_probability(hp: float, n_enemy_adjacent: int = 1, vulnerable: bool = False,
                      in_heal: bool = False, p_base: float = 0.10) -> float:
    """P-C/§7.4：P_death = clamp(BASE + 0.15×(n−1)) × M_hp × M_vul × M_heal，上限 0.95。"""
    raw = p_base + 0.15 * max(0, int(n_enemy_adjacent) - 1)
    raw *= hp_death_multiplier(hp)
    if vulnerable:
        raw *= 2.0
    if in_heal:
        raw *= 0.4
    return max(0.0, min(0.95, raw))


# P-escape 表（03 §7.3），键是"态势"，值是被逃掉的概率。
ESCAPE_TABLE = {
    "axis_free": 0.75,     # 轴线接触且对方能自由移动：追不上（规则 §6.1 不能跟进腾空格）
    "pinned": 0.15,        # 被地形/占位夹住
    "entering_point": 0.35,  # 正在进入资源/出生点/中心，必须站定
    "in_aoe": 0.10,        # 处于 5×5 无双范围，斜向也跑不掉
    "protected": 1.00,     # 出生保护：直接不参战
}


def escape_probability(situation: str) -> float:
    return ESCAPE_TABLE.get(situation, ESCAPE_TABLE["axis_free"])


# ---------------------------------------------------------------- 封顶感知
def resource_headroom(resource_value: float, params: Params | None = None) -> float:
    """资源分在 R2=120 时封顶（§10.2）。封顶后仍有一点价值：同名次比较看资源总价值（§3）。

    这是原版代码没有的一层：它按固定权重一直抢资源，抢到 200/200 之后多抢一个
    只是"同分时的保险"，不该再挤占战争期的时间。
    """
    p = params or Params()
    if not p.use_headroom:
        return 1.0
    left = max(0.0, 1.0 - float(resource_value) / STAGE2_R_FULL)
    return left if left > 0 else p.tie_residual


def damage_headroom(damage: float, params: Params | None = None) -> float:
    """对抗分在 D=500 时封顶（§10.2，v1.1 由 300 改为 500）。"""
    p = params or Params()
    if not p.use_headroom:
        return 1.0
    left = max(0.0, 1.0 - float(damage) / STAGE2_D_FULL)
    return left if left > 0 else p.tie_residual


# ---------------------------------------------------------------- 收益公式
def resource_utility(value: float, dist: int, race: float = 1.0, route: float = 1.0,
                     decay: float = 1.0, params: Params | None = None,
                     headroom: float = 1.0) -> float:
    """U_r = W_RES × 0.25 × value × C_race × B_route × D_mem × H_cap / (dist + K_tie)"""
    p = params or Params()
    return (p.w_res * SCORE_PER_RESOURCE_VALUE * float(value) * race * route * decay * headroom
            / (max(0, dist) + p.k_tie))


def mission_utility(dist: int, tick: int, params: Params | None = None) -> float:
    """U_m = W_MIS × 5.0 × urgency(tick) / (dist + 1)"""
    p = params or Params()
    return p.w_mis * MISSION_POINTS * urgency(tick) / (max(0, dist) + 1.0)


def heal_utility(hp: float, dist: int, params: Params | None = None) -> float:
    """U_h = W_HEAL × (100 − hp) × 0.03 × F_hp / (dist + 1)"""
    p = params or Params()
    missing = max(0.0, HP_MAX - float(hp))
    return p.w_heal * missing * SCORE_PER_DAMAGE * heal_factor(hp) / (max(0, dist) + 1.0)


def fort_utility(dist: int, enemies_near: int = 0, params: Params | None = None) -> float:
    """U_f = W_FORT × V_FORT / (dist + T_DWELL)；周围敌车 ≥2 时 V_FORT ×0.5（易被打断）。"""
    p = params or Params()
    value = p.fort_value
    if enemies_near >= 2:
        value *= p.fort_contested_scale
    return p.w_fort * value / (max(0, dist) + p.fort_dwell)


def combat_utility(expected_damage: float, escape: float, p_death: float,
                   opportunity_cost: float = 0.0, params: Params | None = None) -> float:
    """U_e = W_DMG×E[dmg]×0.03×(1−P_escape) − W_DMG×P_death×60×0.03 − OPP_COST（单位：分）"""
    p = params or Params()
    gain = p.w_dmg * float(expected_damage) * SCORE_PER_DAMAGE * (1.0 - escape)
    loss = p.w_dmg * float(p_death) * DEATH_PENALTY * SCORE_PER_DAMAGE
    return gain - loss - float(opportunity_cost)


# ---------------------------------------------------------------- 阶段判定 P-T
def is_war_phase(tick: int, dry_ticks: int, params: Params | None = None,
                 resource_value_self: float = 0.0) -> bool:
    """资源见底 ⟺ (tick ≥ DRY_MIN_TICK 且连续 DRY_TICKS 无新可见资源)
                  ∨ (本队已收集 ≥100 且连续 DRY_TICKS 无新资源)。

    依据 03 §9.1：判据只用"合法感知到的新发现"，不需要全图。
    """
    p = params or Params()
    if tick < p.dry_min_tick:
        return False
    return dry_ticks >= p.dry_ticks or (resource_value_self >= 100 and dry_ticks >= 1)


# ---------------------------------------------------------------- 计分（用于自检与实验）
def stage2_score(resource_value: float, damage: float, missions: int) -> float:
    r = 30.0 * min(float(resource_value) / STAGE2_R_FULL, 1.0)
    d = 15.0 * min(float(damage) / STAGE2_D_FULL, 1.0)
    m = MISSION_POINTS * max(0, min(3, int(missions)))
    return round(r + d + m, 3)


def stage1_score(resource_value: float) -> float:
    return round(STAGE1_MAX * min(float(resource_value) / STAGE1_R_FULL, 1.0), 3)


# ---------------------------------------------------------------- 占位博弈预测
def contested_cells(moves: dict) -> set:
    """给 {seat: 目标格}，返回"被 ≥2 车指向"的格子（规则 §5.2：多人争格全部失败）。

    离场格（本 Tick 会被腾出来的格子）同样不能进入，单独由 vacated_cells 给出。
    """
    counts: dict = {}
    for cell in moves.values():
        if cell is None:
            continue
        counts[cell] = counts.get(cell, 0) + 1
    return {cell for cell, n in counts.items() if n >= 2}


def vacated_cells(current: dict) -> set:
    """本 Tick 即将被腾出的格子：规则 §6.1 明确不能跟进（含互换位置）。"""
    return {cell for cell in current.values() if cell is not None}
