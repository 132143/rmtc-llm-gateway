"""决策类型表：规则 §2.3 允许现场切换的"已登记枚举"。

规则原文要点：
- 允许切换的决策类型必须在**阶段封存前**完成实现和封装，并向裁判**登记类型标识、作用和读取方式**；
- 现场统一使用 JSON 参数格式，例如 {"decision_type":"collect"}；
- decision_type 的值只能来自本队已登记的枚举；
- 参数文件统一命名为 decision_config.json，现场只允许修改 decision_type，
  不得夹带坐标、动作、目标 ID、任意指令或新增算法参数；
- 每队每场最多申请两次策略切换，须在暂停期间或加赛前提出，经裁判批准并登记。

所以这里就是"登记表"：改动它等于改动封存内容，赛前定好，现场不要再动。

每个类型只决定三件事（都在封存前写死，现场一个字符串切换）：
    strategy     采集内核（greedy，或带 detour 的"值钱优先"greedy）
    combat_mode  战斗纪律（never / opportunistic / finish / evaluate）
    planner      legacy=原版"最近资源"内核；fused=收益表内核（contestant/planner.py）
参数增量来自 contestant/tactics.py 的 WEIGHT_MULT / ABSOLUTE_DELTAS（02 §3 的矩阵）。
"""

from __future__ import annotations

import json
import os

DECISION_TYPES = {
    # ---------------- 第一阶段（只有 MOVE/WAIT）----------------
    # 巡回优先：价值/距离排序，官方扫描 9 张图 40/40
    "s1_tour": {"strategy": "greedy", "combat_mode": "never", "planner": "legacy",
                "detour": 0.0, "stage": 1},
    # 扫描优先：更"值钱优先"（detour 抬到 6），用于上一场出现"扫图丢失资源"时
    "s1_sweep": {"strategy": "greedy", "combat_mode": "never", "planner": "legacy",
                 "detour": 6.0, "stage": 1},
    # ---------------- 第二阶段（MOVE/ATTACK/DEFEND/WAIT）----------------
    # 经济主导：资源权重最高，只在顺路/拦截位置出手（默认）
    "economy": {"strategy": "greedy", "combat_mode": "opportunistic", "planner": "guarded",
                "stage": 2},
    # 伤害主导：开战阈值下调、允许追击与伏击（收益表内核的表达方式）
    "pressure": {"strategy": "greedy", "combat_mode": "hybrid", "planner": "guarded",
                 "stage": 2, "decision_type_tag": "pressure"},
    # 保命与任务优先：只补刀残血（finish），脱战阈值提到 60
    "turtle": {"strategy": "greedy", "combat_mode": "finish", "planner": "guarded",
               "stage": 2},
    # 中心控制：堡垒/治疗点加权（靠收益表内核的 W_FORT/W_HEAL），绑定 [12,12]
    "center_grab": {"strategy": "greedy", "combat_mode": "opportunistic", "planner": "fused",
                    "stage": 2},
    # ---------------- 兼容旧登记名（封存前的老三样）----------------
    "collect": {"strategy": "greedy", "combat_mode": "never", "planner": "guarded",
                "stage": 2, "alias_of": "turtle"},
    # 封存默认走 pressure（hybrid）：前二率最高（0.708 vs 0.688），动作组合也可观测地不同。
    "balanced": {"strategy": "greedy", "combat_mode": "hybrid", "planner": "guarded",
                 "stage": 2, "alias_of": "pressure"},
    "combat": {"strategy": "greedy", "combat_mode": "hybrid", "planner": "guarded",
               "stage": 2, "alias_of": "pressure"},
}
DEFAULT_DECISION_TYPE = "balanced"


def load_decision_config(path="decision_config.json"):
    """读 decision_config.json，返回 (decision_type, 说明)。

    只认 decision_type；其他字段一律忽略（规则禁止夹带参数）。读不到/非法就回默认值，
    绝不因为配置文件有问题让程序起不来。
    """
    if not path or not os.path.exists(path):
        return DEFAULT_DECISION_TYPE, f"未找到 {path}，用默认 {DEFAULT_DECISION_TYPE}"
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return DEFAULT_DECISION_TYPE, f"{path} 读取失败（{exc}），用默认 {DEFAULT_DECISION_TYPE}"
    if not isinstance(payload, dict):
        return DEFAULT_DECISION_TYPE, f"{path} 不是 JSON 对象，用默认 {DEFAULT_DECISION_TYPE}"
    value = payload.get("decision_type")
    if value not in DECISION_TYPES:
        return DEFAULT_DECISION_TYPE, f"decision_type={value!r} 未登记，用默认 {DEFAULT_DECISION_TYPE}"
    extra = sorted(set(payload) - {"decision_type"})
    note = f"（忽略未登记字段：{extra}）" if extra else ""
    return value, f"decision_type={value}{note}"
