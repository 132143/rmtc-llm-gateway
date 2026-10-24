"""decision_config.json 的读取（规则 §2.3 的现场切换机制）。"""

from __future__ import annotations

import json
import os
import unittest

from contestant.decision import DECISION_TYPES, DEFAULT_DECISION_TYPE, load_decision_config


class TestDecisionConfig(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "decision-test.json")
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def write(self, payload):
        with open(self.path, "w", encoding="utf-8") as handle:
            if isinstance(payload, str):
                handle.write(payload)
            else:
                json.dump(payload, handle)

    def test_registered_types(self):
        for name in ("collect", "balanced", "combat"):
            self.assertIn(name, DECISION_TYPES)
            self.write({"decision_type": name})
            value, _ = load_decision_config(self.path)
            self.assertEqual(value, name)

    def test_unknown_type_falls_back(self):
        self.write({"decision_type": "teleport"})
        value, note = load_decision_config(self.path)
        self.assertEqual(value, DEFAULT_DECISION_TYPE)
        self.assertIn("未登记", note)

    def test_extra_fields_are_ignored(self):
        self.write({"decision_type": "combat", "target": [12, 12], "action": "attack"})
        value, note = load_decision_config(self.path)
        self.assertEqual(value, "combat")
        self.assertIn("忽略未登记字段", note)

    def test_missing_file_uses_default(self):
        value, note = load_decision_config(os.path.join(os.path.dirname(self.path), "nope.json"))
        self.assertEqual(value, DEFAULT_DECISION_TYPE)
        self.assertIn("未找到", note)

    def test_broken_json_uses_default(self):
        self.write("{not json")
        value, note = load_decision_config(self.path)
        self.assertEqual(value, DEFAULT_DECISION_TYPE)
        self.assertIn("读取失败", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
