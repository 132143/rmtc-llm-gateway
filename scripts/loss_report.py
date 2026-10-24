"""丢分概率报告：以"扣分几率"为目标时该看的不是均值。

读法：
  · 任务分是 5 分/项、按 ID 结算；只要没完成就固定 −5，与"丢多少伤害/资源"无关。
  · 所以这里直接统计 P(少完成一项) 与缺的是哪一个 ID，并对两组做配对（同一局两边比）。

用法：
    python -m scripts.loss_report docs/experiments/portfolio/lab_raw.json guarded_hybrid guarded_no_portfolio
"""

from __future__ import annotations

import argparse
import json
import statistics

ALL_IDS = ("collect_two_normal", "collect_high_value", "reach_target")


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def by_group(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
    return groups


def summarize(group, rows):
    n = len(rows)
    missions = [row["missions"] for row in rows]
    losses = sum(1 for m in missions if m < 3)
    missing = {mid: 0 for mid in ALL_IDS}
    for row in rows:
        ids = set(row.get("mission_ids") or [])
        for mid in ALL_IDS:
            if mid not in ids:
                missing[mid] += 1
    scores = [row["group_score"] for row in rows]
    return {
        "group": group,
        "n": n,
        "p_loss": losses / n if n else 0.0,
        "losses": losses,
        "missing": missing,
        "mean": statistics.fmean(scores) if scores else 0.0,
        "min": min(scores) if scores else 0.0,
        "stdev": statistics.pstdev(scores) if len(scores) > 1 else 0.0,
    }


def paired(rows, group_a, group_b):
    """同一局配对比：只看"任务全绿 vs 丢任务"的翻转（McNemar 式的方向计数）。"""
    index_a = {(r["env"], r["opponent"], r["seed"], r["rep"]): r for r in rows if r["group"] == group_a}
    index_b = {(r["env"], r["opponent"], r["seed"], r["rep"]): r for r in rows if r["group"] == group_b}
    keys = sorted(set(index_a) & set(index_b))
    a_only_good = b_only_good = both_good = both_bad = 0
    for key in keys:
        good_a = index_a[key]["missions"] >= 3
        good_b = index_b[key]["missions"] >= 3
        if good_a and good_b:
            both_good += 1
        elif good_a and not good_b:
            a_only_good += 1
        elif not good_a and good_b:
            b_only_good += 1
        else:
            both_bad += 1
    return {"n": len(keys), "both_good": both_good, "both_bad": both_bad,
            "a_only_good": a_only_good, "b_only_good": b_only_good}


def sign_p(wins, losses):
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    total, prob = 0.0, 1.0
    for i in range(k + 1):
        if i:
            prob = prob * (n - i + 1) / i
        total += prob
    return min(1.0, 2.0 * total / (2 ** n))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("group_a")
    parser.add_argument("group_b", nargs="?")
    args = parser.parse_args(argv)

    rows = load(args.path)
    groups = by_group(rows)
    print("| 分组 | 局数 | P(少完成一项) | 缺 normal | 缺 high | 缺 reach | 均值 | 最低 | 标准差 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for group in sorted(groups):
        stat = summarize(group, groups[group])
        print("| {0} | {1} | **{2:.1%}** ({3}/{1}) | {4} | {5} | {6} | {7:.2f} | {8:.1f} | {9:.2f} |".format(
            stat["group"], stat["n"], stat["p_loss"], stat["losses"],
            stat["missing"]["collect_two_normal"], stat["missing"]["collect_high_value"],
            stat["missing"]["reach_target"], stat["mean"], stat["min"], stat["stdev"]))

    if args.group_b:
        cross = paired(rows, args.group_a, args.group_b)
        wins, losses = cross["a_only_good"], cross["b_only_good"]
        print("\n配对（同一局比较：任务是否全绿） {0} vs {1}".format(args.group_a, args.group_b))
        print("  两边都全绿 {0} 局 / 都丢 {1} 局 / 只有 A 全绿 {2} 局 / 只有 B 全绿 {3} 局".format(
            cross["both_good"], cross["both_bad"], wins, losses))
        print("  方向性检验 p = {0:.4f}（只看翻转的局，{1} 胜 {2} 负）".format(sign_p(wins, losses), wins, losses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
