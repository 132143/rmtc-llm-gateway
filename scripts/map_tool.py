"""地图工具：与裁判地图编辑器互通（导出给他改、导入给我们跑）。

用法：
    # 把我们的一张压力图导出成编辑器格式（队友用浏览器打开 地图编辑器.html 再导入即可手改）
    python -m scripts.map_tool export --env open24 --seed 3 --out docs/maps/open24_s3.json
    python -m scripts.map_tool export --env practice18 --seed 3 --practice --out docs/maps/p18_s3.json

    # 校验一份编辑器导出的地图（相当于裁判导入前的自检）
    python -m scripts.map_tool check --file docs/maps/open24_s3.json

    # 直接在这张图上跑对照实验（--map-file 会忽略 --envs）
    python -m scripts.map_tool lab --file docs/maps/open24_s3.json --reps 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from contestant.eval_lab import ENVIRONMENTS
from contestant.map_import import (MapFormatError, load_editor_file, parse_editor,
                                   to_editor, to_practice_config)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def cmd_export(args):
    if args.env not in ENVIRONMENTS:
        print("未知地图：{0}（可选 {1}）".format(args.env, ", ".join(ENVIRONMENTS)))
        return 2
    game_map = ENVIRONMENTS[args.env](args.seed)
    payload = (to_practice_config(game_map) if args.practice else to_editor(game_map))
    directory = os.path.dirname(os.path.abspath(args.out))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print("已导出 {0}（{1}×{2}，障碍 {3}，资源 {4}，任务点 {5}）→ {6}".format(
        args.env, game_map.width, game_map.height, len(game_map.obstacles),
        len(game_map.resources), game_map.reach, args.out))
    print("用浏览器打开 地图编辑器.html → 导入这个文件 → 改完按 A 导出。")
    return 0


def cmd_check(args):
    try:
        payload = json.load(open(args.file, encoding="utf-8"))
        parsed = parse_editor(payload)
        game_map = load_editor_file(args.file)
    except (MapFormatError, ValueError, OSError) as exc:
        print("[BAD] {0}".format(exc))
        return 1
    size = os.path.getsize(args.file)
    print("[OK] 格式合法：{0}×{1}，阶段 {2}，障碍 {3}，资源 {4}（价值合计 {5}），出生点 {6}，任务点 {7}".format(
        parsed["width"], parsed["height"], parsed["stage"], len(parsed["obstacles"]),
        len(parsed["resources"]), sum(parsed["resources"].values()),
        sorted(parsed["spawns"]), parsed["reach"]))
    print("     文件 {0} 字节（正式导入上限 102400）；仿真对象：{1}×{2}".format(
        size, game_map.width, game_map.height))
    warnings = []
    if size > 100 * 1024:
        warnings.append("超过正式导入上限 100 KiB")
    if parsed["stage"] == 2 and (parsed["width"] < 13 or parsed["height"] < 13):
        warnings.append("第二阶段地图小于 13×13，任务点 [12,12] 会越界")
    if parsed["reach"] and parsed["reach"] in parsed["obstacles"]:
        warnings.append("任务点压在障碍上")
    if warnings:
        print("[WARN] " + "；".join(warnings))
        return 1
    return 0


def cmd_lab(args):
    from contestant import eval_lab
    argv = ["--map-file", args.file, "--groups", args.groups,
            "--opponents", args.opponents, "--reps", str(args.reps),
            "--ticks", str(args.ticks), "--out", args.out, "--quiet"]
    if args.events:
        argv.append("--events")
        if args.hint_cache:
            argv += ["--hint-cache", args.hint_cache]
    return eval_lab.main(argv)


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    export = sub.add_parser("export", help="把我们生成的地图导出成编辑器格式")
    export.add_argument("--env", default="open24")
    export.add_argument("--seed", type=int, default=0)
    export.add_argument("--practice", action="store_true", help="导出完整测试配置（含任务/事件）")
    export.add_argument("--out", required=True)
    export.set_defaults(func=cmd_export)

    check = sub.add_parser("check", help="校验编辑器导出的地图")
    check.add_argument("--file", required=True)
    check.set_defaults(func=cmd_check)

    lab = sub.add_parser("lab", help="直接用这张地图跑对照实验")
    lab.add_argument("--file", required=True)
    lab.add_argument("--groups", default="guarded_hybrid,baseline_hybrid")
    lab.add_argument("--opponents", default="probe_mid,probe_strong")
    lab.add_argument("--reps", type=int, default=3)
    lab.add_argument("--ticks", type=int, default=500)
    lab.add_argument("--events", action="store_true")
    lab.add_argument("--hint-cache", default="docs/events_cache.json")
    lab.add_argument("--out", default="docs/experiments/mapcheck")
    lab.set_defaults(func=cmd_lab)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
