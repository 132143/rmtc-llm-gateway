#!/bin/bash
# 第二阶段 A/B：多种战斗模式 × 多张图 × 多次重复，输出 CSV。
# 用法: stage2_ab.sh [repeats] [ticks] [tickms]
REPEATS="${1:-3}"
TICKS="${2:-200}"
TICKMS="${3:-100}"
REACH=$(( TICKS * 3 / 5 ))
CFGDIR="$HOME/rmtc-llm-gateway/docs/sweep_stage2"
CSV=/tmp/stage2_ab.csv
: > "$CSV"
echo "mode,config,rep,score,resource_value,damage,missions,hp" >> "$CSV"

pkill -f practice.py 2>/dev/null; pkill -f contestant.bot_node 2>/dev/null; pkill -f robot_node 2>/dev/null
sleep 1

cd ~/rmtc_practice
nohup ./start_referee.sh --stage 2 > /tmp/ref_ab.log 2>&1 &
for i in $(seq 1 25); do curl -s -m 1 http://127.0.0.1:8080/api/state > /dev/null 2>&1 && break; sleep 1; done

cd ~/rmtc-llm-gateway
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=27 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMTC_SEAT=A RMTC_TEAM_TOKEN=practice-A
export RMTC_AI_URL="http://127.0.0.1:8080/v1/chat/completions"

for MODE in never opportunistic resource_first; do
  pkill -f contestant.bot_node 2>/dev/null
  sleep 1
  nohup python3 -m contestant.bot_node --seat A --stage 2 --combat "$MODE" \
    --reach-after-tick "$REACH" > "/tmp/bot_${MODE}.log" 2>&1 &
  sleep 6
  for REP in $(seq 1 "$REPEATS"); do
    for CFG in "$CFGDIR"/official.json "$CFGDIR"/open.json; do
      NAME=$(basename "$CFG" .json)
      BODY=$(python3 -c "
import json
print(json.dumps({'stage': 2, 'tick_ms': $TICKMS, 'max_ticks': $TICKS,
                  'bots': ['B','C','D'], 'config': json.load(open('$CFG'))}))
")
      curl -s -X POST http://127.0.0.1:8080/api/start -H 'Content-Type: application/json' -d "$BODY" > /dev/null
      curl -s -X POST http://127.0.0.1:8080/api/resume -H 'Content-Type: application/json' -d '{}' > /dev/null
      for i in $(seq 1 60); do
        sleep 1
        FIN=$(curl -s -m 2 http://127.0.0.1:8080/api/state | python3 -c "import json,sys;print(json.load(sys.stdin)['status']['finished'])" 2>/dev/null)
        [ "$FIN" = "True" ] && break
      done
      curl -s http://127.0.0.1:8080/api/state | python3 -c "
import json,sys
s=json.load(sys.stdin)
a=[t for t in s['board']['teams'] if t['team']=='A']
a=a[0] if a else {}
print('$MODE,$NAME,$REP,', a.get('score'), ',', a.get('resource_value'), ',',
      a.get('effective_damage'), ',', a.get('mission_completed'), ',', a.get('hp'), sep='')
" >> "$CSV"
      echo "done $MODE $NAME #$REP"
    done
  done
done
pkill -f contestant.bot_node 2>/dev/null; pkill -f practice.py 2>/dev/null
echo "== CSV =="
cat "$CSV"
