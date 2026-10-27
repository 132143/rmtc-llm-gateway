"""裁判地图编辑器格式的测试（导入 / 导出 / 校验）与 v1.2 的两个坐标约定。

为什么值得单测：
  · 地图格式错了，仿真结论全部作废（我们曾把任务点当"地图中心"，18×18 上判定和裁判不一致）；
  · 规则 v1.2 §8.5 说天下无双"多数是 (11,11)-(13,13)，部分地图有特殊变化，以公告为准"，
    所以程序既不能写死坐标、仿真默认值又必须与公告一致。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from contestant import events as event_mod
from contestant.map_import import (MapFormatError, build_map, parse_editor,
                                   to_editor, to_practice_config)
from contestant.stage2_sim import Stage2Map


def geometry(width=24, height=24, obstacles=(), resources=(), spawns=None):
    spawns = spawns or {"A": [1, 1], "B": [22, 1], "C": [1, 22], "D": [22, 22]}
    return {"map": {"width": width, "height": height,
                    "obstacles": [list(p) for p in obstacles],
                    "resources": [{"id": "r{0}_{1}".format(i, p[0]), "position": list(p),
                                   "value": v} for i, (p, v) in enumerate(resources)]},
            "robots": [{"team": team, "spawn": list(pos)} for team, pos in spawns.items()]}


class TestEditorFormat(unittest.TestCase):
    def test_geometry_roundtrip(self):
        payload = geometry(obstacles=[(2, 2)], resources=[((5, 5), 20), ((6, 6), 10)])
        game_map = build_map(payload)
        self.assertEqual((game_map.width, game_map.height), (24, 24))
        self.assertIn((2, 2), game_map.obstacles)
        self.assertEqual(game_map.resources[(5, 5)], 20)
        self.assertEqual(game_map.spawns["D"], (22, 22))
        back = to_editor(game_map)
        again = build_map(back)
        self.assertEqual(again.obstacles, game_map.obstacles)
        self.assertEqual(again.resources, game_map.resources)
        self.assertEqual(again.spawns, game_map.spawns)

    def test_practice_config_keeps_missions(self):
        payload = to_practice_config(Stage2Map.build(24, 24, obstacles=4, seed=1))
        self.assertEqual(payload["stage"], 2)
        self.assertIn("missions", payload)
        parsed = parse_editor(payload)
        self.assertEqual(parsed["reach"], (12, 12))

    def test_resource_id_must_be_unique(self):
        payload = geometry(resources=[((3, 3), 10)])
        payload["map"]["resources"].append(dict(payload["map"]["resources"][0]))
        with self.assertRaises(MapFormatError):
            build_map(payload)

    def test_resource_cannot_overlap_obstacle(self):
        payload = geometry(obstacles=[(4, 4)], resources=[((4, 4), 10)])
        with self.assertRaises(MapFormatError):
            build_map(payload)

    def test_spawn_rules(self):
        with self.assertRaises(MapFormatError):                    # 重合
            build_map(geometry(spawns={"A": [1, 1], "B": [1, 1], "C": [1, 22], "D": [22, 22]}))
        with self.assertRaises(MapFormatError):                    # 压在障碍上
            build_map(geometry(obstacles=[(1, 1)]))
        partial = geometry(spawns={"A": [1, 1]})
        partial["stage"] = 2                                       # 第二阶段必须是 A/B/C/D
        with self.assertRaises(MapFormatError):
            build_map(partial)

    def test_stage1_only_allows_seat_a(self):
        payload = geometry(width=20, height=20, spawns={"A": [1, 1], "B": [18, 18]})
        payload["stage"] = 1
        with self.assertRaises(MapFormatError):
            build_map(payload)

    def test_reach_must_be_inside_and_free(self):
        with self.assertRaises(MapFormatError):                    # 地图太小
            build_map(geometry(width=12, height=24))
        with self.assertRaises(MapFormatError):                    # 任务点压在障碍上
            build_map(geometry(obstacles=[(12, 12)]))

    def test_external_event_file_rejected(self):
        payload = geometry()
        payload["event_params_file"] = "events.json"
        with self.assertRaises(MapFormatError):
            build_map(payload)

    def test_size_cap_on_file_load(self):
        from contestant.map_import import load_editor_file
        path = os.path.join(tempfile.mkdtemp(), "big.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(geometry()) + " " * (101 * 1024))
        with self.assertRaises(MapFormatError):
            load_editor_file(path)


class TestV12Conventions(unittest.TestCase):
    def test_reach_point_is_12_12_not_map_center(self):
        """规则 v1.2 / 裁判编辑器：第二阶段到达任务固定 [12,12]，与地图中心无关。"""
        for size in (13, 18, 24, 30):
            game_map = Stage2Map.build(size, size, obstacles=0, seed=0)
            self.assertEqual(game_map.reach, (12, 12), "size {0}".format(size))

    def test_build_keeps_reach_free_of_obstacles(self):
        game_map = Stage2Map.build(24, 24, obstacles=40, seed=5)
        self.assertNotIn(game_map.reach, game_map.obstacles)

    def test_fortress_default_is_11_to_13(self):
        """v1.2 §8.5：天下无双多数为 (11,11)-(13,13)；以公告为准，仿真默认按这个范围。"""
        game_map = Stage2Map.open_field(24, 24, seed=0)
        script = event_mod.default_script(game_map)
        fort = [e for e in script.events if e.kind == "fort"]
        self.assertEqual(len(fort), 1)
        cells = fort[0].cells
        self.assertEqual(len(cells), 9)
        self.assertEqual((min(c[0] for c in cells), min(c[1] for c in cells)), (11, 11))
        self.assertEqual((max(c[0] for c in cells), max(c[1] for c in cells)), (13, 13))
        self.assertEqual(fort[0].anchor, (12, 12))

    def test_agent_never_hardcodes_fortress_position(self):
        """堡垒位置只能来自模型公告（buff_zone）：agent 里不许出现 11..13 的写死判断。"""
        import inspect
        from contestant import agent as agent_mod
        from contestant import planner as planner_mod
        for module in (agent_mod, planner_mod):
            source = inspect.getsource(module)
            self.assertNotIn("(11, 11)", source)
            self.assertNotIn("(13, 13)", source)
            self.assertNotIn("range(11, 14)", source)


class TestFortressV12(unittest.TestCase):
    """v1.2：堡垒多数为 (11,11)-(13,13)。程序侧的落点必须来自模型给的 buff_zone。"""

    def make_agent(self):
        from contestant.agent import Agent, AgentConfig
        agent = Agent(AgentConfig(seat="A", stage=2))
        agent.on_perception({"tick": 0, "session_id": "m1", "vision_radius": 4,
                             "self": {"position": [12, 13], "hp": 100, "alive": True,
                                      "protected": False, "defending": False},
                             "visible_entities": [], "map": {"width": 24, "height": 24},
                             "missions": [], "mission_text": "", "event_text": ""})
        return agent

    def test_buff_zone_from_hint_is_used(self):
        agent = self.make_agent()
        agent._learn_buff({"buff_zone": [[11, 11], [13, 13]], "buff_until_tick": 325})
        self.assertEqual(agent._buff_zone, (11, 11, 13, 13))
        self.assertTrue(agent._buff_active())

    def test_camp_waits_inside_and_attacks_within_5x5(self):
        agent = self.make_agent()
        agent._learn_buff({"buff_zone": [[11, 11], [13, 13]], "buff_until_tick": 325})
        agent.last_tick = 300
        # 已经站在堡垒里、附近没有敌人 → 留在里面攒驻留（wait）
        action = agent._buff_camp_move([], {"score": {"effective_damage": 0}})
        self.assertEqual(action, {"action": "wait"})
        # 斜向相邻的敌人（5×5 内）→ 直接攻击，方向不影响命中
        enemy = ({"type": "robot", "hp": 100, "protected": False}, (13, 14))
        action = agent._buff_camp_move([enemy], {"score": {"effective_damage": 0}})
        self.assertEqual(action["action"], "attack")


if __name__ == "__main__":
    unittest.main(verbosity=2)
