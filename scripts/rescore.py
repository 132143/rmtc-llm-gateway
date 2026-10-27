"""练习成绩换算：把裁判按旧口径（D/300 封顶）算出的分，换算成规则 v1.2 的 D/500 口径。

为什么需要它：练习包里的 rules.txt 还是 v1.0 的 300 封顶，而正式规则 v1.2 是 500。
同一个局面（例如 D=320）在练习裁判里给 15 分，按正式规则只有 9.6 分——
**用练习分数判断"我是不是满分"会误判**。

用法：
    python -m scripts.rescore /tmp/state_2.json            # 裁判 /api/state 的返回值
    python -m scripts.rescore docs/evidence/match.json --stage2-key board
"""

from __future__ import annotations

import argparse
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

R_FULL = 120.0
D_FULL_V12 = 500.0     # 规则 v1.1 起（含 v1.2）
D_FULL_V10 = 300.0     # 练习包 rules.txt 用的旧口径


def score(resource_value, damage, missions, d_full):
    r = 30.0 * min(float(resource_value) / R_FULL, 1.0)
    d = 15.0 * min(float(damage) / d_full, 1.0)
    m = 5.0 * float(missions)
    return round(r + d + m, 2)


def teams_from(payload):
    """兼容三种输入：/api/state、单场 score.json、以及 {"teams": [...]}。"""
    if isinstance(payload, dict):
        if isinstance(payload.get("board"), dict) and "teams" in payload["board"]:
            return payload["board"]["teams"]
        if isinstance(payload.get("teams"), list):
            return payload["teams"]
        if isinstance(payload.get("seats"), dict):
            return [{"team": seat, **info} for seat, info in payload["seats"].items()]
    raise SystemExit("看不懂这个 JSON：需要 /api/state（含 board.teams）或租户结算 JSON")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args(argv)
    payload = json.load(open(args.path, encoding="utf-8"))
    teams = teams_from(payload)
    rows = []
    for team in teams:
        rv = float(team.get("resource_value") or 0)
        dmg = float(team.get("effective_damage") or 0)
        missions = int(team.get("mission_completed") or 0)
        old = team.get("score")
        rows.append({
            "team": team.get("team") or team.get("seat"),
            "resource_value": rv, "damage": dmg, "missions": missions,
            "reported_score": old,
            "score_v10_300cap": score(rv, dmg, missions, D_FULL_V10),
            "score_v12_500cap": score(rv, dmg, missions, D_FULL_V12),
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print("%-6s %8s %8s %4s %12s %12s %10s" % ("席位", "资源R2", "伤害D", "任务",
                                               "练习口径(300)", "v1.2口径(500)", "裁判报的"))
    for row in rows:
        print("%-6s %8.0f %8.0f %4d %12.2f %12.2f %10s" % (
            row["team"], row["resource_value"], row["damage"], row["missions"],
            row["score_v10_300cap"], row["score_v12_500cap"], row["reported_score"]))
    gap = [abs(r["score_v10_300cap"] - r["score_v12_500cap"]) for r in rows]
    if gap and max(gap) > 0.005:
        print("\n注意：最大差异 %.2f 分（伤害落在 300~500 区间时，练习分偏高）。正式成绩以 v1.2 口径为准。" % max(gap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
