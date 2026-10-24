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
        self.assertIn(blocked_cell, agent.known_obstacles)
        self.assertIn(blocked_cell, agent.strategy.blocked)

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
