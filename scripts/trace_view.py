"""把一次运行的轨迹（trace jsonl）在终端里**放出来**，看清机器人到底怎么跑的。

轨迹从哪来：
    RMTC_TRACE=/tmp/trace_A.jsonl python3 -m contestant.bot_node --seat A --stage 2
    （离线仿真也会写同样的格式：Agent(trace_path=...)）

用法：
    python -m scripts.trace_view /tmp/trace_A.jsonl            # 打印摘要 + 每 25 片一帧
    python -m scripts.trace_view /tmp/trace_A.jsonl --every 5  # 更密
    python -m scripts.trace_view /tmp/trace_A.jsonl --summary  # 只看摘要
    python -m scripts.trace_view /tmp/trace_A.jsonl --play     # 终端动画（Ctrl-C 退出）

图例：A=当前车  o=走过的格子（近 80 步）  #=撞墙  x=阵亡格  ·=空地
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def load(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def summary(rows):
    if not rows:
        print("轨迹为空")
        return
    first, last = rows[0], rows[-1]
    moves = sum(1 for r in rows if r.get("action") == "move")
    attacks = sum(1 for r in rows if r.get("action") == "attack")
    defends = sum(1 for r in rows if r.get("action") == "defend")
    waits = sum(1 for r in rows if r.get("action") == "wait")
    deaths = last.get("deaths") or 0
    visited = {(tuple(r["pos"])) for r in rows if r.get("pos")}
    width = max((p[0] for p in visited), default=0) + 1
    height = max((p[1] for p in visited), default=0) + 1
    print("=" * 78)
    print("轨迹摘要：%s" % os.path.basename(getattr(rows, "name", "")))
    print("  Tick 范围      : %s → %s（共 %d 帧）" % (first.get("tick"), last.get("tick"), len(rows)))
    print("  动作分布       : move %d / attack %d / defend %d / wait %d" % (moves, attacks, defends, waits))
    print("  终局           : 资源 %s ｜ 伤害 %s ｜ 任务 %s ｜ 死亡 %d" % (
        last.get("resource_value"), last.get("effective_damage"),
        last.get("missions"), deaths))
    print("  走过格子       : %d 格（覆盖约 %d×%d 的范围）" % (len(visited), width, height))
    print("  AI 心跳已确认  : %s ｜ 事件效果 %s ｜ 施法/蹲点移动 %s ｜ 蹲点攻击 %s" % (
        last.get("ai_confirmed"), last.get("effects"), last.get("buff_moves"),
        last.get("buff_attacks")))
    print("=" * 78)


def render(row, trail, width, height, block):
    grid = [["·"] * width for _ in range(height)]
    for cell in trail:
        if 0 <= cell[0] < width and 0 <= cell[1] < height:
            grid[cell[1]][cell[0]] = "o"
    if block:
        grid[block[1]][block[0]] = "#"
    pos = row.get("pos")
    alive = row.get("alive")
    if pos:
        grid[pos[1]][pos[0]] = "A" if alive is not False else "x"
    lines = []
    for y, line in enumerate(grid):
        lines.append("%2d %s" % (y, "".join(line)))
    header = "Tick %-4s %-7s %-6s hp=%-4s rv=%-3s dmg=%-5s 任务=%s 死亡=%s" % (
        row.get("tick"), row.get("action"), row.get("direction") or "-", row.get("hp"),
        row.get("resource_value"), row.get("effective_damage"), row.get("missions"),
        row.get("deaths"))
    return header, lines


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--every", type=int, default=25, help="每多少帧画一张图")
    parser.add_argument("--summary", action="store_true", help="只打印摘要")
    parser.add_argument("--play", action="store_true", help="终端动画（真实速度，建议配合 --speed）")
    parser.add_argument("--speed", type=float, default=0.02, help="动画每帧间隔秒数")
    parser.add_argument("--trail", type=int, default=80, help="尾巴长度（格子数）")
    args = parser.parse_args(argv)

    rows = load(args.path)
    if not rows:
        print("没有读到任何轨迹行：%s" % args.path)
        return 1
    summary(rows)
    if args.summary:
        return 0

    width = max((r["pos"][0] for r in rows if r.get("pos")), default=23) + 1
    height = max((r["pos"][1] for r in rows if r.get("pos")), default=23) + 1
    trail = []
    for index, row in enumerate(rows):
        pos = row.get("pos")
        if pos:
            trail.append((int(pos[0]), int(pos[1])))
            trail = trail[-args.trail:]
        block = None
        ack = row.get("ack") or {}
        if ack.get("error") == "E_BLOCKED" and pos:
            dx, dy = DIRS.get(row.get("direction") or "", (0, 0))
            block = (int(pos[0]) + dx, int(pos[1]) + dy)
        show = args.play or (index % max(1, args.every) == 0) or index == len(rows) - 1
        if not show:
            continue
        header, lines = render(row, trail, width, height, block)
        if args.play:
            print("\033[H\033[J", end="")
        print(header)
        for line in lines:
            print(line)
        print("")
        if args.play:
            time.sleep(max(0.0, args.speed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
