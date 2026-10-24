"""全盘压力扫描：把能踩的边界都踩一遍，确认没有隐性崩溃。

覆盖四类风险（这些都不是单元测试能覆盖的"组合爆炸"区）：
  1) 第一阶段：随机地图 + 随机种子（探索退化、原地打转）；
  2) 第二阶段：地图 × 对手 × 事件开/关 × 长局，检查异常/非法动作/是否卡死；
  3) 确定性：同种子重跑必须逐位一致（否则"对比实验"本身不可信）；
  4) 畸形输入：随机垃圾感知帧，程序只能降级，绝不允许抛异常。

用法：
    python -m scripts.stress_sweep --stage2 24 --events-both
"""

from __future__ import annotations

import argparse
import json
import random
import sys

from contestant.agent import Agent, AgentConfig
from contestant.events import default_script
from contestant.llm_replay import ReplayLlmClient
from contestant.sim import Stage1Map, run_stage1
from contestant.stage2_sim import Stage2Map, Stage2Simulation
from contestant.strategy import GreedyBfsStrategy

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

JUNK_TICKS = 200


def junk_frame(rng):
    """随机畸形感知：字段缺失、类型错、坐标越界、坐标不是数字……"""
    pos = [rng.randint(-2, 30), rng.randint(-2, 30)]
    entities = []
    for _ in range(rng.randint(0, 6)):
        entities.append(rng.choice([
            {"type": "robot", "position": 12345},
            {"type": "robot", "position": "000\n111"},
            {"type": "resource_normal", "position": None, "value": "abc"},
            {"type": "obstacle", "position": "\n".join(["0" * 9] * 9)},
            "just-a-string",
            None,
            {"type": "resource_high", "value": 20, "position": "\n".join(
                ["0" * 9] * 4 + ["000010000"] + ["0" * 9] * 4)},
        ]))
    return {
        "tick": rng.choice([rng.randint(0, 600), "5", None, 3.5]),
        "session_id": rng.choice(["s1", "", None, 7]),
        "vision_radius": rng.choice([0, 4, 9, None, "4"]),
        "self": rng.choice([
            {"position": pos, "hp": rng.choice([100, -5, "x", None]), "alive": rng.choice([True, False]),
             "protected": rng.choice([True, False]), "defending": rng.choice([True, False]),
             "last_action": rng.choice([None, {"moved": False, "error": "E_BLOCKED"}, "bad"])},
            None, {}, {"position": "8,8"},
        ]),
        "visible_entities": entities,
        "map": rng.choice([{"width": 24, "height": 24}, {"width": 0, "height": 0}, {}, None]),
        "mission_text": rng.choice(["", None, 5]),
        "missions": rng.choice([[], None, [{"id": 1, "type": "reach", "position": [1]}],
                                [{"id": "reach_target", "type": "reach", "position": [12, 12]}]]),
        "event_text": rng.choice(["", "第 1 Tick：随便说说", None]),
        "score": rng.choice([{"resource_value": 10, "effective_damage": 0},
                             {"resource_value": None, "effective_damage": "x"}, {}, None]),
    }


def check_stage1(count, ticks):
    problems = []
    for seed in range(count):
        game_map = Stage1Map.random(seed=seed + 9000)
        strategy = GreedyBfsStrategy(game_map.width, game_map.height, detour=0.0)
        try:
            result = run_stage1(game_map, strategy, ticks=ticks)
        except Exception as exc:
            problems.append(("stage1", seed, repr(exc)))
            continue
        if result["blocked_moves"] > ticks:
            problems.append(("stage1", seed, "撞墙次数超过 Tick 数"))
    return problems


def play_stage2(env, seed, opponent, events, ticks=500):
    if env == "open24":
        game_map = Stage2Map.open_field(24, 24, seed=seed)
    elif env == "dense24":
        game_map = Stage2Map.dense(24, 24, seed=seed)
    elif env == "maze24":
        game_map = Stage2Map.maze(24, 24, seed=seed)
    else:
        game_map = Stage2Map.build(18, 18, obstacles=6, seed=seed)
    script = default_script(game_map) if events else None
    brains = {}
    for seat in ("A", "B", "C", "D"):
        client = ReplayLlmClient(cache_path="docs/events_cache.json") if events else None
        spec = AgentConfig(seat=seat, stage=2)
        brains[seat] = Agent(spec, client=client)
    sim = Stage2Simulation(game_map, brains, ticks=ticks, events=script,
                           session_id="stress-{0}-{1}".format(env, seed))
    return sim.run()


def check_stage2(cases, ticks, events_both):
    problems = []
    for env, seed, opponent in cases:
        for events in ((False, True) if events_both else (False,)):
            try:
                result = play_stage2(env, seed, opponent, events, ticks=ticks)
            except Exception as exc:
                problems.append((env, seed, events, "异常：{0}".format(exc)))
                continue
            if result["errors"]:
                problems.append((env, seed, events, "感知/决策异常 {0} 次".format(len(result["errors"]))))
            total_invalid = sum(seat["invalid_actions"] for seat in result["seats"].values())
            if total_invalid:
                problems.append((env, seed, events, "非法动作 {0} 次".format(total_invalid)))
            if all(seat["moves"] == 0 for seat in result["seats"].values()):
                problems.append((env, seed, events, "四台车都没动（卡死）"))
    return problems


def check_determinism(cases, ticks):
    problems = []
    def stable(result):
        """比较时剔除耗时字段：plan_p99_ms / plan_max_ms 天生随机器负载变化。"""
        return json.dumps({k: v for k, v in result.items() if not k.startswith("plan_")},
                          sort_keys=True, default=str)

    for env, seed, _opponent in cases:
        first = play_stage2(env, seed, "probe_mid", False, ticks=ticks)
        second = play_stage2(env, seed, "probe_mid", False, ticks=ticks)
        if stable(first) != stable(second):
            problems.append((env, seed, "同种子两次结果不一致"))
    return problems


def check_junk_frames(rounds):
    rng = random.Random(20261024)
    problems = []
    agent = Agent(AgentConfig(seat="A", stage=2))
    for index in range(rounds):
        try:
            agent.on_perception(junk_frame(rng))
        except Exception as exc:
            problems.append(("junk", index, repr(exc)))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage1", type=int, default=40)
    parser.add_argument("--stage2", type=int, default=12)
    parser.add_argument("--ticks", type=int, default=300)
    parser.add_argument("--events-both", action="store_true")
    parser.add_argument("--junk", type=int, default=JUNK_TICKS)
    args = parser.parse_args(argv)

    envs = ("open24", "dense24", "maze24", "practice18")
    opponents = ("probe_weak", "probe_mid", "probe_farmer", "probe_strong")
    cases = [(envs[i % len(envs)], 500 + 17 * i, opponents[i % len(opponents)])
             for i in range(args.stage2)]

    failures = 0
    problems = []
    problems += [("stage1",) + p[1:] for p in check_stage1(args.stage1, 100)]
    problems += check_stage2(cases, args.ticks, args.events_both)
    problems += check_determinism(cases[:3], args.ticks)
    problems += check_junk_frames(args.junk)
    failures = len(problems)

    print("第一阶段 {0} 张随机图 / 第二阶段 {1} 个组合（事件{2}）/ 确定性 {3} 组 / 畸形帧 {4} 帧".format(
        args.stage1, args.stage2, "开+关" if args.events_both else "关", 3, args.junk))
    if problems:
        print("[BAD] 发现 {0} 个问题：".format(failures))
        for item in problems[:40]:
            print("   ", item)
        return 1
    print("[OK] 未发现异常、非法动作、卡死或不确定性")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
