"""决策类型表：规则 §2.3 允许现场切换的"已登记枚举"。

规则原文要点：
- 允许切换的决策类型必须在**阶段封存前**完成实现和封装，并向裁判**登记类型标识、作用和读取方式**；
- 现场统一使用 JSON 参数格式，例如 {"decision_type":"collect"}；
- decision_type 的值只能来?本队已登记的枚举；
- 参数文件统一命名为 decision_config.json，现场只允许修改 decision_type，
  不得夹带坐标、动作、目标 ID、任意指令或新增算法参数；
- 每队每场最多申请两次策略切换，须在暂停期间或加赛前提出，经裁判批准并登记。

所以这里就是"登记表"：改动它等于改动封存内容，赛前定好，现场不要再动。
"""

from __future__ import annotations

import json
import os

DECISION_TYPES = {
    # 只采集：第二阶段也不主动进攻（适合资源充裕、需要稳分的场次）
    "collect": {"strategy": "greedy", "combat_mode": "never"},
    # 平衡（默认）：顺路就打，不主动追
    "balanced": {"strategy": "greedy", "combat_mode": "opportunistic"},
    # 对抗：没资源可捡时转为压制对手（正式节奏实测最高分）
    "combat": {"strategy": "greedy", "combat_mode": "hybrid"},
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
