"""守住"现场最小集"：这 17 个文件必须够跑，且不许漂移。

背景：交付分成两份——现场运行集（小）与完整仓库（练习/测试/文档）。
最小的那份一旦漏文件，现场就是"起不来"，而这个错误只有在比赛机器上才会暴露。
所以把白名单钉成测试：文件必须存在、模块级依赖必须被覆盖。
"""

from __future__ import annotations

import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.package import RUNTIME_FILES  # noqa: E402


class TestRuntimeBundle(unittest.TestCase):
    def test_all_listed_files_exist(self):
        for rel in RUNTIME_FILES:
            path = os.path.join(ROOT, rel.replace("/", os.sep))
            self.assertTrue(os.path.isfile(path), "运行集缺文件：" + rel)

    def test_bot_entry_imports_are_covered(self):
        """bot_node 的模块级 import 必须都在运行集里（否则现场 ImportError）。"""
        path = os.path.join(ROOT, "contestant", "bot_node.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        needed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                needed.add(node.module)
            elif isinstance(node, ast.Import):
                needed.update(a.name for a in node.names)
        local = {rel.replace(".py", "").replace("/", ".") for rel in RUNTIME_FILES}
        packages = {"contestant", "rmtc_client"}          # 包名本身：由包内 __init__.py 覆盖
        for mod in needed:
            if not mod.startswith(("contestant", "rmtc_client")):
                continue
            if mod in packages:
                self.assertIn(mod + ".__init__", local, "运行集缺 " + mod + "/__init__.py")
                continue
            self.assertIn(mod, local, "bot_node 依赖 " + mod + " 但运行集里没有")

    def test_lazy_import_targets_are_present(self):
        """agent 里 lazy import 的东西也必须被覆盖（sim.View、sweeper 除外）。"""
        path = os.path.join(ROOT, "contestant", "agent.py")
        source = open(path, encoding="utf-8").read()
        local = {rel.replace(".py", "").replace("/", ".") for rel in RUNTIME_FILES}
        for mod in ("contestant.tactics", "contestant.planner", "contestant.strategy",
                    "contestant.sim", "rmtc_client.llm_client",
                    "rmtc_client.event_understanding", "rmtc_client.json_extract"):
            self.assertIn(mod, local, "agent 依赖 " + mod + " 但运行集里没有")
        self.assertIn("contestant.sim import View", source,
                      "agent 仍在 lazy import sim.View：运行集必须带 sim.py")

    def test_no_dev_modules_in_runtime(self):
        """实验台/离线仿真不该混进现场集（省 100KB 且避免误用）。"""
        for rel in RUNTIME_FILES:
            self.assertNotIn(rel, ("contestant/eval_lab.py", "contestant/stage2_sim.py",
                                   "contestant/run_experiments.py", "contestant/map_import.py",
                                   "contestant/llm_replay.py", "contestant/events.py",
                                   "contestant/sweeper.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
