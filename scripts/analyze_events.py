#!/usr/bin/env python3
"""统计"本车暴露在活跃事件危险区里的 Tick 数"。

用法：
    python scripts/analyze_events.py <config.json> <trace.jsonl> [delay_ticks]

危险区规则（来自 config 的 events.definitions + schedule）：
  · explosion  矩形，持续 duration_ticks
  · illusion   矩形，持续 duration_ticks（效果另算）
  · world_split 以 point 为交点的整行整列，持续 duration_ticks
  · pepper      point 触发的整行整列（保守按"行+列都算危险"），持续 duration_ticks
  · 其它事件（angel/unrivaled/purity/pan）不计入"危险格"
激活 Tick = announce_tick + delay_ticks（公告里"5 Tick 后"）。
"""

from __future__ import annotations

import json
import sys


def danger_cells(definition: dict, name: str) -> set:
    cells = set()
    rect = definition.get("rect")
    if isinstance(rect, dict):
        x1, y1 = rect.get("top_left", [0, 0])
        x2, y2 = rect.get("bottom_right", [0, 0])
        for x in range(min(x1, x2), max(x1, x2) + 1):
            for y in range(min(y1, y2), max(y1, y2) + 1):
                cells.add((x, y))
    point = definition.get("point")
    if name == "world_split" and isinstance(point, list) and len(point) == 2:
        px, py = point
        for i in range(-40, 41):          # 覆盖足够长的行/列
            cells.add((px + i, py))
            cells.add((px, py + i))
    for item in definition.get("points") or []:
        if isinstance(item, list) and len(item) == 2:
            px, py = item
            for i in range(-40, 41):
                cells.add((px + i, py))
                cells.add((px, py + i))
    return cells


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    config = json.load(open(sys.argv[1], encoding="utf-8"))
    trace = [json.loads(line) for line in open(sys.argv[2], encoding="utf-8") if line.strip()]
    delay = int(sys.argv[3]) if len(sys.argv) > 3 else int(
        config.get("events", {}).get("announcement_delay_ticks", 4))

    windows = []
    for entry in config.get("events", {}).get("schedule", []):
        name = entry.get("event")
        definition = config["events"]["definitions"].get(name, {})
        start = int(entry.get("announce_tick", 0)) + delay
        duration = int(definition.get("duration_ticks", 1))
        cells = danger_cells(definition, name)
        if cells:
            windows.append((start, start + max(1, duration), name, cells))

    exposure = 0
    per_event = {}
    for record in trace:
        tick = record.get("tick")
        pos = record.get("pos")
        if not isinstance(pos, list) or len(pos) != 2:
            continue
        cell = (pos[0], pos[1])
        for start, end, name, cells in windows:
            if start <= tick < end and cell in cells:
                exposure += 1
                per_event[name] = per_event.get(name, 0) + 1
                break

    print(json.dumps({"trace_len": len(trace), "windows": len(windows),
                      "exposure_ticks": exposure, "per_event": per_event,
                      "deaths": trace[-1].get("deaths") if trace else None,
                      "effects": trace[-1].get("effects") if trace else None},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
