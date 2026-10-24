#!/usr/bin/env python3
"""从裁判的 communication.jsonl 统计某席位的关键指标（实验用）。

用法：
    python scripts/analyze_session.py <communication.jsonl> [席位=A]

统计：死亡次数（position_reason=respawn 或 alive 由真变假）、
移动未成功（last_action.moved=false，含 E_BLOCKED/E_OOB）、
invalid_actions 末值、资源/伤害/任务、有效移动格数。
"""

from __future__ import annotations

import collections
import json
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    seat = sys.argv[2] if len(sys.argv) > 2 else "A"

    alive_prev = None
    deaths_by_alive = 0
    respawns = 0
    blocked = collections.Counter()
    moved_ok = 0
    last = None
    distances = 0
    prev_pos = None
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("kind") != "perception" or record.get("seat") != seat:
            continue
        data = record["data"]
        me = data.get("self") or {}
        last = data
        if me.get("position_reason") == "respawn":
            respawns += 1
        alive = me.get("alive")
        if alive is False and alive_prev is True:
            deaths_by_alive += 1
        alive_prev = alive
        ack = me.get("last_action")
        if isinstance(ack, dict):
            if ack.get("moved") is False:
                blocked[str(ack.get("error"))] += 1
            elif ack.get("moved") is True:
                moved_ok += 1
        pos = me.get("position")
        if isinstance(pos, list) and len(pos) == 2 and prev_pos:
            distances += abs(pos[0] - prev_pos[0]) + abs(pos[1] - prev_pos[1])
        if isinstance(pos, list) and len(pos) == 2:
            prev_pos = pos

    if last is None:
        print("没有该席位的感知记录")
        return 1
    score = last.get("score") or {}
    print(json.dumps({
        "seat": seat,
        "deaths_by_alive": deaths_by_alive,
        "respawn_markers": respawns,
        "move_failed": dict(blocked),
        "move_ok": moved_ok,
        "moved_cells": distances,
        "resource_value": score.get("resource_value"),
        "effective_damage": score.get("effective_damage"),
        "mission_completed": score.get("mission_completed"),
        "invalid_actions": score.get("invalid_actions"),
        "final_hp": (last.get("self") or {}).get("hp"),
        "tick": last.get("tick"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
