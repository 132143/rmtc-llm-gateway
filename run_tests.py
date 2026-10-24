#!/usr/bin/env python3
"""一条命令跑完全部测试：python3 run_tests.py -v"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))

# Windows 控制台默认是 GBK，打印中文测试说明会崩；统一转 UTF-8（不可用时忽略）
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def syntax_check(root: str) -> list:
    """先编译全部 .py：这种"字符串里有真换行"的低级错误，单元测试覆盖不到的文件会漏掉。"""
    broken = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "dist")]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            try:
                with open(path, "rb") as handle:
                    compile(handle.read(), path, "exec")     # 只在内存里编译，不落 .pyc
            except SyntaxError as exc:
                broken.append(f"{os.path.relpath(path, root)}:{exc.lineno}: {exc.msg}")
    return broken


def main() -> int:
    verbosity = 2 if "-v" in sys.argv else 1
    broken = syntax_check(ROOT)
    if broken:
        print("语法检查失败：")
        for item in broken:
            print("  ", item)
        return 1
    print(f"语法检查通过（{sum(1 for b, d, f in os.walk(ROOT) for n in f if n.endswith('.py'))} 个 .py）")
    suite = unittest.TestLoader().discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
