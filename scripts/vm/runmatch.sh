#!/bin/bash
# 令牌/网关只从环境变量读：缺了就立刻停下。
# 以前这里写死了 练习令牌 + 本地地址，正式比赛直接跑会静默 401 → 本场无 AI 资格 → 任务分 0。
: "${RMTC_TEAM_TOKEN:?必须设置 RMTC_TEAM_TOKEN（练习用裁判打印的令牌）}"
: "${RMTC_AI_URL:?必须设置 RMTC_AI_URL（练习用本地裁判的 /v1/chat/completions）}"

# 用法: runmatch.sh <stage> [bots]
STAGE="${1:-1}"
BOTS="${2:-}"
OUT=/tmp/match_${STAGE}.txt
{
  pkill -f practice.py 2>/dev/null; pkill -f contestant.bot_node 2>/dev/null; pkill -f robot_node 2>/dev/null
  sleep 1
  rm -rf ~/rmtc-llm-gateway; mkdir -p ~/rmtc-llm-gateway
  tar -xf /tmp/repo.tar -C ~/rmtc-llm-gateway || exit 1

  cd ~/rmtc_practice || exit 1
  ARGS="--stage $STAGE"
  [ -n "$BOTS" ] && ARGS="$ARGS --bots $BOTS"
  nohup ./start_referee.sh $ARGS > /tmp/ref.log 2>&1 &
  for i in $(seq 1 30); do curl -s -m 1 http://127.0.0.1:8080/api/state > /dev/null 2>&1 && break; sleep 1; done

  cd ~/rmtc-llm-gateway
  source /opt/ros/humble/setup.bash
  export ROS_DOMAIN_ID=27 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
  nohup python3 -m contestant.bot_node --seat A --stage $STAGE > /tmp/bot.log 2>&1 &
  sleep 6

  curl -s -X POST http://127.0.0.1:8080/api/start -H 'Content-Type: application/json' -d '{"stage":'"$STAGE"'}' > /tmp/start.json
  curl -s -X POST http://127.0.0.1:8080/api/resume -H 'Content-Type: application/json' -d '{}' > /dev/null

  for i in $(seq 1 60); do
    sleep 2
    FIN=$(curl -s -m 2 http://127.0.0.1:8080/api/state | python3 -c "import json,sys;print(json.load(sys.stdin)['status']['finished'])" 2>/dev/null)
    [ "$FIN" = "True" ] && break
  done

  curl -s http://127.0.0.1:8080/api/state > /tmp/state_$STAGE.json
  echo "== stage $STAGE 结果 =="
  python3 - <<'PY'
import json, os, glob
s = json.load(open(os.environ["STATE_FILE"]))
print("finished:", s["status"]["finished"], "tick:", s["status"]["tick"], "/", s["status"]["max_ticks"])
for team in s["board"]["teams"]:
    print(json.dumps(team, ensure_ascii=False))
print("checks A:", json.dumps(s.get("checks", {}).get("A", {}), ensure_ascii=False))
sid = s["session_id"]
d = os.path.expanduser("~/.rmtc-practice/" + sid)
comm = os.path.join(d, "communication.jsonl")
if os.path.exists(comm):
    perc = acts = 0
    last = None
    for line in open(comm, encoding="utf-8"):
        try: rec = json.loads(line)
        except Exception: continue
        if rec.get("kind") == "perception":
            perc += 1; last = rec["data"]
        elif rec.get("kind") == "action":
            acts += 1
    print(f"感知 {perc} 条 / 动作 {acts} 条")
    if last:
        print("最后感知 score:", json.dumps(last.get("score"), ensure_ascii=False))
        print("最后 self:", json.dumps({k: last["self"].get(k) for k in ("position","hp","alive","last_action")}, ensure_ascii=False))
PY
  echo "== bot 日志尾部 =="
  tail -4 /tmp/bot.log
  pkill -f contestant.bot_node 2>/dev/null; pkill -f practice.py 2>/dev/null
} > "$OUT" 2>&1
cat "$OUT"
