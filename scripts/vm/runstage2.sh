#!/bin/bash
# 令牌/网关只从环境变量读：缺了就立刻停下。
# 以前这里写死了 练习令牌 + 本地地址，正式比赛直接跑会静默 401 → 本场无 AI 资格 → 任务分 0。
: "${RMTC_TEAM_TOKEN:?必须设置 RMTC_TEAM_TOKEN（练习用裁判打印的令牌）}"
: "${RMTC_AI_URL:?必须设置 RMTC_AI_URL（练习用本地裁判的 /v1/chat/completions）}"

# 第二阶段 A/B：跑一种战斗模式，扫一批配置。
# 用法: runstage2.sh <combat: never|opportunistic|aggressive>
MODE="${1:-opportunistic}"
TICKS="${2:-200}"
TICKMS="${3:-100}"
REACH="$(( TICKS * 3 / 5 ))"
CFGDIR="$HOME/rmtc-llm-gateway/docs/sweep_stage2"
RESULTS=/tmp/stage2_${MODE}.txt
: > "$RESULTS"

pkill -f practice.py 2>/dev/null; pkill -f contestant.bot_node 2>/dev/null; pkill -f robot_node 2>/dev/null
sleep 1

cd ~/rmtc-llm-gateway || exit 1
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=27 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
nohup python3 -m contestant.bot_node --seat A --stage 2 --combat "$MODE" --reach-after-tick "$REACH" \
  > /tmp/bot_stage2_${MODE}.log 2>&1 &
sleep 6

cd ~/rmtc_practice
nohup ./start_referee.sh --stage 2 > /tmp/ref_stage2.log 2>&1 &
for i in $(seq 1 25); do curl -s -m 1 http://127.0.0.1:8080/api/state > /dev/null 2>&1 && break; sleep 1; done

for cfg in "$CFGDIR"/*.json; do
  name=$(basename "$cfg" .json)
  BODY=$(python3 -c "
import json
print(json.dumps({'stage': 2, 'tick_ms': $TICKMS, 'max_ticks': $TICKS,
                  'bots': ['B','C','D'],
                  'config': json.load(open('$cfg'))}))
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
ai=(s.get('ai') or {}).get('A') or {}
print('$name', 'score', a.get('score'), 'rv', a.get('resource_value'),
      'dmg', a.get('effective_damage'), 'missions', a.get('mission_completed'),
      'invalid', a.get('invalid_actions'), 'hp', a.get('hp'),
      'ai_ok', ai.get('success_count'), '/', ai.get('attempt_count'))
" >> "$RESULTS"
  echo "done $name"
done
pkill -f contestant.bot_node 2>/dev/null; pkill -f practice.py 2>/dev/null
echo "== $MODE 结果 =="
cat "$RESULTS"
