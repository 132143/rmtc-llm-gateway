#!/usr/bin/env python3
"""把仓库打包成交付给队友的 zip（只打 git 跟踪的文件，排除开发用的虚拟机脚本）。

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
SKIP_PREFIX = ("scripts/vm/",)          # 只在虚拟机里用，比赛不需要


def tracked_files() -> list:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(os.path.dirname(ROOT), "dist"))
    parser.add_argument("--name", default="rmtc-competition-" +
                        datetime.date.today().isoformat())
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    zip_path = os.path.join(args.out, args.name + ".zip")
    files = tracked_files()
    included = [f for f in files if not f.startswith(SKIP_PREFIX)]

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in included:
            zf.write(os.path.join(ROOT, rel.replace("/", os.sep)),
                     arcname=args.name + "/" + rel)

    digest = hashlib.sha256(open(zip_path, "rb").read()).hexdigest()
    size = os.path.getsize(zip_path)
    print(f"打包完成：{zip_path}")
    print(f"文件数 {len(included)}（跳过 {len(files) - len(included)} 个开发用脚本）")
    print(f"大小 {size / 1024:.0f} KB")
    print(f"SHA256 {digest}")
    print(f"顶层目录：{args.name}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
