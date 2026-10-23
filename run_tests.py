#!/usr/bin/env python3
"""一条命令跑完全部测试：python3 run_tests.py -v"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> int:
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.TestLoader().discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
