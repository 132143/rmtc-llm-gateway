#!/bin/bash
# 更严格的第二阶段 A/B：每一"轮"都重起裁判与选手进程，并把模式交错（消除顺序污染）。
# 用法: stage2_ab2.sh [reps] [ticks] [tickms] ["模式1 模式2 ..."] [配置目录]
# 输出 CSV: mode,config,rep,score,resource_value,damage,missions,hp

exec 9>/tmp/stage2_ab2.lock
flock -n 9 || { echo "已有一个批次在跑，退出"; exit 1; }

REPS="${1:-2}"
TICKS="${2:-200}"
TICKMS="${3:-100}"
REACH=$(( TICKS * 3 / 5 ))
# 等待上限必须跟着局时长走：200 Tick × 50 ms 约 10 秒，但正式节奏 500 × 500 ms 要 250 秒。
# 早先写死 60 次轮询，导致正式节奏的两局被截断在 tick≈120（踩过的坑）。
POLL_MAX=$(( TICKS * TICKMS / 1000 + 90 ))
CFGDIR="${5:-$HOME/rmtc-llm-gateway/docs/sweep_stage2}"
CSV=/tmp/stage2_ab2.csv
MODES="${4:-never opportunistic resource_first}"
: > "$CSV"
echo "mode,config,rep,score,resource_value,damage,missions,hp" >> "$CSV"

start_referee() {
  pkill -f practice.py 2>/dev/null
  # 必须等端口真的空出来，否则会连到"上一局的旧裁判"，读到 finished=True 直接空跑（踩过 2.5 分钟跑完 500 Tick 的坑）
  for i in $(seq 1 20); do
    curl -s -m 1 http://127.0.0.1:8080/api/state > /dev/null 2>&1 || break
    sleep 1
  done
  ( cd ~/rmtc_practice && nohup ./start_referee.sh --stage 2 > /tmp/ref_ab2.log 2>&1 & )
  for i in $(seq 1 25); do
    curl -s -m 1 http://127.0.0.1:8080/api/state > /dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# 开局后校验：真的新开了场次、真的没结束，否则重试一次
ensure_started() {
  local body="$1"
  for attempt in 1 2; do
    local sid
    sid=$(curl -s -X POST http://127.0.0.1:8080/api/start -H 'Content-Type: application/json' -d "$body" \
          | python3 -c "import json,sys;print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
    local state
    state=$(curl -s http://127.0.0.1:8080/api/state)
    local ok
    ok=$(echo "$state" | python3 -c "
import json,sys
s=json.load(sys.stdin)
print('ok' if (s['status']['finished'] is False and s['status']['tick'] < 5) else 'stale')
" 2>/dev/null)
    if [ "$ok" = "ok" ]; then
      curl -s -X POST http://127.0.0.1:8080/api/resume -H 'Content-Type: application/json' -d '{}' > /dev/null
      echo "$sid"
      return 0
    fi
    echo "start 校验失败（attempt $attempt）：读到旧场次，重来" >&2
    start_referee || return 1
    sleep 2
  done
  return 1
}

start_bot() {
  local mode="$1"
  pkill -f contestant.bot_node 2>/dev/null
  sleep 1
  ( cd ~/rmtc-llm-gateway
    source /opt/ros/humble/setup.bash
    export ROS_DOMAIN_ID=27 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
    export RMTC_SEAT=A RMTC_TEAM_TOKEN=practice-A
    export RMTC_AI_URL="http://127.0.0.1:8080/v1/chat/completions"
    nohup python3 -m contestant.bot_node --seat A --stage 2 --combat "$mode" \
      --reach-after-tick "$REACH" > "/tmp/bot_${mode}.log" 2>&1 & )
  sleep 6
}

for REP in $(seq 1 "$REPS"); do
  for MODE in $MODES; do
    start_referee || { echo "裁判起不来"; exit 1; }
    start_bot "$MODE"
    for CFG in "$CFGDIR"/*.json; do
      NAME=$(basename "$CFG" .json)
      BODY=$(python3 -c "
import json
print(json.dumps({'stage': 2, 'tick_ms': $TICKMS, 'max_ticks': $TICKS,
                  'bots': ['B','C','D'], 'config': json.load(open('$CFG'))}))
")
      ensure_started "$BODY" > /dev/null || { echo "开局失败，跳过 $MODE/$NAME"; continue; }
      for i in $(seq 1 "$POLL_MAX"); do
        sleep 1
        FIN=$(curl -s -m 2 http://127.0.0.1:8080/api/state | python3 -c "import json,sys;print(json.load(sys.stdin)['status']['finished'])" 2>/dev/null)
        [ "$FIN" = "True" ] && break
      done
      TICK_NOW=$(curl -s http://127.0.0.1:8080/api/state | python3 -c "import json,sys;print(json.load(sys.stdin)['status']['tick'])" 2>/dev/null)
      if [ "$TICK_NOW" != "$TICKS" ]; then
        echo "警告：$MODE/$NAME 只跑到 tick $TICK_NOW / $TICKS（被截断，结果不可信）" >&2
      fi
      curl -s http://127.0.0.1:8080/api/state | python3 -c "
import json,sys
s=json.load(sys.stdin)
a=[t for t in s['board']['teams'] if t['team']=='A']
a=a[0] if a else {}
print('$MODE,$NAME,$REP,', a.get('score'), ',', a.get('resource_value'), ',',
      a.get('effective_damage'), ',', a.get('mission_completed'), ',', a.get('hp'), sep='')
" >> "$CSV"
      echo "done rep$REP $MODE $NAME"
    done
  done
done
pkill -f contestant.bot_node 2>/dev/null; pkill -f practice.py 2>/dev/null
echo "== CSV =="
cat "$CSV"
