#!/bin/bash
# 令牌/网关只从环境变量读：缺了就立刻停下。
# 以前这里写死了 练习令牌 + 本地地址，正式比赛直接跑会静默 401 → 本场无 AI 资格 → 任务分 0。
: "${RMTC_TEAM_TOKEN:?必须设置 RMTC_TEAM_TOKEN（练习用裁判打印的令牌）}"
: "${RMTC_AI_URL:?必须设置 RMTC_AI_URL（练习用本地裁判的 /v1/chat/completions）}"

# 在官方练习裁判上扫一批第一阶段的图，跑同一个策略。
# 用法: runsweep.sh <strategy: greedy|snake> [地图目录]
# 结果追加到 /tmp/sweep_<strategy>.txt，一行一局：名字 得分 资源 非法动作 感知数
# 注意：不要开 set -u —— source /opt/ros/humble/setup.bash 会引用未定义变量
STRATEGY="${1:-greedy}"
MAPDIR="${2:-$HOME/rmtc-llm-gateway/docs/sweep_maps}"
RESULTS=/tmp/sweep_${STRATEGY}.txt
: > "$RESULTS"

pkill -f practice.py 2>/dev/null; pkill -f contestant.bot_node 2>/dev/null
sleep 1

cd ~/rmtc-llm-gateway || exit 1
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=27 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
nohup python3 -m contestant.bot_node --seat A --stage 1 --strategy "$STRATEGY" > /tmp/bot_sweep.log 2>&1 &
sleep 6

for map in "$MAPDIR"/*.json; do
  name=$(basename "$map" .json)
  pkill -f practice.py 2>/dev/null
  sleep 1
  ( cd ~/rmtc_practice && nohup ./start_referee.sh --stage 1 --config "$map" > /tmp/ref_${name}.log 2>&1 & )
  for i in $(seq 1 25); do curl -s -m 1 http://127.0.0.1:8080/api/state > /dev/null 2>&1 && break; sleep 1; done
  sleep 2
  curl -s -X POST http://127.0.0.1:8080/api/start -H 'Content-Type: application/json' -d '{"stage":1}' > /dev/null
  curl -s -X POST http://127.0.0.1:8080/api/resume -H 'Content-Type: application/json' -d '{}' > /dev/null
  for i in $(seq 1 40); do
    sleep 1
    FIN=$(curl -s -m 2 http://127.0.0.1:8080/api/state | python3 -c "import json,sys;print(json.load(sys.stdin)['status']['finished'])" 2>/dev/null)
    [ "$FIN" = "True" ] && break
  done
  curl -s http://127.0.0.1:8080/api/state | python3 -c "
import json,sys
s=json.load(sys.stdin)
t=s['board']['teams'][0] if s['board']['teams'] else {}
print('$name', t.get('score'), t.get('resource_value'), t.get('invalid_actions'),
      'tick', s['status']['tick'], '/', s['status']['max_ticks'])
" >> "$RESULTS"
  echo "done $name"
done
pkill -f contestant.bot_node 2>/dev/null
pkill -f practice.py 2>/dev/null
echo "== $STRATEGY 结果 =="
cat "$RESULTS"
