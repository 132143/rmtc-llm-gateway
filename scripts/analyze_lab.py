"""成对比较分析：把同一批种子下的两组结果配对，判断差异是不是噪声。

为什么需要它：一轮 48 局的均值标准差约 8 分，均值差 2 分只是 0.25σ —— 单看均值会自欺。
成对比较（同图同对手同种子）能消掉"这局地图太糟"这类公共噪声，再用
符号检验（sign test）与 bootstrap 置信区间回答"到底是真差异还是运气"。

用法：
    python -m scripts.analyze_lab docs/experiments/round2/lab_raw.json guarded_hybrid baseline_hybrid
    python -m scripts.analyze_lab docs/experiments/final/lab_raw.json guarded_hybrid baseline_hybrid --bootstrap 4000
"""

from __future__ import annotations

import argparse
import json
import random
import statistics


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def index(rows, group):
    """键 = (地图, 对手, 种子, 重复)；确保两边比的是同一局。"""
    out = {}
    for row in rows:
        if row.get("group") != group:
            continue
        key = (row["env"], row["opponent"], row["seed"], row["rep"])
        out[key] = row
    return out


def sign_test_p(wins, losses):
    """双边符号检验的精确二项 p 值（n 小，直接算）。"""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    total = 0
    prob = 1.0
    for i in range(k + 1):
        if i:
            prob = prob * (n - i + 1) / i
        total += prob
    return min(1.0, 2.0 * total / (2 ** n))


def bootstrap_ci(values, rounds=2000, alpha=0.05, seed=12345):
    rng = random.Random(seed)
    means = []
    for _ in range(rounds):
        sample = [values[rng.randrange(len(values))] for _ in range(len(values))]
        means.append(statistics.fmean(sample))
    means.sort()
    lo = means[int(rounds * (alpha / 2))]
    hi = means[int(rounds * (1 - alpha / 2)) - 1]
    return lo, hi


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("group_a")
    parser.add_argument("group_b")
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--metric", default="group_score")
    args = parser.parse_args(argv)

    rows = load(args.path)
    a = index(rows, args.group_a)
    b = index(rows, args.group_b)
    keys = sorted(set(a) & set(b))
    if not keys:
        print("两组没有可配对的局（检查批次文件与分组名）")
        return 1

    diffs, wins, losses, ties = [], 0, 0, 0
    for key in keys:
        delta = float(a[key][args.metric]) - float(b[key][args.metric])
        diffs.append(delta)
        if delta > 1e-9:
            wins += 1
        elif delta < -1e-9:
            losses += 1
        else:
            ties += 1

    mean_diff = statistics.fmean(diffs)
    lo, hi = bootstrap_ci(diffs, rounds=max(200, args.bootstrap))
    p = sign_test_p(wins, losses)
    print("配对局数: {0}（{1} vs {2}，指标 {3}）".format(len(keys), args.group_a, args.group_b, args.metric))
    print("平均差值: {0:+.2f} 分   95% 置信区间 [{1:+.2f}, {2:+.2f}]".format(mean_diff, lo, hi))
    print("胜负: {0} 胜 / {1} 负 / {2} 平   符号检验 p = {3:.4f}".format(wins, losses, ties, p))
    print("读法: 置信区间不含 0 且 p < 0.05 才算有统计支撑的差异；否则只能说方向一致、样本不足。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
