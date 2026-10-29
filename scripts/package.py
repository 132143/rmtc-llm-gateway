#!/usr/bin/env python3
"""把仓库打包成交付给队友/主办方的 zip（只打 git 跟踪的文件）。

⚠️ 必须用本脚本打包，**不要用 Windows 的 Compress-Archive**：
   它写出的条目分隔符是反斜杠（`contestant\agent.py`），在 Ubuntu 上 `unzip` 会解成一堆带反斜杠的
   文件名而不是目录，主办方那边直接跑不起来（2026-10-29 实际踩到并修正）。
   本脚本用 zipfile + arcname="包名/相对路径"，条目全部是正斜杠，且带顶层目录。

用法（仓库根目录）：
    python scripts/package.py                      # 输出到 ../dist/rmtc-competition-<日期>.zip
    python scripts/package.py --out 目录 --name 包名
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tracked_files() -> list:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


RUNTIME_FILES = (
    # contestant/：bot_node 及其**运行期真正依赖**（agent 里 lazy import 的 sim.View 也算）
    "contestant/__init__.py",
    "contestant/agent.py",
    "contestant/bot_node.py",
    "contestant/decision.py",
    "contestant/planner.py",
    "contestant/strategy.py",
    "contestant/tactics.py",
    "contestant/sim.py",
    # rmtc_client/：AI 链路全部要
    "rmtc_client/__init__.py",
    "rmtc_client/llm_client.py",
    "rmtc_client/event_understanding.py",
    "rmtc_client/json_extract.py",
    # 现场工具
    "scripts/preflight.py",
    "scripts/trace_view.py",
    # 配置与封存
    "decision_config.json",
    "SEALED_VERSION.txt",
)
RUNTIME_EXTRA = (("docs/RUN_ON_SITE.md", "RUN_ON_SITE.md"),)


def runtime_files() -> list:
    """现场最小集（17 个文件）：只含上场要用的模块。

    刻意**不用 import 闭包自动裁**：闭包会把离线仿真/实验台也带进来，
    而 lazy import（agent 里的 sweeper / sim.View）又会漏。这里用显式白名单，
    并由 tests 里的最小集冒烟守住（构建后能在干净目录里 import + 跑一帧决策）。
    """
    tracked = set(tracked_files())
    missing = [rel for rel in RUNTIME_FILES if rel not in tracked]
    if missing:
        raise SystemExit("运行集里这些文件没被 git 跟踪：" + ", ".join(missing))
    return list(RUNTIME_FILES)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(os.path.dirname(ROOT), "dist"))
    parser.add_argument("--name", default="rmtc-competition-" +
                        datetime.date.today().isoformat())
    parser.add_argument("--runtime", action="store_true",
                        help="只打现场运行所需的最小集（不给练习/测试/文档）")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    zip_path = os.path.join(args.out, args.name + ".zip")
    files = runtime_files() if args.runtime else tracked_files()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            zf.write(os.path.join(ROOT, rel.replace("/", os.sep)),
                     arcname=args.name + "/" + rel)
        if args.runtime:
            for src, dst in RUNTIME_EXTRA:
                zf.write(os.path.join(ROOT, src.replace("/", os.sep)),
                         arcname=args.name + "/" + dst)

    digest = hashlib.sha256(open(zip_path, "rb").read()).hexdigest()
    size = os.path.getsize(zip_path)
    print("打包完成：" + zip_path)
    print("文件数 %d" % len(files))
    print("大小 %.0f KB" % (size / 1024))
    print("SHA256 " + digest)
    print("顶层目录：" + args.name + "/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
