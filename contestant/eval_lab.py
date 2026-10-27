"""对照实验台：把"原版 / 单策略 / 多策略组合 / 裁剪版"放进同一批地图与对手里跑。

四个分组维度（对应交付要求）：
  baseline  原版基线（planner=legacy，原版战斗模式）
  single    单策略套进现有框架（只开一个收益项，其余关掉）
  combo     多策略拼接（全部开启）
  ablation  裁剪阉割版（全开减掉一项，用来证明那一项真的有用）

环境维度：地图（24×24 开阔/障碍/长墙、18×18 练习尺寸）× 对手阵容 × 随机种子 × 重复次数。
统计维度：得分均值/中位/最低/最高/标准差、名次、资源、伤害、任务、死亡、无效动作、
          撞墙次数、决策耗时 P99、异常次数。任何异常/超时都单独计数，不静默吞掉。

用法：
    python -m contestant.eval_lab --reps 3 --ticks 500
    python -m contestant.eval_lab --groups baseline,combo --reps 1 --ticks 200
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

from contestant.agent import Agent, AgentConfig
from contestant.events import default_script
from contestant.llm_replay import ReplayLlmClient
from contestant.stage2_sim import Stage2Map, Stage2Simulation

# 事件注入（--events）与真实模型回放（--hint-cache）由 main() 写入这两个开关，
# 这样 run_one 不必改签名，也能让所有分组跑在"有事件"的环境里。
EVENTS_ON = False
HINT_CACHE = None
# 裁判地图编辑器导出的地图（--map-file）：设置后所有分组都跑同一张真图，seed 只用于重复
MAP_FILE = None
_MAP_CACHE = None

# 单策略：每个只打开一个开关，其余全关（= "把一条思路塞进现有框架"）
SINGLE_BASE = {"use_race": False, "use_decay": False, "use_route": False,
               "use_contention": False, "use_headroom": False}

DEFAULT_OVERRIDES = {"use_race": False, "use_decay": False, "use_route": False,
                     "use_contention": False, "use_headroom": False,
                     "engage_th": 0.35, "pursue_r": 0}

GROUPS = {
    # ---- 原版基线 ----------------------------------------------------
    "baseline_hybrid": {"kind": "baseline", "planner": "legacy", "combat_mode": "hybrid",
                        "memory_falsify": False},
    "baseline_opportunistic": {"kind": "baseline", "planner": "legacy",
                               "combat_mode": "opportunistic", "memory_falsify": False},
    "baseline_never": {"kind": "baseline", "planner": "legacy", "combat_mode": "never",
                       "memory_falsify": False},
    # 只加"记忆证伪"一项（其余仍是原版）：用来单独度量这一条改动的贡献
    "baseline_plus_falsify": {"kind": "baseline", "planner": "legacy",
                              "combat_mode": "hybrid", "memory_falsify": True},
    # 融合版（guarded）：原版决策路径 + 记忆证伪 + 封顶感知 + 战争期不再空等
    "guarded_hybrid": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                       "memory_falsify": True},
    "guarded_opportunistic": {"kind": "combo", "planner": "guarded",
                              "combat_mode": "opportunistic", "memory_falsify": True},
    # 只关掉"争格避让"（H2）：用来单独度量这一条改动的贡献
    "guarded_no_contention": {"kind": "ablation", "planner": "guarded",
                              "combat_mode": "opportunistic", "memory_falsify": True,
                              "param_overrides": {"use_contention": False}},
    "guarded_no_headroom": {"kind": "ablation", "planner": "guarded",
                            "combat_mode": "opportunistic", "memory_falsify": True,
                            "param_overrides": {"use_headroom": False}},
    "guarded_no_falsify": {"kind": "ablation", "planner": "guarded",
                           "combat_mode": "opportunistic", "memory_falsify": False},
    # 关掉"任务组合约束"= 旧的"第一个未完成资源任务"逻辑（用来度量丢任务概率的变化）
    "guarded_no_portfolio": {"kind": "ablation", "planner": "guarded",
                             "combat_mode": "hybrid", "mission_portfolio": False},
    # 扫图型机器人本身（作为被测方，用来确认它真的在扫图、也用来当对照）
    "sweeper_bot": {"kind": "combo", "planner": "sweeper", "combat_mode": "opportunistic"},
    # 资源争抢评估（race guard）：关掉它 = 上一版行为
    "guarded_no_race_guard": {"kind": "ablation", "planner": "guarded",
                              "combat_mode": "hybrid", "race_guard": False},
    # 战争期巡逻两个小修：① 热点在脚下就换下一个（消等待）② 末期封顶后不巡逻（少送死）
    "guarded_patrol_off": {"kind": "ablation", "planner": "guarded", "combat_mode": "hybrid",
                           "patrol_skip_reached": False, "late_cap_guard": False},
    "guarded_only_skip": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                          "late_cap_guard": False},
    "guarded_only_cap": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                         "patrol_skip_reached": False},
    # 开局静默：前 60/120 片不开火，验证"早期接敌是否拖累资源下限"
    "guarded_quiet60": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                        "early_quiet": 60},
    "guarded_quiet120": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                         "early_quiet": 120},
    "guarded_race_k8": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                        "race_history_ticks": 8},
    "guarded_race_k16": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                         "race_history_ticks": 16},
    # 障碍记忆"永久记住"（原版行为）对照：验证临时墙过期是不是引入的副作用
    "guarded_ttl_forever": {"kind": "ablation", "planner": "guarded",
                            "combat_mode": "hybrid", "obstacle_ttl": 100000},
    # 前段抢稀缺资源（高价值权重倍数扫描）
    "guarded_high15": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                       "high_value_scale": 1.5},
    "guarded_high20": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                       "high_value_scale": 2.0},
    "guarded_high30": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                       "high_value_scale": 3.0},
    "guarded_high20_all": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                           "high_value_scale": 2.0, "high_first_ticks": 0},
    # 任务资源"顺路半径"标定（P-M 门限；实测低分全部来自资源类任务没完成）
    "guarded_md12": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                     "memory_falsify": True, "mission_max_distance": 12},
    "guarded_md16": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                     "memory_falsify": True, "mission_max_distance": 16},
    # ---- 单策略（其余关闭）-------------------------------------------
    "single_race": {"kind": "single", "planner": "fused", "combat_mode": "hybrid",
                    "param_overrides": {**DEFAULT_OVERRIDES, "use_race": True}},
    "single_decay": {"kind": "single", "planner": "fused", "combat_mode": "hybrid",
                     "param_overrides": {**DEFAULT_OVERRIDES, "use_decay": True}},
    "single_route": {"kind": "single", "planner": "fused", "combat_mode": "hybrid",
                     "param_overrides": {**DEFAULT_OVERRIDES, "use_route": True}},
    "single_contention": {"kind": "single", "planner": "fused", "combat_mode": "hybrid",
                          "param_overrides": {**DEFAULT_OVERRIDES, "use_contention": True}},
    "single_headroom": {"kind": "single", "planner": "fused", "combat_mode": "hybrid",
                        "param_overrides": {**DEFAULT_OVERRIDES, "use_headroom": True}},
    "single_all_off": {"kind": "single", "planner": "fused", "combat_mode": "hybrid",
                       "param_overrides": dict(DEFAULT_OVERRIDES)},
    # ---- 多策略拼接 --------------------------------------------------
    "combo_hybrid": {"kind": "combo", "planner": "fused", "combat_mode": "hybrid"},
    "combo_evaluate": {"kind": "combo", "planner": "fused", "combat_mode": "evaluate"},
    "combo_evaluate_loose": {"kind": "combo", "planner": "fused", "combat_mode": "evaluate",
                             "engage_th": 0.15},
    "combo_never": {"kind": "combo", "planner": "fused", "combat_mode": "never"},
    "combo_pressure": {"kind": "combo", "planner": "fused", "combat_mode": "evaluate",
                       "decision_type": "pressure"},
    # 竞争折扣强度标定（03 §12.3 建议的第一敏感项）
    "combo_race0": {"kind": "combo", "planner": "fused", "combat_mode": "hybrid",
                    "param_overrides": {"race_strength": 0.0}},
    "combo_race05": {"kind": "combo", "planner": "fused", "combat_mode": "hybrid",
                     "param_overrides": {"race_strength": 0.5}},
    # 战争期主动找人（approach 修好后才有意义）
    "combo_hunt": {"kind": "combo", "planner": "fused", "combat_mode": "evaluate",
                   "pursue_r": 6, "engage_th": 0.05},
    "combo_hunt_race0": {"kind": "combo", "planner": "fused", "combat_mode": "evaluate",
                         "pursue_r": 6, "engage_th": 0.05,
                         "param_overrides": {"race_strength": 0.0}},
    # ---- 裁剪阉割（全开减一项）---------------------------------------
    "ablate_no_race": {"kind": "ablation", "planner": "fused", "combat_mode": "hybrid",
                       "param_overrides": {"use_race": False}},
    "ablate_no_decay": {"kind": "ablation", "planner": "fused", "combat_mode": "hybrid",
                        "param_overrides": {"use_decay": False}},
    "ablate_no_contention": {"kind": "ablation", "planner": "fused", "combat_mode": "hybrid",
                             "param_overrides": {"use_contention": False}},
    "ablate_no_headroom": {"kind": "ablation", "planner": "fused", "combat_mode": "hybrid",
                           "param_overrides": {"use_headroom": False}},
    "ablate_no_route": {"kind": "ablation", "planner": "fused", "combat_mode": "hybrid",
                        "param_overrides": {"use_route": False}},
}

OPPONENTS = {
    "probe_weak": {"planner": "legacy", "combat_mode": "opportunistic"},
    "probe_mid": {"planner": "legacy", "combat_mode": "hybrid"},
    "probe_farmer": {"planner": "legacy", "combat_mode": "never"},
    "probe_strong": {"planner": "fused", "combat_mode": "hybrid"},
    # 扫图型假想敌：移动优先 + 扫遍全图 + 拿最近资源 + 打一下就跑（contestant/sweeper.py）
    "probe_sweep": {"planner": "sweeper", "combat_mode": "opportunistic"},
}

ENVIRONMENTS = {
    "open24": lambda seed: Stage2Map.open_field(24, 24, seed=seed),
    "dense24": lambda seed: Stage2Map.dense(24, 24, seed=seed),
    "maze24": lambda seed: Stage2Map.maze(24, 24, seed=seed),
    "practice18": lambda seed: Stage2Map.build(18, 18, obstacles=6, seed=seed),
}


def make_brain(seat, spec):
    kwargs = {key: value for key, value in spec.items() if key != "kind"}
    kwargs.setdefault("decision_type", "balanced")
    cfg = AgentConfig(seat=seat, stage=2, **kwargs)
    client = ReplayLlmClient(cache_path=HINT_CACHE) if HINT_CACHE else None
    return Agent(cfg, client=client)


def load_editor_map():
    """读一次编辑器地图并缓存；每局 clone 一份（仿真会就地加临时墙）。"""
    global _MAP_CACHE
    if _MAP_CACHE is None:
        from contestant.map_import import load_editor_file
        _MAP_CACHE = load_editor_file(MAP_FILE)
        print("使用编辑器地图：{0}×{1}，障碍 {2}，资源 {3}，出生点 {4}，任务点 {5}".format(
            _MAP_CACHE.width, _MAP_CACHE.height, len(_MAP_CACHE.obstacles),
            len(_MAP_CACHE.resources), sorted(_MAP_CACHE.spawns), _MAP_CACHE.reach))
    return _MAP_CACHE.clone()


def run_one(group_spec, env_name, opponent, seed, ticks):
    game_map = load_editor_map() if MAP_FILE else ENVIRONMENTS[env_name](seed)
    brains = {"A": make_brain("A", group_spec)}
    for offset, seat in enumerate(("B", "C", "D")):
        brains[seat] = make_brain(seat, OPPONENTS[opponent])
    script = default_script(game_map) if EVENTS_ON else None
    started = time.perf_counter()
    try:
        sim = Stage2Simulation(game_map, brains, ticks=ticks, events=script,
                               session_id="lab-{0}-{1}-{2}".format(env_name, opponent, seed))
        result = sim.run()
    except Exception as exc:                       # 仿真器自身异常也要记账，不能中断整批
        return {"group_score": 0.0, "sim_error": repr(exc), "elapsed_s": 0.0}
    elapsed = time.perf_counter() - started
    seats = result["seats"]
    scores = [seats[seat]["score"] for seat in sorted(seats)]
    mine = seats["A"]
    rank = sorted(scores, reverse=True).index(mine["score"]) + 1
    return {
        "group_score": mine["score"],
        "rank": rank,
        "resource_value": mine["resource_value"],
        "damage": mine["damage"],
        "missions": mine["missions"],
        "mission_ids": list(mine.get("mission_ids") or []),
        "deaths": mine["deaths"],
        "invalid_actions": mine["invalid_actions"],
        "blocked_moves": mine["blocked_moves"],
        "attacks": mine["attacks"],
        "waits": mine["waits"],
        "plan_p99_ms": result.get("plan_p99_ms", 0.0),
        "plan_max_ms": result.get("plan_max_ms", 0.0),
        "errors": len(result["errors"]),
        "sim_error": "",
        "elapsed_s": round(elapsed, 2),
        "seat_scores": scores,
        "hint_hit": _client_stat(brains["A"], "hit"),
        "hint_miss": _client_stat(brains["A"], "miss"),
        "fused_actions": brains["A"].fused_actions,
        "fused_reasons": dict(brains["A"].fused_reasons),
        "war_phase_ticks": 0,
    }


def _client_stat(brain, key):
    """回放客户端的命中/未命中计数（没有客户端时返回 0）。"""
    client = getattr(brain, "client", None)
    if client is None or not hasattr(client, "stats"):
        return 0
    try:
        return int(client.stats().get(key, 0))
    except Exception:
        return 0


def summarize(rows):
    scores = [row["group_score"] for row in rows]
    out = {
        "runs": len(rows),
        "mean": round(statistics.fmean(scores), 2) if scores else 0.0,
        "median": round(statistics.median(scores), 2) if scores else 0.0,
        "min": round(min(scores), 2) if scores else 0.0,
        "max": round(max(scores), 2) if scores else 0.0,
        "stdev": round(statistics.pstdev(scores), 2) if len(scores) > 1 else 0.0,
        "mean_rank": round(statistics.fmean([row.get("rank", 4) for row in rows]), 2) if rows else 0.0,
        "top2_rate": round(sum(1 for row in rows if row.get("rank", 4) <= 2) / max(1, len(rows)), 3),
        "mean_resource": round(statistics.fmean([row["resource_value"] for row in rows]), 1) if rows else 0.0,
        "mean_damage": round(statistics.fmean([row["damage"] for row in rows]), 1) if rows else 0.0,
        "mean_missions": round(statistics.fmean([row["missions"] for row in rows]), 2) if rows else 0.0,
        "mean_deaths": round(statistics.fmean([row["deaths"] for row in rows]), 2) if rows else 0.0,
        "invalid_actions": sum(row["invalid_actions"] for row in rows),
        "blocked_moves": sum(row["blocked_moves"] for row in rows),
        "errors": sum(row["errors"] for row in rows),
        "sim_errors": sum(1 for row in rows if row.get("sim_error")),
        "hint_hit": sum(row.get("hint_hit", 0) for row in rows),
        "hint_miss": sum(row.get("hint_miss", 0) for row in rows),
        "plan_p99_ms": max((row["plan_p99_ms"] for row in rows), default=0.0),
        "mean_elapsed_s": round(statistics.fmean([row["elapsed_s"] for row in rows]), 2) if rows else 0.0,
    }
    return out


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", default="", help="逗号分隔；默认全部")
    parser.add_argument("--envs", default="", help="逗号分隔；默认全部")
    parser.add_argument("--opponents", default="", help="逗号分隔；默认全部")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--ticks", type=int, default=500)
    parser.add_argument("--out", default="docs/experiments")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--events", action="store_true",
                        help="打开事件注入（默认关：无事件环境更接近第一批实验的可比条件）")
    parser.add_argument("--hint-cache", default=None,
                        help="真实模型理解结果的缓存（由 scripts/record_llm_hints.py 生成）")
    parser.add_argument("--map-file", default=None,
                        help="裁判地图编辑器导出的 JSON；给了它就跑这张真图（忽略 --envs）")
    args = parser.parse_args(argv)
    global EVENTS_ON, HINT_CACHE, MAP_FILE
    EVENTS_ON = bool(args.events)
    HINT_CACHE = args.hint_cache
    MAP_FILE = args.map_file

    groups = [name for name in (args.groups.split(",") if args.groups else []) if name] or list(GROUPS)
    envs = [name for name in (args.envs.split(",") if args.envs else []) if name] or list(ENVIRONMENTS)
    opponents = [name for name in (args.opponents.split(",") if args.opponents else []) if name] \
        or list(OPPONENTS)

    results = {}
    raw = []
    for group in groups:
        if group not in GROUPS:
            print("未知分组：{0}".format(group))
            continue
        rows = []
        for env_name in envs:
            for opponent in opponents:
                for rep in range(args.reps):
                    seed = 1000 * envs.index(env_name) + 17 * rep + 3
                    row = run_one(GROUPS[group], env_name, opponent, seed, args.ticks)
                    row.update({"group": group, "env": env_name, "opponent": opponent,
                                "seed": seed, "rep": rep})
                    rows.append(row)
                    raw.append(row)
                    if not args.quiet:
                        print("{0:24s} {1:10s} {2:12s} seed={3:<5d} score={4:5.2f} "
                              "rv={5:3d} dmg={6:6.1f} m={7} d={8} inv={9}".format(
                                  group, env_name, opponent, seed, row["group_score"],
                                  row["resource_value"], row["damage"], row["missions"],
                                  row["deaths"], row["invalid_actions"]))
                        sys.stdout.flush()
        results[group] = summarize(rows)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "lab_raw.json"), "w", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "lab_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=1)

    header = ("| 分组 | 类别 | 均值 | 中位 | 最低 | 最高 | 标准差 | 均名次 | 前二率 | 资源 | "
              "伤害 | 任务 | 死亡 | 无效动作 | 撞墙 | 异常 | 提示命中/未中 | P99(ms) |")
    lines = [header, "|" + "---|" * 18]
    for group, stat in sorted(results.items(), key=lambda item: -item[1]["mean"]):
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} | {10} | "
                     "{11} | {12} | {13} | {14} | {15} | {16}/{17} | {18} |".format(
                         group, GROUPS[group]["kind"], stat["mean"], stat["median"],
                         stat["min"], stat["max"], stat["stdev"], stat["mean_rank"],
                         stat["top2_rate"], stat["mean_resource"], stat["mean_damage"],
                         stat["mean_missions"], stat["mean_deaths"], stat["invalid_actions"],
                         stat["blocked_moves"], stat["errors"] + stat["sim_errors"],
                         stat["hint_hit"], stat["hint_miss"], stat["plan_p99_ms"]))
    table = "\n".join(lines)
    with open(os.path.join(args.out, "lab_summary.md"), "w", encoding="utf-8") as handle:
        handle.write("# 对照实验结果（自动生成）\n\n")
        handle.write("运行条件：{0} 张地图 × {1} 类对手 × {2} 次重复，每局 {3} Tick。\n\n".format(
            len(envs), len(opponents), args.reps, args.ticks))
        handle.write(table + "\n")
    print("\n" + table)
    print("\n结果已写入 {0}/lab_summary.md、lab_summary.json、lab_raw.json".format(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
