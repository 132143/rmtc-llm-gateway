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


RUNTIME_DIRS = ("contestant/", "rmtc_client/", "scripts/preflight.py",
                "scripts/trace_view.py")
RUNTIME_FILES = ("decision_config.json", "SEALED_VERSION.txt")
RUNTIME_EXTRA = (("docs/RUN_ON_SITE.md", "RUN_ON_SITE.md"),)


def runtime_files() -> list:
    """现场最小集：只含 bot_node / preflight / trace_view 及其依赖。

    刻意**不按 import 闭包裁**（闭包会把离线仿真模块也带进来，且 lazy import
    在运行期才发现缺文件）。这里按"目录 + 白名单"取，剩 20 个文件左右。
    """
    out = []
    for rel in tracked_files():
        if rel.startswith(RUNTIME_DIRS) or rel in RUNTIME_FILES:
            out.append(rel)
    return out


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
