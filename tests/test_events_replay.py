"""事件注入 + 真实模型回放 + 高层意图的测试。

三块都是"会直接影响合规与稳定性"的新代码：
  · 事件结算错了 → 该躲没躲（掉分）、不该打打了（反伤）；
  · 回放写错了 → 仿真里出现"模型没说过的话"（自欺）；
  · 意图没校验 → 一条过期/低置信度的模型输出把整局带偏。
所以每条规则都配一个能证伪的用例。
"""

from __future__ import annotations

import unittest

from contestant.agent import Agent, AgentConfig
from contestant.events import Event, EventEngine, EventScript, default_script, rect_cells
from contestant.llm_replay import (ReplayLlmClient, key_for_prompt, key_for_text,
                                   recording_plan, save_cache)
from contestant.stage2_sim import Stage2Map, Stage2Simulation
from rmtc_client.event_understanding import EventUnderstanding, PROMPT_TEMPLATE


def brain(script):
    class Scripted:
        def on_perception(self, data):
            payload = {"tick": data["tick"], "session_id": data["session_id"]}
            payload.update(script(data["tick"]))
            return payload
    return Scripted()


def small_map(spawns=None, resources=None):
    return Stage2Map(8, 8, obstacles=set(), resources=resources or [],
                     spawns=spawns or {"A": (1, 1)}, reach=(4, 4))


class TestEventEngine(unittest.TestCase):
    def test_transcript_only_reveals_announced_events(self):
        script = EventScript([Event("explosion", 10, 15, 16,
                                    cells=rect_cells(1, 1, 2, 2))])
        script.events[0].render(8, 8)
        self.assertEqual(script.transcript(9), "")
        text = script.transcript(10)
        self.assertIn("爆炸", text)
        self.assertIn("80 HP", text)      # 自然语言里的数值是允许的
        for banned in ("threat_zone", "{", "[["):
            self.assertNotIn(banned, text)  # 不能把结构化参数喂给程序

    def test_explosion_damages_but_protection_is_immune(self):
        game_map = small_map(spawns={"A": (1, 1), "B": (2, 2)})
        event = Event("explosion", 0, 0, 1, cells=rect_cells(1, 1, 2, 2))
        script = EventScript([event])
        sim = Stage2Simulation(game_map, {"A": brain(lambda t: {"action": "wait"}),
                                          "B": brain(lambda t: {"action": "wait"})},
                               ticks=1, events=script)
        sim.robots["B"].protected_until = 5
        sim.run()
        self.assertEqual(sim.robots["A"].hp, 20.0)
        self.assertEqual(sim.robots["B"].hp, 100.0)

    def test_world_split_kills_respawns_and_forces_wait(self):
        game_map = small_map(spawns={"A": (4, 1)})
        event = Event("world_split", 0, 0, 30, anchor=(4, 1),
                      cells=frozenset({(4, 1), (4, 2)}))
        script = EventScript([event])
        sim = Stage2Simulation(game_map, {"A": brain(lambda t: {"action": "move",
                                                              "direction": "down"})},
                               ticks=1, events=script)
        result = sim.run()
        self.assertEqual(result["seats"]["A"]["moves"], 0)      # 强制等待
        self.assertEqual(sim.robots["A"].deaths, 1)
        self.assertNotEqual(sim.robots["A"].pos, (4, 1))        # 十字线已变成临时墙，落点改到最近合法格
        self.assertEqual(sim.robots["A"].hp, 100.0)             # 立即满血复活
        self.assertEqual(sim.robots["A"].damage, 0.0)           # 扣 60 后被下限 0 兜住

    def test_illusion_reverses_move_only(self):
        game_map = small_map(spawns={"A": (4, 4)})
        event = Event("illusion", 0, 0, 50, cells=rect_cells(0, 0, 7, 7))
        script = EventScript([event])
        sim = Stage2Simulation(game_map, {"A": brain(lambda t: {"action": "move",
                                                              "direction": "right"})},
                               ticks=1, events=script)
        sim.run()
        self.assertEqual(sim.robots["A"].pos, (3, 4))            # 裁判把 right 反成 left

    def test_pepper_line_hits_others_not_trigger(self):
        game_map = small_map(spawns={"A": (2, 2), "B": (2, 5)})
        event = Event("pepper", 0, 0, 30, anchor=(2, 2), cells={(2, 2)})
        script = EventScript([event])
        # A 原地等待不动 → 不触发；B 向左走进 (2,4) 不触发。
        # 让 A 向右走一步再向左回来会触发；这里直接让 A 站在点上并"成功移动进入"：
        sim = Stage2Simulation(game_map, {"A": brain(
            lambda t: {"action": "move", "direction": "right"} if t == 0
            else {"action": "move", "direction": "left"}),
            "B": brain(lambda t: {"action": "wait"})}, ticks=3, events=script)
        sim.run()
        # 第 1 Tick A 从 (2,3) 走回 (2,2)（点位）→ 触发火线，命中同列 (2,5) 的 B
        self.assertLess(sim.robots["B"].hp, 100.0)
        self.assertEqual(sim.robots["A"].hp, 100.0)

    def test_fort_dwell_grants_aoe(self):
        game_map = small_map(spawns={"A": (4, 4), "B": (5, 4)})
        fort_cells = frozenset((x, y) for x in range(3, 6) for y in range(3, 6))
        event = Event("fort", 0, 0, 60, anchor=(4, 4), cells=fort_cells)
        script = EventScript([event])
        sim = Stage2Simulation(game_map, {"A": brain(
            lambda t: {"action": "wait"} if t < 10 else {"action": "attack", "direction": "right"}),
            "B": brain(lambda t: {"action": "wait"})}, ticks=11, events=script)
        result = sim.run()
        self.assertGreaterEqual(result["seats"]["A"]["damage"], 50.0)

    def test_vulnerability_doubles_normal_damage(self):
        game_map = small_map(spawns={"A": (1, 1), "B": (2, 1)})
        event = Event("vulnerability", 0, 0, 40)
        script = EventScript([event])
        sim = Stage2Simulation(game_map, {"A": brain(
            lambda t: {"action": "attack", "direction": "right"}),
            "B": brain(lambda t: {"action": "move", "direction": "right"} if t == 0
                       else {"action": "wait"})}, ticks=2, events=script)
        sim.run()
        # B 在 Tick0 成功移动 → 获得易伤；Tick1 的普通攻击应打 40 而不是 20
        self.assertGreaterEqual(sim.robots["A"].damage, 40.0)

    def test_clean_hands_recoils_attacker(self):
        game_map = small_map(spawns={"A": (1, 1), "B": (2, 1)})
        event = Event("clean_hands", 0, 0, 20)
        script = EventScript([event])
        sim = Stage2Simulation(game_map, {"A": brain(
            lambda t: {"action": "attack", "direction": "right"}),
            "B": brain(lambda t: {"action": "defend"})}, ticks=1, events=script)
        sim.run()
        self.assertEqual(sim.robots["B"].hp, 100.0)     # 目标无伤
        self.assertEqual(sim.robots["A"].hp, 80.0)      # 攻击者自受 20
        self.assertEqual(sim.robots["A"].damage, 0.0)

    def test_heal_point_regenerates(self):
        game_map = small_map(spawns={"A": (3, 3)})
        event = Event("heal", 0, 0, 20, anchor=(3, 3), cells={(3, 3)})
        script = EventScript([event])
        sim = Stage2Simulation(game_map, {"A": brain(lambda t: {"action": "wait"})},
                               ticks=3, events=script)
        sim.robots["A"].hp = 50.0
        sim.run()
        self.assertGreater(sim.robots["A"].hp, 50.0)

    def test_default_script_covers_all_eight_types(self):
        game_map = Stage2Map.open_field(24, 24, seed=0)
        script = default_script(game_map)
        kinds = {event.kind for event in script.events}
        self.assertEqual(len(kinds), 8, kinds)
        for event in script.events:
            self.assertTrue(event.text)
            self.assertNotIn("{", event.text)

    def test_engine_reversal_expiry(self):
        script = EventScript([Event("illusion", 0, 0, 10, cells=rect_cells(0, 0, 7, 7))])
        engine = EventEngine(script)
        game_map = small_map()
        sim = Stage2Simulation(game_map, {"A": brain(lambda t: {"action": "wait"})},
                               ticks=1, events=script)
        engine.pre_move(sim, 0)
        self.assertTrue(engine.reversed("A", 1))
        self.assertTrue(engine.reversed("A", 49))       # §8.3：幻术固定 50 Tick
        self.assertFalse(engine.reversed("A", 60))


class TestReplay(unittest.TestCase):
    def test_key_is_stable_and_specific(self):
        prompt_a = "公告：第 10 Tick：爆炸发生"
        prompt_b = "公告：第 10 Tick：幻术生效"
        self.assertEqual(key_for_prompt(prompt_a), key_for_prompt(prompt_a))
        self.assertNotEqual(key_for_prompt(prompt_a), key_for_prompt(prompt_b))
        self.assertEqual(key_for_prompt(prompt_a), key_for_text("第 10 Tick：爆炸发生"))

    def test_missing_cache_fails_instead_of_inventing(self):
        client = ReplayLlmClient({})
        result = client.ask("公告：第 1 Tick：某事")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "not_recorded")

    def test_cached_text_is_returned_through_real_event_understanding(self):
        prompt_text = "第 5 Tick：矩形内将发生爆炸"
        key = key_for_text(prompt_text)
        client = ReplayLlmClient({key: '{"avoid_cells": [[4, 4]], "danger_level": "fatal"}'})
        understanding = EventUnderstanding(client, logger=None)
        understanding.offer("s1", 5, prompt_text)
        hint = understanding.hint("s1", 5)
        self.assertIsNotNone(hint)
        self.assertEqual(hint["parsed"]["danger_level"], "fatal")

    def test_recording_plan_matches_prompt_template_contract(self):
        game_map = Stage2Map.open_field(24, 24, seed=0)
        script = default_script(game_map)
        plan = recording_plan(game_map, script)
        self.assertEqual(len(plan), len(script.events))
        for key, prompt, announcement in plan:
            self.assertIn(announcement.strip().splitlines()[-1], prompt)
            self.assertIn("target_kind", prompt)
            self.assertEqual(len(key), 16)

    def test_save_cache_roundtrip(self):
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "cache.json")
        save_cache(path, {"abc": '{"x": 1}'})
        client = ReplayLlmClient(cache_path=path)
        self.assertEqual(client.stats()["cached"], 1)


def grid_entity(dx, dy, kind="obstacle"):
    rows = [["0"] * 9 for _ in range(9)]
    rows[4][4] = "1"
    rows[4 + dy][4 + dx] = "2"
    return {"type": kind, "position": "\n".join("".join(r) for r in rows)}
def frame(tick, entities=(), pos=(8, 8), session="m1", damage=0.0):
    return {"tick": tick, "session_id": session, "vision_radius": 4,
            "self": {"position": list(pos), "hp": 100, "alive": True,
                     "protected": False, "defending": False},
            "visible_entities": list(entities), "map": {"width": 24, "height": 24},
            "missions": [], "mission_text": "", "event_text": "",
            "score": {"resource_value": 0, "effective_damage": damage,
                      "mission_completed_ids": []}}


class TestObstacleMemory(unittest.TestCase):
    """临时墙（事件十字线）不能被当成永久障碍：这是事件注入跑出来的真 bug。"""

    def test_obstacle_is_blocked_while_visible(self):
        agent = Agent(AgentConfig(seat="A", stage=2))
        agent.on_perception(frame(1, [grid_entity(1, 0)]))
        self.assertIn((9, 8), agent._blocked_now())

    def test_obstacle_expires_after_ttl(self):
        agent = Agent(AgentConfig(seat="A", stage=2))
        agent.on_perception(frame(1, [grid_entity(1, 0)]))
        agent.on_perception(frame(60, []))          # 40 Tick 没再看到 → 认为可能是临时墙
        self.assertNotIn((9, 8), agent._blocked_now())

    def test_obstacle_refreshed_when_seen_again(self):
        agent = Agent(AgentConfig(seat="A", stage=2))
        agent.on_perception(frame(1, [grid_entity(1, 0)]))
        agent.on_perception(frame(30, [grid_entity(1, 0)]))
        self.assertIn((9, 8), agent._blocked_now())

    def test_boundary_is_permanent(self):
        agent = Agent(AgentConfig(seat="A", stage=2))
        # 站在 (0,0)，上/左越界 → E_OOB，边界永不遗忘
        agent.on_perception(frame(0, [], pos=(0, 0)))
        agent.on_perception(frame(1, [], pos=(0, 0)))
        agent.last_move = ((0, 0), "up")
        agent._learn_from_last_action({"moved": False, "error": "E_OOB"})
        agent.on_perception(frame(200, [], pos=(0, 0)))
        self.assertIn((0, -1), agent.boundary_obstacles)


class TestLlmIntent(unittest.TestCase):
    def make_agent(self, intent=None, confidence=0.9, tick=3):
        agent = Agent(AgentConfig(seat="A", stage=2, combat_mode="opportunistic"))
        agent.on_perception({"tick": 0, "session_id": "m1", "vision_radius": 4,
                             "self": {"position": [8, 8], "hp": 100, "alive": True,
                                      "protected": False, "defending": False},
                             "visible_entities": [], "map": {"width": 18, "height": 18},
                             "missions": [], "mission_text": "", "event_text": ""})
        agent.understanding = EventUnderstanding(ReplayLlmClient({}), logger=None)
        agent.understanding._hint = {"session_id": "m1", "tick": 0,
                                     "parsed": {"target_kind": intent,
                                                "target_confidence": confidence},
                                     "advice": "test", "raw": "{}"}
        return agent

    def frame(self, tick, robots=(), damage=0.0):
        entities = []
        for dx, dy in robots:
            rows = [["0"] * 9 for _ in range(9)]
            rows[4][4] = "1"
            rows[4 + dy][4 + dx] = "2"
            entities.append({"type": "robot", "position": "\n".join("".join(r) for r in rows),
                             "hp": 100, "protected": False})
        return {"tick": tick, "session_id": "m1", "vision_radius": 4,
                "self": {"position": [8, 8], "hp": 100, "alive": True,
                         "protected": False, "defending": False},
                "visible_entities": entities, "map": {"width": 18, "height": 18},
                "missions": [], "mission_text": "", "event_text": "",
                "score": {"resource_value": 0, "effective_damage": damage,
                          "mission_completed_ids": []}}

    def test_flee_blocks_attacking(self):
        agent = self.make_agent("flee")
        action = agent.on_perception(self.frame(1, robots=[(1, 0)]))
        self.assertNotEqual(action["action"], "attack")

    def test_engage_overrides_damage_cap(self):
        agent = self.make_agent("engage")
        action = agent.on_perception(self.frame(1, robots=[(1, 0)], damage=600))
        self.assertEqual(action["action"], "attack")

    def test_low_confidence_is_ignored(self):
        agent = self.make_agent("flee", confidence=0.1)
        action = agent.on_perception(self.frame(1, robots=[(1, 0)]))
        self.assertEqual(action["action"], "attack")

    def test_unknown_intent_is_ignored(self):
        agent = self.make_agent("teleport-all-bots")
        self.assertEqual(agent.llm_intent, "none")
        action = agent.on_perception(self.frame(1, robots=[(1, 0)]))
        self.assertEqual(action["action"], "attack")

    def test_intent_expires(self):
        agent = self.make_agent("flee")
        agent.on_perception(self.frame(1, robots=[(1, 0)]))
        self.assertEqual(agent.llm_intent, "flee")
        # 过期后再打：意图不再生效（TTL 40 Tick）
        action = agent.on_perception(self.frame(60, robots=[(1, 0)]))
        self.assertEqual(action["action"], "attack")


if __name__ == "__main__":
    unittest.main(verbosity=2)