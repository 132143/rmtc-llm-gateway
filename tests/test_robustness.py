"""健壮性回归：畸形感知 + 除零 + 空位置。

这三类不是"理论风险"，都是全盘扫描（scripts/stress_sweep.py）在本仓库里抓出来的真崩溃：
  1) 感知里没有 self.position（但视野里有实体）→ _resources 用 None 下标 → TypeError；
  2) 同上，_update_memory 里 tuple(None) → TypeError；
  3) detour=0 且资源就在脚下 → 排序时除零 → ZeroDivisionError。
规则 §5.1 要求字段缺失/类型不符都不能让进程退出，所以这里把它们钉成回归测试。
"""

from __future__ import annotations

import random
import unittest

from contestant.agent import Agent, AgentConfig
from contestant.sim import View
from contestant.strategy import GreedyBfsStrategy


def frame(tick=1, session="m1", **overrides):
    base = {"tick": tick, "session_id": session, "vision_radius": 4,
            "self": {"position": [8, 8], "hp": 100, "alive": True,
                     "protected": False, "defending": False},
            "visible_entities": [], "map": {"width": 24, "height": 24},
            "missions": [], "mission_text": "", "event_text": ""}
    base.update(overrides)
    return base


GRID_WITH_ENTITY = "\n".join(["0" * 9] * 4 + ["000020000"] + ["0" * 9] * 4)


class TestMalformedPerception(unittest.TestCase):
    def test_missing_position_does_not_crash(self):
        agent = Agent(AgentConfig(seat="A", stage=2))
        action = agent.on_perception(frame(self={"position": None, "hp": 100, "alive": True},
                                           visible_entities=[{"type": "resource_high",
                                                              "value": 20,
                                                              "position": GRID_WITH_ENTITY}]))
        # 位置未知时只能等：但绝不允许抛异常
        self.assertIn(action["action"], ("wait", "move", "attack", "defend"))

    def test_empty_self_dict_does_not_crash(self):
        agent = Agent(AgentConfig(seat="A", stage=2))
        agent.on_perception(frame(self={}, visible_entities=[{"type": "obstacle",
                                                              "position": GRID_WITH_ENTITY}]))
        self.assertIsNone(agent.pos)

    def test_position_none_then_valid_recovers(self):
        agent = Agent(AgentConfig(seat="A", stage=2))
        agent.on_perception(frame(tick=1, self={"position": None, "alive": True}))
        action = agent.on_perception(frame(tick=2, self={"position": [8, 8], "hp": 100,
                                                         "alive": True}))
        self.assertEqual(agent.pos, (8, 8))
        self.assertIsNotNone(action)

    def test_random_junk_frames_never_raise(self):
        from scripts.stress_sweep import junk_frame
        rng = random.Random(4242)
        agent = Agent(AgentConfig(seat="A", stage=2))
        for _ in range(400):
            agent.on_perception(junk_frame(rng))

    def test_resource_at_own_cell_does_not_divide_by_zero(self):
        # detour=0 时 value/(dist+0) 会除零：这个是"资源就在脚下"的边界
        strategy = GreedyBfsStrategy(6, 6, detour=0.0)
        view = View((3, 3), [((3, 3), 10)], set(), {(3, 3)}, set(), set(), 5, 4)
        action = strategy.decide(view)
        self.assertIn(action["action"], ("move", "wait"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
