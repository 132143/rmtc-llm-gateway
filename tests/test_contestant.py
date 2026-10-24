"""参赛代码的测试：模拟器 + 策略 + Agent（感知→动作）。

跑法：python3 run_tests.py -v tests.test_contestant
"""

from __future__ import annotations

import json
import os
import unittest

from contestant.agent import Agent, AgentConfig, decode_relative
from contestant.sim import Stage1Map, run_stage1
from contestant.strategy import GreedyBfsStrategy, SnakeStrategy, step_toward

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRACTICE_CONFIG = os.path.join(ROOT, "docs", "practice_stage1.json")


def load_practice_map():
    with open(PRACTICE_CONFIG, encoding="utf-8") as handle:
        return Stage1Map.from_config(json.load(handle))


def grid(dx, dy):
    """按协议生成 9×9 相对位置字符串：'1' 在中心，'2' 在 (dx, dy)。"""
    rows = [["0"] * 9 for _ in range(9)]
    rows[4][4] = "1"
    rows[4 + dy][4 + dx] = "2"
    return "\n".join("".join(row) for row in rows)


def perception(tick=0, session="m1", pos=(8, 8), alive=True, resources=(), robots=(),
               missions=None, width=18, height=18, protected=False, hp=100):
    entities = []
    for (dx, dy), value in resources:
        entities.append({"type": "resource_high" if value >= 20 else "resource_normal",
                         "id": f"res{dx}_{dy}", "position": grid(dx, dy), "value": value})
    for (dx, dy) in robots:
        entities.append({"type": "robot", "id": "robot_B", "team": "B", "robot_id": "B",
                         "position": grid(dx, dy), "hp": 60})
    return {"tick": tick, "session_id": session, "vision_radius": 4,
            "self": {"team": "A", "robot_id": "A", "position": list(pos), "hp": hp,
                     "alive": alive, "protected": protected, "defending": False,
                     "respawn_in": 0, "last_action": None},
            "visible_entities": entities, "map": {"width": width, "height": height},
            "missions": missions or [], "mission_text": "", "event_text": ""}


class TestSimulatorAndStrategy(unittest.TestCase):
    def test_practice_map_greedy_beats_snake(self):
        game_map = load_practice_map()
        greedy = run_stage1(game_map, GreedyBfsStrategy(game_map.width, game_map.height))
        snake = run_stage1(game_map, SnakeStrategy(game_map.width, game_map.height))
        self.assertEqual(greedy["score"], 40.0)
        self.assertLess(snake["score"], greedy["score"])

    def test_no_oscillation_regression(self):
        """踩过的坑：采到资源后记忆不清空，会在两格之间来回抖，整局只拿 10 分。"""
        game_map = load_practice_map()
        result = run_stage1(game_map, GreedyBfsStrategy(game_map.width, game_map.height))
        self.assertGreaterEqual(result["collected"], 15)

    def test_step_toward_goes_around_wall(self):
        blocked = {(1, 0), (1, 1)}
        direction = step_toward((0, 0), (2, 0), blocked, 5, 5)
        self.assertIn(direction, ("up", "down"))

    def test_random_map_is_seeded(self):
        first = Stage1Map.random(seed=7)
        second = Stage1Map.random(seed=7)
        self.assertEqual(sorted(first.resources.items()), sorted(second.resources.items()))

    def test_decode_relative(self):
        self.assertEqual(decode_relative(grid(1, -2)), (1, -2))
        # 与自己同格时图里没有 '2'：协议规定中心保留 '1'
        no_target = "\n".join("000010000" if row == 4 else "000000000" for row in range(9))
        self.assertEqual(decode_relative(no_target), (0, 0))
        self.assertIsNone(decode_relative("坏了"))


class TestAgent(unittest.TestCase):
    def make_agent(self, stage=1, **cfg):
        # 这些用例专门验证"进入射程就打"的动作格式，所以固定用 opportunistic；
        # 默认模式是 resource_first（先采集再打），它的行为有单独用例。
        cfg.setdefault("combat_mode", "opportunistic")
        return Agent(AgentConfig(seat="A", stage=stage, **cfg))

    def test_first_perception_produces_valid_action(self):
        agent = self.make_agent()
        action = agent.on_perception(perception(tick=0, resources=(((1, 0), 10),)))
        self.assertIsNotNone(action)
        self.assertEqual(action["tick"], 0)
        self.assertEqual(action["session_id"], "m1")
        self.assertIn(action["action"], ("move", "attack", "defend", "wait"))
        if action["action"] == "move":
            self.assertIn(action["direction"], ("up", "down", "left", "right"))

    def test_duplicate_tick_is_ignored(self):
        agent = self.make_agent()
        self.assertIsNotNone(agent.on_perception(perception(tick=5)))
        self.assertIsNone(agent.on_perception(perception(tick=5)))
        self.assertIsNone(agent.on_perception(perception(tick=4)))

    def test_new_session_resets_state(self):
        agent = self.make_agent()
        agent.on_perception(perception(tick=50, session="match-1"))
        self.assertEqual(agent.last_tick, 50)
        self.assertIsNotNone(agent.on_perception(perception(tick=0, session="match-2")))
        self.assertEqual(agent.session_id, "match-2")
        self.assertEqual(agent.last_tick, 0)

    def test_dead_robot_only_waits(self):
        agent = self.make_agent()
        action = agent.on_perception(perception(alive=False, resources=(((1, 0), 10),)))
        self.assertEqual(action["action"], "wait")

    def test_malformed_perception_does_not_crash(self):
        agent = self.make_agent()
        self.assertIsNone(agent.on_perception(None))
        self.assertIsNone(agent.on_perception({"session_id": "x"}))
        self.assertIsNone(agent.on_perception({"tick": "3", "session_id": "x"}))
        weird = perception(tick=1)
        weird["visible_entities"] = [{"type": "robot", "position": 12345}, "文字", None]
        self.assertIsNotNone(agent.on_perception(weird))

    def test_stage2_attacks_enemy_in_line(self):
        agent = self.make_agent(stage=2)
        action = agent.on_perception(perception(tick=3, robots=((2, 0),)))
        self.assertEqual(action["action"], "attack")
        self.assertEqual(action["direction"], "right")

    def test_stage1_never_attacks(self):
        agent = self.make_agent(stage=1)
        action = agent.on_perception(perception(tick=3, robots=((1, 0),)))
        self.assertNotEqual(action["action"], "attack")

    def test_protected_robot_does_not_attack(self):
        agent = self.make_agent(stage=2)
        action = agent.on_perception(perception(tick=3, robots=((1, 0),), protected=True))
        self.assertNotEqual(action["action"], "attack")

    def test_moves_towards_visible_resource(self):
        agent = self.make_agent()
        action = agent.on_perception(perception(tick=0, pos=(8, 8), resources=(((0, -3), 10),)))
        self.assertEqual(action["action"], "move")
        self.assertEqual(action["direction"], "up")

    def test_explored_grows_with_vision_window(self):
        agent = self.make_agent()
        agent.on_perception(perception(tick=0, pos=(8, 8)))
        self.assertEqual(len(agent.explored), 81)          # 9×9 视野窗口
        agent.on_perception(perception(tick=1, pos=(8, 8)))
        self.assertGreaterEqual(len(agent.explored), 81)

    def test_blocked_is_learned_from_last_action(self):
        """真机实测踩到的坑：撞墙回执没有回灌给策略，于是反复撞同一堵墙。"""
        agent = self.make_agent()
        first = agent.on_perception(perception(tick=0, pos=(1, 1), resources=(((4, 0), 10),)))
        self.assertEqual(first["action"], "move")
        direction = first["direction"]
        dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}[direction]
        blocked_cell = (1 + dx, 1 + dy)
        # 下一帧：位置没动，回执说 E_BLOCKED
        stuck = perception(tick=1, pos=(1, 1), resources=(((4, 0), 10),))
        stuck["self"]["last_action"] = {"tick": 0, "action": "move", "moved": False,
                                        "error": "E_BLOCKED"}
        agent.on_perception(stuck)
        # E_BLOCKED 可能是别的车占位：记成"临时堵塞"（8 Tick 后过期），不是永久障碍
        self.assertIn(blocked_cell, agent.transient_blocked)
        self.assertIn(blocked_cell, agent._blocked_now())
        # 过期后应当重新可以走
        later = perception(tick=40, pos=(1, 1), resources=(((4, 0), 10),))
        agent.on_perception(later)
        self.assertNotIn(blocked_cell, agent._blocked_now())

    def test_exploration_does_not_stall_at_start(self):
        """没有资源可见时必须往外探索，不能原地 wait。"""
        agent = self.make_agent()
        seen_positions = {(1, 1)}
        pos = (1, 1)
        for tick in range(12):
            action = agent.on_perception(perception(tick=tick, pos=pos))
            if action["action"] == "move":
                dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0),
                          "right": (1, 0)}[action["direction"]]
                pos = (pos[0] + dx, pos[1] + dy)
                seen_positions.add(pos)
        self.assertGreaterEqual(len(seen_positions), 6)

    def test_ignores_enemy_not_in_line(self):
        agent = self.make_agent(stage=2)
        action = agent.on_perception(perception(tick=3, robots=((1, 1),)))
        self.assertNotEqual(action["action"], "attack")

    def test_resource_first_holds_fire_below_threshold(self):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="resource_first",
                                  resource_threshold=120))
        frame = perception(tick=3, robots=((1, 0),))
        frame["score"] = {"resource_value": 40, "effective_damage": 0.0,
                          "mission_completed": 0, "invalid_actions": 0}
        action = agent.on_perception(frame)
        self.assertNotEqual(action["action"], "attack")

    def test_resource_first_attacks_after_threshold(self):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="resource_first",
                                  resource_threshold=120))
        frame = perception(tick=3, robots=((1, 0),))
        frame["score"] = {"resource_value": 120, "effective_damage": 0.0,
                          "mission_completed": 1, "invalid_actions": 0}
        action = agent.on_perception(frame)
        self.assertEqual(action["action"], "attack")
        self.assertEqual(action["direction"], "right")

    def test_low_hp_defends_even_before_threshold(self):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="resource_first",
                                  resource_threshold=120, low_hp=30))
        frame = perception(tick=3, pos=(8, 8), robots=((2, 0),), hp=20)
        frame["score"] = {"resource_value": 0}
        action = agent.on_perception(frame)
        self.assertEqual(action["action"], "defend")

    def test_trace_file_records_each_decision(self):
        import json as jsonlib
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trace-test.jsonl")
        if os.path.exists(path):
            os.remove(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        agent = Agent(AgentConfig(seat="A", stage=1), trace_path=path)
        agent.on_perception(perception(tick=0, pos=(8, 8)))
        agent.on_perception(perception(tick=1, pos=(8, 8)))
        with open(path, encoding="utf-8") as handle:
            lines = [jsonlib.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["tick"], 0)
        self.assertIn(lines[0]["action"], ("move", "attack", "defend", "wait"))

    def test_protected_enemy_is_not_attacked(self):
        """规则 §6.2：最近的敌人处于出生保护时，它挡住后面目标且自身伤害为 0 —— 打了纯浪费。"""
        agent = self.make_agent(stage=2)
        frame = perception(tick=3, robots=((1, 0),))
        frame["visible_entities"][0]["protected"] = True
        action = agent.on_perception(frame)
        self.assertNotEqual(action["action"], "attack")

    def test_ai_heartbeat_fires_without_events(self):
        """规则 §10.2：本场没有一次真实 AI 成功调用 → 任务分 M=0，所以必须主动发一次心跳。"""
        agent = self.make_agent(stage=2)
        offers = []

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                offers.append(kwargs.get("event_key"))
                return True

            def hint(self, *args, **kwargs):
                return None

        agent.on_perception(perception(tick=0))          # 先用真实流程建立场次
        agent.understanding = FakeUnderstanding()        # 再换成假的理解器，避免被 _start_session 覆盖
        agent.client = type("C", (), {"ok_calls": 0})()
        agent.on_perception(perception(tick=1))          # 还没到心跳 Tick
        self.assertEqual(offers, [])
        agent.on_perception(perception(tick=3))          # 到点亮心跳
        self.assertEqual(offers, ["__heartbeat__"])

    def test_avoid_cells_makes_robot_leave_danger(self):
        """世界分裂/火爆辣椒是"整行整列"，模型会列出格子；本车在里面时必须先走开。"""
        agent = self.make_agent(stage=2)

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                return True

            def hint(self, *args, **kwargs):
                return {"parsed": {"avoid_cells": [[8, 8], [8, 7], [8, 9], [7, 8], [9, 8]],
                                   "avoid_until_tick": 400, "danger_level": "fatal"},
                        "advice": "十字线致命", "tick": 1, "session_id": "m1"}

        agent.understanding = FakeUnderstanding()
        action = agent.on_perception(perception(tick=10, pos=(8, 8)))
        self.assertEqual(action["action"], "move")
        self.assertIn(action["direction"], ("up", "down", "left", "right"))
        self.assertGreaterEqual(agent.effects_applied, 1)

    def test_damage_level_ignored_at_full_hp(self):
        """实测教训：为躲 30 HP 的辣椒线反复横跳，会被对手抢光资源（45 → 32.5）。"""
        agent = self.make_agent(stage=2)

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                return True

            def hint(self, *args, **kwargs):
                return {"parsed": {"avoid_cells": [[8, 8]], "avoid_until_tick": 400,
                                   "danger_level": "damage"},
                        "advice": "掉血", "tick": 1, "session_id": "m1"}

        agent.understanding = FakeUnderstanding()
        action = agent.on_perception(perception(tick=10, pos=(8, 8), resources=(((0, -2), 10),), hp=100))
        self.assertEqual(action["direction"], "up")      # 血满：照常捡资源

    def test_damage_level_dodged_when_hp_low(self):
        agent = self.make_agent(stage=2)

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                return True

            def hint(self, *args, **kwargs):
                return {"parsed": {"avoid_cells": [[8, 8]], "avoid_until_tick": 400,
                                   "danger_level": "damage"},
                        "advice": "掉血", "tick": 1, "session_id": "m1"}

        agent.understanding = FakeUnderstanding()
        action = agent.on_perception(perception(tick=10, pos=(8, 8), hp=40))
        self.assertEqual(action["action"], "move")

    def test_missing_danger_level_ignored(self):
        agent = self.make_agent(stage=2)

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                return True

            def hint(self, *args, **kwargs):
                return {"parsed": {"avoid_cells": [[8, 8]], "avoid_until_tick": 400},
                        "advice": "没说危险等级", "tick": 1, "session_id": "m1"}

        agent.understanding = FakeUnderstanding()
        action = agent.on_perception(perception(tick=10, pos=(8, 8), resources=(((2, 0), 10),)))
        self.assertEqual(action["direction"], "right")

    def test_avoid_cells_ignored_when_outside(self):
        agent = self.make_agent(stage=2)

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                return True

            def hint(self, *args, **kwargs):
                return {"parsed": {"avoid_cells": [[2, 2]], "avoid_until_tick": 400},
                        "advice": "不在危险区", "tick": 1, "session_id": "m1"}

        agent.understanding = FakeUnderstanding()
        action = agent.on_perception(perception(tick=10, pos=(8, 8), resources=(((0, -2), 10),)))
        self.assertEqual(action["direction"], "up")     # 正常去捡资源

    def test_expired_avoid_cells_ignored(self):
        agent = self.make_agent(stage=2)

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                return True

            def hint(self, *args, **kwargs):
                return {"parsed": {"avoid_cells": [[8, 8]], "avoid_until_tick": 5},
                        "advice": "已过期", "tick": 1, "session_id": "m1"}

        agent.understanding = FakeUnderstanding()
        action = agent.on_perception(perception(tick=10, pos=(8, 8), resources=(((2, 0), 10),)))
        self.assertEqual(action["direction"], "right")

    def test_unfinished_high_value_mission_is_chased(self):
        """任务"至少 1 个高价值资源"值 5 分；没完成时要优先去高价值资源，而不是最近的普通资源。"""
        agent = self.make_agent(stage=2)
        frame = perception(tick=10, pos=(8, 8), resources=(((1, 0), 10), ((4, 0), 20)),
                           missions=[{"id": "collect_high_value", "type": "high_resources", "target": 1},
                                     {"id": "collect_two_normal", "type": "normal_resources", "target": 2}])
        frame["score"] = {"resource_value": 0, "mission_completed_ids": []}
        action = agent.on_perception(frame)
        self.assertEqual(action["action"], "move")
        self.assertEqual(action["direction"], "right")     # 高价值在右边 4 格
        self.assertGreaterEqual(agent.mission_moves, 1)

    def test_far_mission_resource_is_not_chased(self):
        """大地图实测：为了 5 分的任务跑半张图会把开局抢资源拖慢（首个资源 19 → 12 Tick）。"""
        agent = Agent(AgentConfig(seat="A", stage=2, mission_max_distance=4))
        agent.on_perception(perception(tick=0, pos=(8, 8)))          # 建立场次与地图
        # 记忆里有一个 9 格外的 20 分值资源（视野外也能记住）
        agent.strategy.seen_resources[(17, 8)] = 20
        frame = perception(tick=10, pos=(8, 8), resources=(((1, 0), 10),),
                           missions=[{"id": "collect_high_value", "type": "high_resources", "target": 1}])
        frame["score"] = {"resource_value": 0, "mission_completed_ids": []}
        action = agent.on_perception(frame)
        # 高价值在 9 格外（超过门限 4），应改去右边 1 格的普通资源
        self.assertEqual(action["direction"], "right")

    def test_finished_mission_does_not_hijack(self):
        agent = self.make_agent(stage=2)
        frame = perception(tick=10, pos=(8, 8), resources=(((4, 0), 20),),
                           missions=[{"id": "collect_high_value", "type": "high_resources", "target": 1}])
        frame["score"] = {"resource_value": 20, "mission_completed_ids": ["collect_high_value"]}
        agent.on_perception(frame)
        self.assertEqual(agent.mission_moves, 0)

    def test_illusion_reverses_move_only(self):
        """规则 §8.3：幻术让 MOVE 上下左右互换，ATTACK 方向不反转。"""
        agent = self.make_agent(stage=2)

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                return True

            def hint(self, *args, **kwargs):
                return {"parsed": {"direction_reversed": True, "reversed_until_tick": 500},
                        "advice": "方向被颠倒", "tick": 1, "session_id": "m1"}

        agent.understanding = FakeUnderstanding()
        # 资源在正上方 → 正常应走 up；幻术生效时应变成 down
        action = agent.on_perception(perception(tick=2, pos=(8, 8), resources=(((0, -3), 10),)))
        self.assertEqual(action["action"], "move")
        self.assertEqual(action["direction"], "down")
        self.assertGreaterEqual(agent.effects_applied, 1)

    def test_expired_illusion_does_not_reverse(self):
        agent = self.make_agent(stage=2)

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                return True

            def hint(self, *args, **kwargs):
                return {"parsed": {"direction_reversed": True, "reversed_until_tick": 1},
                        "advice": "已过期", "tick": 1, "session_id": "m1"}

        agent.understanding = FakeUnderstanding()
        action = agent.on_perception(perception(tick=5, pos=(8, 8), resources=(((0, -3), 10),)))
        self.assertEqual(action["direction"], "up")

    def test_threat_zone_triggers_escape(self):
        agent = self.make_agent(stage=2)

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                return True

            def hint(self, *args, **kwargs):
                return {"parsed": {"threat_zone": [[6, 6], [10, 10]], "affects_me": True},
                        "advice": "撤离", "tick": 1, "session_id": "m1"}

        agent.understanding = FakeUnderstanding()
        action = agent.on_perception(perception(tick=1, pos=(8, 8)))
        self.assertEqual(action["action"], "move")

    def _buff_agent(self, until=400):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid"))

        class FakeUnderstanding:
            def offer(self, *args, **kwargs):
                return True

            def hint(self, *args, **kwargs):
                return {"parsed": {"buff_zone": [[8, 8], [10, 10]], "buff_until_tick": until},
                        "advice": "去堡垒", "tick": 1, "session_id": "m1"}

        agent.understanding = FakeUnderstanding()
        return agent

    def test_buff_zone_attacks_enemy_within_5x5(self):
        """规则 §8.5：堡垒增益下 ATTACK 命中 5×5 内全部敌人，不再要求同排同列。"""
        agent = self._buff_agent()
        action = agent.on_perception(perception(tick=5, pos=(9, 9), robots=((1, 1),)))
        self.assertEqual(action["action"], "attack")
        self.assertGreaterEqual(agent.buff_attacks, 1)

    def test_buff_zone_waits_inside_without_enemy(self):
        agent = self._buff_agent()
        action = agent.on_perception(perception(tick=5, pos=(9, 9)))
        self.assertEqual(action["action"], "wait")

    def test_buff_zone_moves_in_when_outside(self):
        agent = self._buff_agent()
        action = agent.on_perception(perception(tick=5, pos=(3, 3)))
        self.assertEqual(action["action"], "move")
        self.assertGreaterEqual(agent.buff_moves, 1)

    def test_buff_zone_skipped_when_resources_nearby(self):
        """资源 30 分 > 对抗 15 分：附近有资源就不去蹲堡垒。"""
        agent = self._buff_agent()
        action = agent.on_perception(perception(tick=5, pos=(3, 3), resources=(((1, 0), 10),)))
        self.assertEqual(action["action"], "move")
        self.assertEqual(agent.buff_moves, 0)

    def test_buff_zone_skipped_when_damage_already_high(self):
        agent = self._buff_agent()
        frame = perception(tick=5, pos=(9, 9), robots=((1, 1),))
        frame["score"] = {"resource_value": 0, "effective_damage": 450, "mission_completed": 0}
        action = agent.on_perception(frame)
        self.assertEqual(agent.buff_attacks, 0)

    def test_expired_buff_zone_is_ignored(self):
        agent = self._buff_agent(until=2)
        action = agent.on_perception(perception(tick=50, pos=(9, 9), robots=((1, 1),)))
        self.assertNotEqual(action["action"], "attack")   # 增益过期，回到普通规则

    def test_opening_high_first_prefers_value(self):
        """开局抢高地：视野里 20 分资源比更近的 10 分资源优先。"""
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid",
                                  opening_mode="high_first", opening_ticks=30))
        frame = perception(tick=2, pos=(8, 8), resources=(((1, 0), 10), ((0, 4), 20)))
        action = agent.on_perception(frame)
        self.assertEqual(action["direction"], "down")     # 20 分在下 4 格

    def test_opening_nearest_skips_mission(self):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid",
                                  opening_mode="nearest", opening_ticks=30))
        agent.on_perception(perception(tick=0, pos=(8, 8)))
        agent.strategy.seen_resources[(17, 8)] = 20       # 远处高价值任务资源
        frame = perception(tick=2, pos=(8, 8), resources=(((1, 0), 10),),
                           missions=[{"id": "collect_high_value", "type": "high_resources", "target": 1}])
        frame["score"] = {"resource_value": 0, "mission_completed_ids": []}
        action = agent.on_perception(frame)
        self.assertEqual(action["direction"], "right")    # 就近

    def test_reach_mission_beats_hunting(self):
        """镜像局实测：追击会把 reach 任务挤掉，白丢 5 分。到点后必须优先去任务坐标。"""
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid",
                                  reach_after_tick=10, low_hp=30))
        frame = perception(tick=100, pos=(3, 3), robots=((2, 0),),
                           missions=[{"id": "reach_target", "type": "reach", "position": [9, 9]}],
                           hp=100)
        action = agent.on_perception(frame)
        self.assertEqual(action["action"], "move")
        # 朝 (9,9) 走（右下两个方向都算对；BFS 在自己的枚举顺序里挑第一个最短步）
        self.assertIn(action["direction"], ("right", "down"))
        self.assertGreaterEqual(agent.reach_moves, 1)

    def test_late_join_does_not_skip_collecting(self):
        """晚开局不提前冲任务点：四席环境晚 5~30 Tick 是常态，提前改去任务点会抢不到资源。"""
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid", reach_after_tick=250))
        mission = [{"id": "reach_target", "type": "reach", "position": [9, 9]}]
        agent.on_perception(perception(tick=30, pos=(3, 3), hp=100, missions=mission))
        self.assertTrue(agent.late_join)
        action = agent.on_perception(perception(tick=31, pos=(3, 3), hp=100,
                                                resources=(((1, 0), 10),), missions=mission))
        self.assertEqual(action["action"], "move")
        self.assertNotEqual(action.get("direction"), None)

    def test_early_reach_only_on_large_maps(self):
        """18×18（练习包）不提前去任务点；20×20 以上（正式赛尺寸）才提前。"""
        mission = [{"id": "reach_target", "type": "reach", "position": [9, 9]}]
        small = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid", reach_after_tick=250))
        small.on_perception(perception(tick=0, pos=(3, 3), hp=100, width=18, height=18,
                                       resources=(((1, 0), 10),), missions=mission))
        small.on_perception(perception(tick=1, pos=(4, 3), hp=100, width=18, height=18,
                                       missions=mission))
        small.explored = {(x, y) for x in range(18) for y in range(18)}
        small_action = small.on_perception(perception(tick=160, pos=(3, 3), hp=100,
                                                      width=18, height=18, missions=mission))
        self.assertFalse(small._is_large_map())

        big = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid", reach_after_tick=250))
        big.on_perception(perception(tick=0, pos=(3, 3), hp=100, width=24, height=24,
                                     resources=(((1, 0), 10),), missions=mission))
        big.on_perception(perception(tick=1, pos=(4, 3), hp=100, width=24, height=24,
                                     missions=mission))
        big.explored = {(x, y) for x in range(24) for y in range(24)}
        big_action = big.on_perception(perception(tick=160, pos=(3, 3), hp=100,
                                                  width=24, height=24, missions=mission))
        self.assertTrue(big._is_large_map())
        self.assertEqual(big_action["action"], "move")     # 大图：无资源可捡 → 去任务点
        # 小图：保持旧行为（全图探完、无资源时不硬冲任务点），这一帧是 wait
        self.assertEqual(small_action["action"], "wait")

    def test_no_resources_left_goes_for_reach(self):
        """见过资源、后来一个都不剩 → 立刻去任务点（而不是继续空搜）。"""
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid", reach_after_tick=250))
        mission = [{"id": "reach_target", "type": "reach", "position": [9, 9]}]
        agent.on_perception(perception(tick=0, pos=(3, 3), hp=100, width=24, height=24,
                                      resources=(((1, 0), 10),), missions=mission))
        agent.on_perception(perception(tick=1, pos=(4, 3), hp=100, width=24, height=24,
                                      missions=mission))  # 资源已捡走
        agent.explored = {(x, y) for x in range(24) for y in range(24)}
        # 大图 + tick ≥150 + 确实没资源 → 立刻去任务点
        action = agent.on_perception(perception(tick=160, pos=(3, 3), hp=100,
                                               width=24, height=24, missions=mission))
        self.assertEqual(action["action"], "move")

    def test_never_saw_resources_does_not_skip_searching(self):
        """从没见过资源 ≠ 没资源；这时不能提前放弃搜索去任务点（实测会把分数打到 3 分）。"""
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid", reach_after_tick=250))
        agent.on_perception(perception(tick=0, pos=(3, 3), hp=100))
        agent.explored = {(x, y) for x in range(18) for y in range(18)}
        frame = perception(tick=80, pos=(3, 3), hp=100,
                           missions=[{"id": "reach_target", "type": "reach", "position": [9, 9]}])
        agent.on_perception(frame)
        self.assertEqual(agent.resources_ever_seen, 0)

    def test_high_value_mission_uses_wider_gate_early(self):
        """只有 4 个高价值资源，抢不到就白丢 5 分：前期放宽到 12 格。"""
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid",
                                  mission_max_distance=8))
        agent.on_perception(perception(tick=0, pos=(8, 8)))
        agent.strategy.seen_resources[(18, 8)] = 20      # 10 格外（普通门限 8 会放弃）
        frame = perception(tick=20, pos=(8, 8),
                           missions=[{"id": "collect_high_value", "type": "high_resources", "target": 1}])
        frame["score"] = {"resource_value": 0, "mission_completed_ids": []}
        action = agent.on_perception(frame)
        self.assertEqual(action["action"], "move")
        self.assertEqual(action["direction"], "right")

    def test_reach_clears_occupant_by_attacking(self):
        """实测：任务点被别的车长期占住时，我们对着它撞了 50+ 次 E_BLOCKED —— 应该打掉占位者。"""
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid",
                                  reach_after_tick=10, low_hp=30))
        mission = [{"id": "reach_target", "type": "reach", "position": [9, 9]}]
        agent.on_perception(perception(tick=0, pos=(8, 9), hp=100, missions=mission))
        action = agent.on_perception(perception(tick=20, pos=(8, 9), hp=100,
                                                robots=((1, 0),), missions=mission))
        self.assertEqual(action["action"], "attack")
        self.assertEqual(action["direction"], "right")
        self.assertGreaterEqual(agent.reach_clear_attacks, 1)

    def test_defend_does_not_lock_the_robot(self):
        """真机踩过：血量长期 20 时连续 251 帧都在防御，任务与伤害全丢。"""
        agent = self.make_agent(stage=2, low_hp=30, reach_after_tick=10)
        actions = []
        for tick in range(6):
            frame = perception(tick=tick, pos=(3, 3), robots=((2, 0),), hp=20,
                               missions=[{"id": "reach_target", "type": "reach", "position": [9, 9]}])
            actions.append(agent.on_perception(frame)["action"])
        self.assertIn("defend", actions)
        self.assertTrue(any(a in ("move", "attack") for a in actions),
                        f"不能一直防御：{actions}")

    def test_reach_mission_defers_to_defend_when_low_hp(self):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid",
                                  reach_after_tick=10, low_hp=30))
        frame = perception(tick=100, pos=(3, 3), robots=((2, 0),),
                           missions=[{"id": "reach_target", "type": "reach", "position": [9, 9]}],
                           hp=20)
        action = agent.on_perception(frame)
        self.assertEqual(action["action"], "defend")

    def test_patrol_is_off_by_default(self):
        """负结果也要固定下来：巡逻实测不提分，默认关。"""
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid"))
        self.assertFalse(agent.cfg.patrol_when_idle)

    def test_patrol_when_idle_and_no_enemy(self):
        """大地图实测 277/500 Tick 在空等；空闲时应该往巡逻点走。"""
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid",
                                  patrol_when_idle=True))
        agent.on_perception(perception(tick=0, pos=(6, 6)))
        agent.explored = {(x, y) for x in range(18) for y in range(18)}
        action = agent.on_perception(perception(tick=1, pos=(6, 6)))     # 无资源、无敌人
        self.assertEqual(action["action"], "move")
        self.assertGreaterEqual(agent.patrol_moves, 1)

    def test_patrol_can_be_disabled(self):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid",
                                  patrol_when_idle=False))
        agent.on_perception(perception(tick=0, pos=(6, 6)))
        agent.explored = {(x, y) for x in range(18) for y in range(18)}
        action = agent.on_perception(perception(tick=1, pos=(6, 6)))
        self.assertEqual(action["action"], "wait")

    def test_hybrid_hunts_instead_of_waiting(self):
        """真机实测：资源被对手抢完后，程序原地 wait 了 415 Tick。hybrid 应该改为去压制对手。"""
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="hybrid"))
        agent.on_perception(perception(tick=0, pos=(13, 13)))     # 先建立场次与地图
        agent.explored = {(x, y) for x in range(18) for y in range(18)}   # 全图已探索
        action = agent.on_perception(perception(tick=1, pos=(13, 13), robots=((3, 1),)))
        self.assertEqual(action["action"], "move")
        self.assertIn(action["direction"], ("right", "down"))

    def test_finish_mode_only_attacks_weak_enemy(self):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="finish", finish_hp=60))
        healthy = perception(tick=3, robots=((1, 0),))
        healthy["visible_entities"][0]["hp"] = 90
        self.assertNotEqual(agent.on_perception(healthy)["action"], "attack")

        agent2 = Agent(AgentConfig(seat="A", stage=2, combat_mode="finish", finish_hp=60))
        weak = perception(tick=3, robots=((1, 0),))
        weak["visible_entities"][0]["hp"] = 40
        action = agent2.on_perception(weak)
        self.assertEqual(action["action"], "attack")
        self.assertEqual(action["direction"], "right")

    def test_snake_strategy_can_be_selected(self):
        agent = Agent(AgentConfig(seat="A", stage=1, strategy="snake"))
        agent.on_perception(perception(tick=0, pos=(8, 8)))
        self.assertEqual(type(agent.strategy).__name__, "SnakeStrategy")

    def test_combat_never_never_attacks(self):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="never"))
        action = agent.on_perception(perception(tick=3, robots=((1, 0),)))
        self.assertNotEqual(action["action"], "attack")

    def test_aggressive_chases_enemy_at_distance(self):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="aggressive",
                                  aggressive_range=4))
        # 敌人在 3 格外（同一行），直线攻击够不到，aggressive 应该主动靠近
        action = agent.on_perception(perception(tick=3, pos=(8, 8), robots=((3, 0),)))
        self.assertEqual(action["action"], "move")
        self.assertEqual(action["direction"], "right")


if __name__ == "__main__":
    unittest.main(verbosity=2)
