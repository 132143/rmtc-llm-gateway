"""扫图型假想敌（SweeperBrain）的行为测试。

它存在的意义是当"照妖镜"：一直在动、把图扫光、顺手拿最近资源、打一下就溜。
所以测试只盯四件事：**真的在动**、**真的挑最近**、**真的打一下就跑**、**真的躲危险**。
"""

from __future__ import annotations

import unittest

from contestant.planner import WorldView
from contestant.sweeper import SweeperBrain
from contestant import tactics

DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def view(pos=(5, 5), tick=10, width=16, height=16, resources=(), enemies=(),
         blocked=(), avoid=(), alive=True, protected=False, combat_allowed=True,
         hp=100, danger="none"):
    return WorldView(pos=pos, tick=tick, hp=hp, width=width, height=height,
                     alive=alive, protected=protected, blocked=set(blocked),
                     resources=[(tuple(p), v, tick) for p, v in resources],
                     enemies=[(e, tuple(p), tick, None) for e, p in enemies],
                     avoid_cells=set(avoid), danger_level=danger,
                     combat_allowed=combat_allowed, params=tactics.Params())


def enemy(hp=100, protected=False):
    return {"hp": hp, "protected": protected, "defending": False}


class TestSweeper(unittest.TestCase):
    def test_always_moves(self):
        brain = SweeperBrain(16, 16)
        action = brain.plan(view())
        self.assertEqual(action["action"], "move")

    def test_picks_nearest_not_most_valuable(self):
        brain = SweeperBrain(16, 16)
        # 近的普通资源（距离 2）、远的高价值（距离 6）→ 应该选近的
        action = brain.plan(view(resources=[((7, 5), 10), ((11, 5), 20)]))
        self.assertEqual(action["action"], "move")
        self.assertEqual(action["direction"], "right")

    def test_attacks_then_runs(self):
        brain = SweeperBrain(16, 16)
        shot = brain.plan(view(tick=10, enemies=[(enemy(), (7, 5))]))
        self.assertEqual(shot["action"], "attack")
        self.assertEqual(shot["direction"], "right")
        # 同一片再问一次：仍在"打完就跑"窗口里 → 应该往反方向走，而不是继续打
        after = brain.plan(view(tick=11, enemies=[(enemy(), (7, 5))]))
        self.assertEqual(after["action"], "move")
        self.assertNotEqual(after["direction"], "right")

    def test_does_not_attack_protected_target(self):
        brain = SweeperBrain(16, 16)
        action = brain.plan(view(enemies=[(enemy(protected=True), (7, 5))]))
        self.assertNotEqual(action["action"], "attack")

    def test_does_not_attack_when_protected_self(self):
        brain = SweeperBrain(16, 16)
        action = brain.plan(view(protected=True, enemies=[(enemy(), (7, 5))]))
        self.assertNotEqual(action["action"], "attack")

    def test_avoids_danger_cells(self):
        brain = SweeperBrain(16, 16)
        # 资源在右边，但右边那格是危险格 → 不能往右
        action = brain.plan(view(resources=[((9, 5), 10)], avoid=[(6, 5)]))
        self.assertEqual(action["action"], "move")
        self.assertNotEqual(action["direction"], "right")

    def test_escapes_when_standing_in_danger(self):
        brain = SweeperBrain(16, 16)
        action = brain.plan(view(pos=(6, 5), avoid=[(6, 5), (6, 6), (7, 5)]))
        self.assertEqual(action["action"], "move")
        self.assertEqual(action["reason"], "escape")

    def test_dead_robot_waits(self):
        brain = SweeperBrain(16, 16)
        self.assertEqual(brain.plan(view(alive=False))["action"], "wait")

    def test_coverage_grows_over_time(self):
        """连续跑 80 片，覆盖比例应该明显上升（证明它真的在扫图，而不是打转）。"""
        brain = SweeperBrain(12, 12)
        pos = (1, 1)
        for tick in range(80):
            action = brain.plan(view(pos=pos, tick=tick, width=12, height=12))
            self.assertEqual(action["action"], "move", "第 {0} 片它没动".format(tick))
            dx, dy = DIRS[action["direction"]]
            pos = (max(0, min(11, pos[0] + dx)), max(0, min(11, pos[1] + dy)))
        self.assertGreater(brain.covered_ratio(), 0.35)

    def test_hit_and_run_window_expires(self):
        brain = SweeperBrain(16, 16)
        brain.plan(view(tick=10, enemies=[(enemy(), (7, 5))]))
        # 窗口过后，若敌人重新进入射程，仍会再打一次（不是永久不还手）
        later = brain.plan(view(tick=10 + SweeperBrain.FLEE_TICKS + 1,
                                enemies=[(enemy(), (7, 5))]))
        self.assertEqual(later["action"], "attack")


if __name__ == "__main__":
    unittest.main(verbosity=2)
