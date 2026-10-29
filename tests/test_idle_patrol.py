"""空闲巡逻：没事可做时不要原地 wait（2026-10-29 用户要求）。

背景：第一阶段捡完全部资源后，explore() 无目标 → 返回 wait → 车在最后 20~30 片不动。
分数上无损，但看起来像卡死；且若有资源被视野边角漏掉就永远补不到。
"""

from __future__ import annotations

import os
import unittest

from contestant.agent import Agent, AgentConfig


def grid(dx, dy):
    rows = [["0"] * 9 for _ in range(9)]
    rows[4][4] = "1"
    rows[4 + dy][4 + dx] = "2"
    return "\n".join("".join(row) for row in rows)


def perception(tick=0, pos=(5, 5), session="s1", width=18, height=18):
    return {"tick": tick, "session_id": session, "vision_radius": 4,
            "self": {"team": "A", "robot_id": "A", "position": list(pos), "hp": 100,
                     "alive": True, "protected": False, "defending": False,
                     "respawn_in": 0, "last_action": None},
            "visible_entities": [], "map": {"width": width, "height": height},
            "missions": [], "mission_text": "", "event_text": ""}


class TestIdlePatrol(unittest.TestCase):
    def make(self, **cfg):
        agent = Agent(AgentConfig(seat="A", stage=1, planner="guarded",
                                  combat_mode="never", **cfg))
        return agent

    def test_never_waits_when_nothing_to_do(self):
        agent = self.make()
        pos = (5, 5)
        waits = 0
        for tick in range(60):
            action = agent.on_perception(perception(tick=tick, pos=pos))
            self.assertIsNotNone(action)
            if action.get("action") == "wait":
                waits += 1
                continue
            self.assertEqual(action["action"], "move")
            dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}[action["direction"]]
            pos = (min(17, max(0, pos[0] + dx)), min(17, max(0, pos[1] + dy)))
        self.assertEqual(waits, 0, "空闲时不应该 wait")

    def test_moves_are_deterministic(self):
        first, second = self.make(), self.make()
        seq1 = [first.on_perception(perception(tick=t, pos=(5, 5))).get("direction") for t in (0, 1, 2, 3)]
        seq2 = [second.on_perception(perception(tick=t, pos=(5, 5))).get("direction") for t in (0, 1, 2, 3)]
        self.assertEqual(seq1, seq2)

    def test_can_be_disabled(self):
        """关掉开关时，行为回到旧版（全图已探明 → wait）。"""
        agent = self.make(idle_patrol=False)
        data = perception(tick=0, pos=(5, 5), width=9, height=9)
        agent.on_perception(data)                      # 先建立 strategy
        agent.explored = {(x, y) for x in range(9) for y in range(9)}
        action = agent.on_perception(perception(tick=1, pos=(5, 5), width=9, height=9))
        self.assertEqual(action.get("action"), "wait")

    def test_patrols_when_map_fully_explored(self):
        """全图已探明、没有资源 → 开巡逻时必须继续移动（这就是第一阶段最后 25 片的场景）。"""
        agent = self.make()
        agent.on_perception(perception(tick=0, pos=(5, 5), width=9, height=9))
        agent.explored = {(x, y) for x in range(9) for y in range(9)}
        action = agent.on_perception(perception(tick=1, pos=(5, 5), width=9, height=9))
        self.assertEqual(action.get("action"), "move")
        self.assertIn(action.get("direction"), ("up", "down", "left", "right"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
