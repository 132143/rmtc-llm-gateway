"""分析第二阶段 A/B 的 CSV（stage2_ab.sh 输出）。

用法：python3 scripts/analyze_ab.py stage2_ab.csv

会丢掉被踩断的行（resource_value 为空的那几行——两个批次并发跑时出现过）。
"""

from __future__ import annotations

import collections
import csv
import sys


def parse(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                if row["resource_value"] in (None, "", "None"):
                    continue
                rows.append({
                    "mode": row["mode"],
                    "config": row["config"],
                    "score": float(row["score"]),
                    "rv": float(row["resource_value"]),
                    "dmg": float(row["damage"]),
                    "missions": float(row["missions"]),
                    "hp": float(row["hp"]) if row["hp"] not in (None, "", "None") else 0.0,
                })
            except (TypeError, ValueError):
                continue
    return rows


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "stage2_ab.csv"
    rows = parse(path)
    if not rows:
        print("没有可用数据")
        return 1
    groups = collections.defaultdict(list)
    for row in rows:
        groups[(row["mode"], row["config"])].append(row)

    print(f"{'模式':<16}{'地图':<10}{'局数':>4}{'平均分':>9}{'最低':>7}{'最高':>7}"
          f"{'资源':>7}{'伤害':>8}{'任务':>6}")
    print("-" * 82)
    for (mode, config), items in sorted(groups.items()):
        scores = [item["score"] for item in items]
        print(f"{mode:<16}{config:<10}{len(items):>4}{sum(scores)/len(scores):>9.1f}"
              f"{min(scores):>7.1f}{max(scores):>7.1f}"
              f"{sum(i['rv'] for i in items)/len(items):>7.1f}"
              f"{sum(i['dmg'] for i in items)/len(items):>8.1f}"
              f"{sum(i['missions'] for i in items)/len(items):>6.1f}")
    print()
    by_mode = collections.defaultdict(list)
    for row in rows:
        by_mode[row["mode"]].append(row["score"])
    print("按模式总体平均：" + "　".join(
        f"{mode} {sum(v)/len(v):.1f}" for mode, v in sorted(by_mode.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
