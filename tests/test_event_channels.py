"""公告双通道合并（感知 event_text + /referee/event_text 话题）。

为什么加：协议 §12.1 里 /referee/event_text 是"逐条发送"的官方通道（TRANSIENT_LOCAL、深度 128），
而感知里的 event_text 只是"近期公告"。只吃感知时，晚 join 或公告密集可能漏掉最早的公告，
极端情况下一条都看不到 → 没有真实 AI 调用 → 任务分 0（规则 §10.2）。
"""

from __future__ import annotations

import unittest


class FakeNode:
    """只带上 on_event_text / merged_event_text 需要的两个属性。"""

    def __init__(self):
        self.event_lines = []
        self.mission_line = ""

    on_event_text = None      # 由测试注入真实方法


def make_helper():
    """从 bot_node 里取出两个方法，挂到一个假节点上（避免 import rclpy）。"""
    import types

    # bot_node 顶层 import rclpy（比赛机才有），所以这里只把三个方法体抠出来执行，
    # 不 import 整个模块 —— 单测在任意机器上都能跑。
    import ast
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "contestant", "bot_node.py")
    text = open(path, encoding="utf-8").read()
    tree = ast.parse(text)
    wanted = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("on_event_text", "on_mission_text",
                                                              "merged_event_text"):
            wanted[node.name] = node
    module = ast.Module(body=[n for n in wanted.values()], type_ignores=[])
    code = compile(ast.fix_missing_locations(module), path, "exec")
    namespace = {}
    exec(code, namespace)                      # noqa: S102 — 只执行这三个方法
    helper = FakeNode()
    for name in wanted:
        setattr(helper, name, types.MethodType(namespace[name], helper))
    return helper


class Msg:
    def __init__(self, data):
        self.data = data


class TestEventChannels(unittest.TestCase):
    def test_topic_lines_are_buffered_and_deduped(self):
        node = make_helper()
        node.on_event_text(Msg("第 60 Tick：爆炸预告 A"))
        node.on_event_text(Msg("第 60 Tick：爆炸预告 A\n第 120 Tick：幻术预告 B"))
        self.assertEqual(node.event_lines, ["第 60 Tick：爆炸预告 A", "第 120 Tick：幻术预告 B"])

    def test_merge_keeps_perception_order_then_topic(self):
        node = make_helper()
        node.on_event_text(Msg("第 120 Tick：B"))
        data = {"event_text": "第 60 Tick：A"}
        self.assertEqual(node.merged_event_text(data), "第 60 Tick：A\n第 120 Tick：B")

    def test_merge_dedupes_same_line(self):
        node = make_helper()
        node.on_event_text(Msg("第 60 Tick：A"))
        self.assertEqual(node.merged_event_text({"event_text": "第 60 Tick：A"}), "第 60 Tick：A")

    def test_merge_works_when_perception_has_nothing(self):
        node = make_helper()
        node.on_event_text(Msg("第 60 Tick：A"))
        self.assertEqual(node.merged_event_text({"event_text": ""}), "第 60 Tick：A")

    def test_mission_fallback(self):
        node = make_helper()
        node.on_mission_text(Msg("收集两个普通资源"))
        self.assertEqual(node.mission_line, "收集两个普通资源")


if __name__ == "__main__":
    unittest.main(verbosity=2)
