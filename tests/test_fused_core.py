"""fused 内核的单元测试：参数层（tactics）、决策层（planner）、评测层（stage2_sim）。

这三块是"收益表策略"的全部新增代码，也是最容易出隐性 bug 的地方：
优先级写反、边界条件漏判、分数公式位移都会让整场稳定丢分。
所以这里不测"跑得起来"，只测"行为是否符合规则条款"。
"""

from __future__ import annotations

import unittest

from contestant import tactics
from contestant.planner import Planner, WorldView, build_field
from contestant.stage2_sim import (
    ATTACK_DAMAGE,
    DEFENDED_DAMAGE,
    MISSIONS,
    Stage2Map,
    Stage2Simulation,
)


def make_view(**overrides):
    defaults = dict(pos=(5, 5), tick=10, hp=100, width=24, height=24,
                    alive=True, protected=False, defending=False, blocked=set(),
                    resources=[], enemies=[], reach_target=None, reach_done=False,
                    avoid_cells=set(), danger_level="none", heal_points=[],
                    fort_zone=None, fort_until=None, dry_ticks=0, resource_value=0,
                    damage=0, missions_completed=[], combat_allowed=True,
                    params=tactics.Params(), deaths=0, war_phase=False)
    defaults.update(overrides)
    return WorldView(**defaults)


class TestTactics(unittest.TestCase):
    def test_race_discount_table(self):
        self.assertAlmostEqual(tactics.race_discount(2), 0.95)
        self.assertAlmostEqual(tactics.race_discount(1), 0.85)
        self.assertAlmostEqual(tactics.race_discount(0), 0.50)
        self.assertAlmostEqual(tactics.race_discount(-1), 0.35)
        self.assertAlmostEqual(tactics.race_discount(-5), 0.15)
        self.assertAlmostEqual(tactics.race_discount(None), 1.0)

    def test_memory_decay(self):
        self.assertEqual(tactics.mem_decay(0), 1.0)
        self.assertEqual(tactics.mem_decay(40), 1.0)
        self.assertEqual(tactics.mem_decay(41), 0.85)
        self.assertEqual(tactics.mem_decay(81), 0.70)

    def test_urgency_ramp_is_monotonic(self):
        values = [tactics.urgency(tick) for tick in (0, 199, 200, 249, 250, 299, 300)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 3.5)

    def test_death_probability_is_clamped(self):
        low = tactics.death_probability(hp=100, n_enemy_adjacent=1)
        self.assertAlmostEqual(low, 0.10)
        high = tactics.death_probability(hp=10, n_enemy_adjacent=3, vulnerable=True)
        self.assertLessEqual(high, 0.95)
        healed = tactics.death_probability(hp=10, n_enemy_adjacent=1, in_heal=True)
        self.assertLess(healed, high)

    def test_escape_probability_table(self):
        self.assertEqual(tactics.escape_probability("pinned"), 0.15)
        self.assertEqual(tactics.escape_probability("in_aoe"), 0.10)
        self.assertEqual(tactics.escape_probability("unknown-situation"),
                         tactics.ESCAPE_TABLE["axis_free"])

    def test_resource_utility_prefers_value_and_near(self):
        near_normal = tactics.resource_utility(10, 2)
        far_normal = tactics.resource_utility(10, 8)
        self.assertGreater(near_normal, far_normal)
        self.assertGreater(tactics.resource_utility(20, 4), tactics.resource_utility(10, 4))
        # 同一格（dist=0）不除零
        self.assertGreater(tactics.resource_utility(10, 0), 0)

    def test_headroom_saturates_after_cap(self):
        self.assertGreater(tactics.resource_headroom(0), tactics.resource_headroom(60))
        self.assertAlmostEqual(tactics.resource_headroom(120), tactics.Params().tie_residual)
        self.assertAlmostEqual(tactics.resource_headroom(500), tactics.Params().tie_residual)
        self.assertGreater(tactics.damage_headroom(400), tactics.damage_headroom(500))
        off = tactics.Params(use_headroom=False)
        self.assertEqual(tactics.resource_headroom(500, off), 1.0)

    def test_stage2_score_matches_rules(self):
        # §10.2：30×min(R2/120,1) + 15×min(D/500,1) + 5×M
        self.assertAlmostEqual(tactics.stage2_score(120, 500, 3), 60.0)
        self.assertAlmostEqual(tactics.stage2_score(0, 0, 0), 0.0)
        self.assertAlmostEqual(tactics.stage2_score(60, 250, 1), 15.0 + 7.5 + 5.0)
        self.assertAlmostEqual(tactics.stage2_score(999, 9999, 3), 60.0)

    def test_decision_type_deltas(self):
        economy = tactics.params_for("economy")
        turtle = tactics.params_for("turtle")
        pressure = tactics.params_for("pressure")
        self.assertAlmostEqual(economy.engage_th, 0.35)
        self.assertAlmostEqual(pressure.w_dmg, 0.80 * 1.90)      # 权重是乘数
        self.assertAlmostEqual(pressure.engage_th, 0.15)         # 阈值是绝对值
        self.assertGreater(turtle.flee_hp, economy.flee_hp)
        self.assertGreater(turtle.engage_th, economy.engage_th)
        # 旧登记名必须还能用（封存前的兼容）
        self.assertEqual(tactics.params_for("collect").w_dmg, turtle.w_dmg)

    def test_war_phase_requires_min_tick_and_dry(self):
        params = tactics.Params()
        self.assertFalse(tactics.is_war_phase(119, 100, params))
        self.assertFalse(tactics.is_war_phase(200, params.dry_ticks - 1, params))
        self.assertTrue(tactics.is_war_phase(200, params.dry_ticks, params))
        self.assertTrue(tactics.is_war_phase(130, 1, params, resource_value_self=100))

    def test_contention_and_vacated_cells(self):
        moves = {"A": (1, 1), "B": (1, 1), "C": (2, 2), "D": None}
        self.assertEqual(tactics.contested_cells(moves), {(1, 1)})
        self.assertEqual(tactics.vacated_cells({"A": (1, 1), "B": None}), {(1, 1)})


class TestPlanner(unittest.TestCase):
    def test_field_gives_distance_and_first_step(self):
        field = build_field((0, 0), {(1, 0)}, 5, 5)
        self.assertEqual(field.d((0, 0)), 0)
        self.assertEqual(field.d((1, 0)), None)      # 被堵
        self.assertEqual(field.step((0, 2)), "down")

    def test_moves_towards_resource(self):
        planner = Planner()
        action = planner.plan(make_view(resources=[((9, 5), 10, 10)]))
        self.assertEqual(action["action"], "move")
        self.assertEqual(action["direction"], "right")

    def test_never_steps_into_danger_cell(self):
        planner = Planner()
        action = planner.plan(make_view(pos=(5, 5), resources=[((5, 9), 10, 10)],
                                        avoid_cells={(5, 6), (5, 7), (5, 8), (5, 9)}))
        self.assertNotEqual(action, None)
        self.assertNotEqual(action.get("direction"), "down")

    def test_dead_robot_waits(self):
        planner = Planner()
        action = planner.plan(make_view(alive=False))
        self.assertEqual(action, {"action": "wait", "reason": "dead"})

    def test_protected_robot_never_attacks(self):
        planner = Planner()
        enemy = {"hp": 20, "protected": False, "defending": False}
        view = make_view(pos=(5, 5), enemies=[(enemy, (6, 5), 10, None)], protected=True,
                         params=tactics.Params(engage_th=0.0))
        self.assertNotEqual((planner.plan(view) or {}).get("action"), "attack")

    def test_protected_enemy_is_not_a_target(self):
        planner = Planner()
        enemy = {"hp": 20, "protected": True, "defending": False}
        view = make_view(pos=(5, 5), enemies=[(enemy, (6, 5), 10, None)],
                         params=tactics.Params(engage_th=0.0))
        self.assertNotEqual((planner.plan(view) or {}).get("action"), "attack")

    def test_pinned_target_gets_attacked_when_worth_it(self):
        # 目标只剩 1 个可退格 → 逃逸概率 0.15，一轮交火收益超过阈值
        blocked = {(6, 4), (6, 6), (7, 5)}          # 只剩 (5,5) 一个出口 → 被夹住
        enemy = {"hp": 40, "protected": False, "defending": False}
        planner = Planner()
        view = make_view(pos=(5, 5), blocked=blocked,
                         enemies=[(enemy, (6, 5), 10, None)])
        action = planner.plan(view)
        self.assertEqual(action["action"], "attack")
        self.assertEqual(action["direction"], "right")

    def test_free_running_target_is_not_chased_without_pursue(self):
        enemy = {"hp": 100, "protected": False, "defending": False}
        planner = Planner()
        view = make_view(pos=(5, 5), enemies=[(enemy, (12, 5), 10, "right")])
        action = planner.plan(view)
        self.assertNotEqual((action or {}).get("action"), "attack")

    def test_contention_prediction_avoids_occupied_next_cell(self):
        # 敌车在 (5,4) 上，上一 Tick 方向 down → 下一 Tick 目标 (5,5)=我脚下；改成预测它的下一个格
        enemy = {"hp": 100, "protected": False, "defending": False}
        planner = Planner()
        view = make_view(pos=(5, 5), resources=[((5, 9), 10, 10)],
                         enemies=[(enemy, (5, 4), 10, "down")])
        action = planner.plan(view)
        self.assertEqual(action["action"], "move")
        self.assertNotEqual(action["direction"], "up")

    def test_far_reach_is_not_chased_early(self):
        planner = Planner()
        view = make_view(pos=(1, 1), reach_target=(12, 12), tick=10,
                         resources=[((3, 1), 10, 10)])
        action = planner.plan(view)
        self.assertEqual(action["reason"].split(":")[0], "resource")

    def test_reach_is_chased_in_war_phase(self):
        planner = Planner()
        view = make_view(pos=(1, 1), reach_target=(12, 12), tick=200, war_phase=True)
        action = planner.plan(view)
        self.assertEqual(action["reason"].split(":")[0], "mission")

    def test_no_candidate_in_economy_returns_none(self):
        # 经济期没有目标就把选择权交回 agent 的探索器（那里才有 explored 记忆）
        planner = Planner()
        self.assertIsNone(planner.plan(make_view(tick=10)))

    def test_hunt_only_in_war_phase(self):
        planner = Planner()
        action = planner.plan(make_view(tick=300, dry_ticks=40, war_phase=True))
        self.assertIsNotNone(action)
        self.assertEqual(action["reason"], "hunt")

    def test_high_value_tolerance_ratio(self):
        # 高价值 4 格、普通 1 格：4/1 = 4 > 2.0 → 选普通
        planner = Planner()
        view = make_view(pos=(5, 5), resources=[((9, 5), 20, 10), ((6, 5), 10, 10)])
        action = planner.plan(view)
        self.assertEqual(action["direction"], "right")   # 先走一步没问题
        self.assertIn("(6, 5)", action["reason"])


class ScriptedBrain:
    """按脚本给动作的大脑：用来单独验证仿真器的结算规则。"""

    def __init__(self, script):
        self.script = script
        self.tick = 0
        self.seen = []

    def on_perception(self, data):
        self.seen.append(data["tick"])
        action = self.script(data["tick"])
        payload = {"tick": data["tick"], "session_id": data["session_id"]}
        payload.update(action)
        return payload


def wait_action(_tick):
    return {"action": "wait"}


def one_tick_map(**kwargs):
    return Stage2Map(8, 8, obstacles=set(), resources=[], spawns={
        "A": (1, 1), "B": (3, 1), "C": (1, 3), "D": (5, 5)}, reach=(4, 4), **kwargs)


class TestStage2Sim(unittest.TestCase):
    def test_action_needs_current_tick_and_session(self):
        brain = ScriptedBrain(lambda tick: {"action": "move", "direction": "right"})
        wrong = ScriptedBrain(lambda tick: {"action": "attack", "direction": "up"})
        sim = Stage2Simulation(one_tick_map(), {"A": brain}, ticks=1)
        result = sim.run()
        self.assertEqual(result["seats"]["A"]["invalid_actions"], 0)

    def test_dead_robot_cannot_act(self):
        dead = ScriptedBrain(lambda tick: {"action": "move", "direction": "right"})
        sim = Stage2Simulation(one_tick_map(), {"A": dead}, ticks=3)
        sim.robots["A"].alive = False
        sim.robots["A"].dead_until = 99
        result = sim.run()
        self.assertEqual(result["seats"]["A"]["invalid_actions"], 3)
        self.assertEqual(result["seats"]["A"]["moves"], 0)

    def test_attack_uses_pre_move_positions_and_defend_halves(self):
        map_ = Stage2Map(8, 8, obstacles=set(), resources=[],
                         spawns={"A": (1, 1), "B": (2, 1), "C": (6, 6), "D": (7, 7)},
                         reach=(4, 4))
        attacker = ScriptedBrain(lambda tick: {"action": "attack", "direction": "right"})
        defender = ScriptedBrain(lambda tick: {"action": "defend"})
        sim = Stage2Simulation(map_, {"A": attacker, "B": defender}, ticks=1)
        result = sim.run()
        self.assertEqual(result["seats"]["A"]["damage"], float(DEFENDED_DAMAGE))
        self.assertEqual(result["seats"]["B"]["damage"], 0.0)

    def test_protected_target_blocks_line_of_fire(self):
        map_ = Stage2Map(8, 8, obstacles=set(), resources=[],
                         spawns={"A": (1, 1), "B": (2, 1), "C": (6, 6), "D": (7, 7)},
                         reach=(4, 4))
        attacker = ScriptedBrain(lambda tick: {"action": "attack", "direction": "right"})
        sim = Stage2Simulation(map_, {"A": attacker, "B": ScriptedBrain(wait_action)}, ticks=1)
        sim.robots["B"].protected_until = 5          # 出生保护挡住后面
        result = sim.run()
        self.assertEqual(result["seats"]["A"]["damage"], 0.0)

    def test_movement_cannot_follow_vacated_cell(self):
        map_ = Stage2Map(8, 8, obstacles=set(), resources=[],
                         spawns={"A": (1, 1), "B": (2, 1), "C": (6, 6), "D": (7, 7)},
                         reach=(4, 4))
        # A 向右、B 向右：A 想进 B 现在占的格 → 必须失败（不能跟进腾空格）
        right = ScriptedBrain(lambda tick: {"action": "move", "direction": "right"})
        sim = Stage2Simulation(map_, {"A": right, "B": right}, ticks=1)
        result = sim.run()
        self.assertEqual(result["seats"]["A"]["moves"], 0)
        self.assertEqual(result["seats"]["A"]["blocked_moves"], 1)

    def test_contested_cell_makes_everyone_fail(self):
        map_ = Stage2Map(8, 8, obstacles=set(), resources=[], spawns={
            "A": (3, 3), "B": (3, 5), "C": (5, 3), "D": (5, 5)}, reach=(4, 4))
        to_center_down = ScriptedBrain(lambda tick: {"action": "move", "direction": "down"})
        to_center_up = ScriptedBrain(lambda tick: {"action": "move", "direction": "up"})
        sim = Stage2Simulation(map_, {"A": to_center_down, "B": to_center_up,
                                      "C": to_center_down, "D": to_center_down}, ticks=1)
        result = sim.run()
        self.assertEqual(result["seats"]["A"]["moves"], 0)
        self.assertEqual(result["seats"]["A"]["blocked_moves"], 1)

    def test_resource_collection_and_missions(self):
        map_ = Stage2Map(6, 6, obstacles=set(),
                         resources=[{"position": [2, 1], "value": 10},
                                    {"position": [1, 2], "value": 10},
                                    {"position": [2, 2], "value": 20}],
                         spawns={"A": (1, 1), "B": (5, 5)}, reach=(1, 1))
        walker = ScriptedBrain(lambda tick: {"action": "move",
                                             "direction": ("right", "down", "left", "up")[tick % 4]})
        sim = Stage2Simulation(map_, {"A": walker}, ticks=4)
        result = sim.run()
        seat = result["seats"]["A"]
        self.assertEqual(seat["resource_value"], 40)
        self.assertEqual(seat["missions"], 3)
        self.assertAlmostEqual(seat["score"], 30.0 * 40 / 120 + 5.0 * 3)

    def test_death_penalty_floors_at_zero(self):
        map_ = Stage2Map(6, 6, obstacles=set(), resources=[],
                         spawns={"A": (1, 1), "B": (2, 1)}, reach=(4, 4))
        attacker = ScriptedBrain(lambda tick: {"action": "attack", "direction": "right"})
        sim = Stage2Simulation(map_, {"A": attacker, "B": ScriptedBrain(wait_action)}, ticks=6)
        for _ in range(4):                             # 100 HP / 20 伤害 = 5 次
            pass
        result = sim.run()
        self.assertGreaterEqual(result["seats"]["A"]["damage"], 0.0)

    def test_simulation_is_deterministic(self):
        def play():
            map_ = Stage2Map.build(12, 12, obstacles=6, seed=7)
            brains = {"A": ScriptedBrain(lambda tick: {"action": "move", "direction": "right"}),
                      "B": ScriptedBrain(wait_action)}
            return Stage2Simulation(map_, brains, ticks=20).run()["seats"]
        self.assertEqual(play(), play())

    def test_unknown_action_is_rejected_without_crash(self):
        brain = ScriptedBrain(lambda tick: {"action": "teleport", "direction": "up"})
        sim = Stage2Simulation(one_tick_map(), {"A": brain}, ticks=2)
        result = sim.run()
        self.assertEqual(result["seats"]["A"]["moves"], 0)

    def test_brain_exception_is_counted_not_raised(self):
        class Boom:
            def on_perception(self, data):
                raise RuntimeError("boom")
        sim = Stage2Simulation(one_tick_map(), {"A": Boom()}, ticks=2)
        result = sim.run()
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(result["seats"]["A"]["moves"], 0)

    def test_missions_are_the_registered_three(self):
        self.assertEqual([m["id"] for m in MISSIONS],
                         ["collect_two_normal", "collect_high_value", "reach_target"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
