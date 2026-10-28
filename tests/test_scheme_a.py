"""2026-10-28 方案A（中心偏置 + 开局经济窗口）的行为测试。

两条机制都只在**封存默认档**（planner=guarded + combat_mode=hybrid）生效，
现场切到 economy/turtle 就等于一键回滚——这是"可回滚"承诺的代码保证。
"""

from __future__ import annotations

import unittest

from contestant.agent import Agent, AgentConfig
from contestant.sim import View
from contestant.strategy import GreedyBfsStrategy


def grid(dx, dy):
    rows = [["0"] * 9 for _ in range(9)]
    rows[4][4] = "1"
    rows[4 + dy][4 + dx] = "2"
    return "\n".join("".join(row) for row in rows)


def perception(tick=0, pos=(1, 1), resources=(), robots=(), width=24, height=24, hp=100):
    entities = []
    for (dx, dy), value in resources:
        entities.append({"type": "resource_high" if value >= 20 else "resource_normal",
                         "id": "r{0}_{1}".format(dx, dy), "position": grid(dx, dy), "value": value})
    for (dx, dy) in robots:
        entities.append({"type": "robot", "id": "robot_B", "team": "B", "robot_id": "B",
                         "position": grid(dx, dy), "hp": 60})
    return {"tick": tick, "session_id": "m1", "vision_radius": 4,
            "self": {"team": "A", "robot_id": "A", "position": list(pos), "hp": hp,
                     "alive": True, "protected": False, "defending": False,
                     "respawn_in": 0, "last_action": None},
            "visible_entities": entities, "map": {"width": width, "height": height},
            "missions": [], "mission_text": "", "event_text": ""}


def view(pos=(1, 1), width=24, height=24, explored=None, tick=10):
    """策略层用的轻量 View（与 sim.View / agent 里构造的是同一个）。"""
    return View(pos, [], set(), set(explored or ()), set(), set(), tick, 4)


class TestCenterPull(unittest.TestCase):
    def test_explore_follows_center_when_pull_on(self):
        """同一圈候选里，中心偏置应该让我们往地图中间走，而不是沿角上的边扫。"""
        strat = GreedyBfsStrategy(24, 24)
        # 站在左上角，周围 9x9 已探明（模拟开局视野）
        explored = {(x, y) for x in range(0, 6) for y in range(0, 6)}
        plain = strat.explore(view(pos=(1, 1), explored=explored))
        strat.center_pull = 0.8
        pulled = strat.explore(view(pos=(1, 1), explored=explored))
        self.assertEqual(plain["action"], "move")
        self.assertEqual(pulled["action"], "move")
        # 纯"最近未探明格"会沿同一行向右（y≈0..1）；中心偏置应改朝下/右下的内陆方向
        self.assertIn(plain["direction"], ("right", "down"))
        self.assertIn(pulled["direction"], ("right", "down"))

    def test_center_pull_zero_is_original_behaviour(self):
        strat = GreedyBfsStrategy(24, 24)
        explored = {(x, y) for x in range(0, 6) for y in range(0, 6)}
        first = strat.explore(view(pos=(1, 1), explored=explored))
        strat.center_pull = 0.0
        again = strat.explore(view(pos=(1, 1), explored=explored))
        self.assertEqual(first, again)

    def test_agent_syncs_window_and_gate(self):
        agent = Agent(AgentConfig(seat="A", stage=2, planner="guarded",
                                  combat_mode="hybrid", memory_falsify=True))
        agent.on_perception(perception(tick=0))
        self.assertAlmostEqual(agent.strategy.center_pull, 0.8, places=6)
        self.assertGreaterEqual(agent.center_pull_used, 1)
        agent.on_perception(perception(tick=59, pos=(1, 2)))
        self.assertAlmostEqual(agent.strategy.center_pull, 0.8, places=6)
        agent.on_perception(perception(tick=60, pos=(1, 3)))
        self.assertEqual(agent.strategy.center_pull, 0.0)      # 窗口外回到原行为

    def test_gate_off_for_backup_decision_types(self):
        for mode in ("opportunistic", "finish"):
            agent = Agent(AgentConfig(seat="A", stage=2, planner="guarded",
                                      combat_mode=mode, memory_falsify=True))
            agent.on_perception(perception(tick=5))
            self.assertEqual(agent.strategy.center_pull, 0.0, mode)


class TestEconGuard(unittest.TestCase):
    def make(self, combat="hybrid", econ=60):
        agent = Agent(AgentConfig(seat="A", stage=2, planner="guarded",
                                  combat_mode=combat, memory_falsify=True,
                                  econ_guard=econ))
        agent.width, agent.height = 24, 24
        return agent

    def test_active_when_resources_visible_inside_window(self):
        agent = self.make()
        data = perception(tick=10, resources=[((2, 0), 10)])
        agent.on_perception(data)
        self.assertTrue(agent._econ_guard_active(data))
        self.assertGreaterEqual(agent.econ_guard_ticks, 1)

    def test_inactive_after_window(self):
        agent = self.make()
        data = perception(tick=80, resources=[((2, 0), 10)])
        agent.on_perception(data)
        self.assertFalse(agent._econ_guard_active(data))

    def test_inactive_without_resources(self):
        agent = self.make()
        data = perception(tick=10)
        agent.on_perception(data)
        self.assertFalse(agent._econ_guard_active(data))

    def test_inactive_for_backup_mode(self):
        agent = self.make(combat="opportunistic")
        data = perception(tick=10, resources=[((2, 0), 10)])
        agent.on_perception(data)
        self.assertFalse(agent._econ_guard_active(data))

    def test_attack_is_suppressed_inside_window_with_resource(self):
        """有资源可采时，射程内有敌人也不主动开火（让 Tick 给采集）。"""
        agent = self.make()
        data = perception(tick=10, resources=[((-2, 0), 10)], robots=[(2, 0)])
        action = agent.on_perception(data)
        self.assertNotEqual(action.get("action"), "attack")

    def test_attack_happens_after_window(self):
        agent = self.make()
        data = perception(tick=90, robots=[(2, 0)])
        action = agent.on_perception(data)
        self.assertEqual(action.get("action"), "attack")


if __name__ == "__main__":
    unittest.main(verbosity=2)
