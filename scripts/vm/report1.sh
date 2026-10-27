#!/bin/bash
# 令牌/网关只从环境变量读：缺了就立刻停下。
# 以前这里写死了 练习令牌 + 本地地址，正式比赛直接跑会静默 401 → 本场无 AI 资格 → 任务分 0。
: "${RMTC_TEAM_TOKEN:?必须设置 RMTC_TEAM_TOKEN（练习用裁判打印的令牌）}"
: "${RMTC_AI_URL:?必须设置 RMTC_AI_URL（练习用本地裁判的 /v1/chat/completions）}"

OUT=/tmp/report1.txt
{
  python3 - <<'PY'
import json, os
s = json.load(open("/tmp/state_1.json"))
print("finished:", s["status"]["finished"], "tick:", s["status"]["tick"], "/", s["status"]["max_ticks"])
for team in s["board"]["teams"]:
    print(json.dumps(team, ensure_ascii=False))
print("checks A:", json.dumps(s.get("checks", {}).get("A", {}), ensure_ascii=False))
d = os.path.expanduser("~/.rmtc-practice/" + s["session_id"])
comm = os.path.join(d, "communication.jsonl")
if os.path.exists(comm):
    perc = acts = 0; last = None
    for line in open(comm, encoding="utf-8"):
        try: rec = json.loads(line)
        except Exception: continue
        if rec.get("kind") == "perception": perc += 1; last = rec["data"]
        elif rec.get("kind") == "action": acts += 1
    print(f"感知 {perc} 条 / 动作 {acts} 条")
    if last:
        print("最后感知 score:", json.dumps(last.get("score"), ensure_ascii=False))
        print("最后 self:", json.dumps({k: last["self"].get(k) for k in ("position","hp","alive","last_action")}, ensure_ascii=False))
PY
} > "$OUT" 2>&1
cat "$OUT"
