#!/bin/bash
# 模拟真实比赛（v1.1 计分口径）：裁判只当裁判，B/C/D 由我们自己的程序扮演不同强度对手。
# 用法: tourney.sh <map> <reps> <lineup> <a_real:0|1>
OUT=/tmp/tourney.txt
MAP="$1"; REPS="$2"; LINEUP="$3"; A_REAL="$4"
[ -z "$MAP" ] && MAP=official
[ -z "$REPS" ] && REPS=2
[ -z "$LINEUP" ] && LINEUP=strong,strong,strong
[ -z "$A_REAL" ] && A_REAL=0
{
  pkill -f practice.py 2>/dev/null; pkill -f contestant.bot_node 2>/dev/null; pkill -f rmtc_gateway 2>/dev/null; sleep 2
  tar -xf /tmp/repo.tar -C /home/rogers/rmtc-llm-gateway 2>/dev/null
  printf '%s\n' "sk-b88b76ca961d42dc87c753731b74ab58" > /home/rogers/.rmtc_ai_key && chmod 600 /home/rogers/.rmtc_ai_key
  cd /home/rogers/rmtc-llm-gateway
  echo '{"decision_type": "combat"}' > decision_config.json
  rm -f /tmp/gw_t.jsonl
  RMTC_GW_UPSTREAM_KEY="$(cat /home/rogers/.rmtc_ai_key)" nohup python3 -m rmtc_gateway \
    --port 8000 --tokens "A=gw-A" --quota 0 \
    --upstream https://api.deepseek.com/v1/chat/completions --model deepseek-chat \
    --upstream-timeout 25 --audit /tmp/gw_t.jsonl > /tmp/gw_t.log 2>&1 &
  for i in $(seq 1 15); do curl -s -m 1 http://127.0.0.1:8000/healthz > /dev/null 2>&1 && break; sleep 1; done

  profile_args() {
    case "$1" in
      strong)     echo "--combat hybrid --mission-max-distance 8" ;;
      medium)     echo "--combat opportunistic --mission-max-distance 8" ;;
      aggressive) echo "--combat aggressive --mission-max-distance 8" ;;
      collect)    echo "--combat never --mission-max-distance 8" ;;
      *)          echo "--combat hybrid --mission-max-distance 8" ;;
    esac
  }
  IFS=',' read -r P1 P2 P3 <<< "$LINEUP"
  CSV=/tmp/tourney.csv
  echo "map,lineup,rep,a_mode,A_score,A_rank,A_rv,A_dmg,A_missions,buff_moves,buff_attacks,B_v11,C_v11,D_v11,calls" > "$CSV"

  for REP in $(seq 1 "$REPS"); do
    for ATTEMPT in 1 2; do
      pkill -f practice.py 2>/dev/null; pkill -f contestant.bot_node 2>/dev/null; sleep 2
      ( cd /home/rogers/rmtc_practice && nohup ./start_referee.sh --stage 2 > /tmp/ref_t.log 2>&1 & )
      for i in $(seq 1 30); do curl -s -m 1 http://127.0.0.1:8080/api/state > /dev/null 2>&1 && break; sleep 1; done
      BEFORE=$(wc -l < /tmp/gw_t.jsonl 2>/dev/null || echo 0)

      source /opt/ros/humble/setup.bash
      export ROS_DOMAIN_ID=27 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
      MOCK_AI="http://127.0.0.1:8080/v1/chat/completions"
      if [ "$A_REAL" = "1" ]; then A_AI="http://127.0.0.1:8000/v1/chat/completions"; A_TOK="gw-A"
      else A_AI="$MOCK_AI"; A_TOK="practice-A"; fi

      for S in A B C D; do
        TRACE=$(printf "/tmp/trace_t_%s_%s.jsonl" "$S" "$REP")
        LOGF=$(printf "/tmp/bot_t_%s_%s.log" "$S" "$REP")
        rm -f "$TRACE"
        case $S in
          A) PROF=$(profile_args strong); URL=$A_AI; TOK=$A_TOK ;;
          B) PROF=$(profile_args "$P1"); URL=$MOCK_AI; TOK=practice-B ;;
          C) PROF=$(profile_args "$P2"); URL=$MOCK_AI; TOK=practice-C ;;
          D) PROF=$(profile_args "$P3"); URL=$MOCK_AI; TOK=practice-D ;;
        esac
        RMTC_SEAT=$S RMTC_TEAM_TOKEN=$TOK RMTC_AI_URL=$URL RMTC_TRACE=$TRACE \
          nohup python3 -m contestant.bot_node --seat $S --stage 2 $PROF > "$LOGF" 2>&1 &
      done
      # 等四席都真正收到感知再开局（真机踩过"晚 join 26 Tick，开局资源被抢光"）
      READY=0
      for w in $(seq 1 30); do
        READY=1
        for S in A B C D; do
          TF=$(printf '/tmp/trace_t_%s_%s.jsonl' "$S" "$REP")
          [ -f "$TF" ] || READY=0
        done
        [ "$READY" = "1" ] && break
        sleep 1
      done
      if [ "$READY" != "1" ]; then echo "有席位 30 秒内没收到感知，重跑本局"; continue; fi
      sleep 2

      BODY=$(python3 -c "
import json
cfg = json.load(open('/home/rogers/rmtc-llm-gateway/docs/sweep_stage2/$MAP.json'))
print(json.dumps({'stage':2,'tick_ms':100,'max_ticks':500,'bots':[],'config':cfg}))")
      START=$(curl -s -X POST http://127.0.0.1:8080/api/start -H 'Content-Type: application/json' -d "$BODY")
      case "$START" in *'"ok": true'*) ;; *) echo "start 失败 REP=$REP ATTEMPT=$ATTEMPT: $START"; continue ;; esac
      curl -s -X POST http://127.0.0.1:8080/api/resume -H 'Content-Type: application/json' -d '{}' > /dev/null
      for i in $(seq 1 90); do
        sleep 1
        FIN=$(curl -s -m 2 http://127.0.0.1:8080/api/state | python3 -c "import json,sys;print(json.load(sys.stdin)['status']['finished'])" 2>/dev/null)
        [ "$FIN" = "True" ] && break
      done
      AFTER=$(wc -l < /tmp/gw_t.jsonl 2>/dev/null || echo 0)
      curl -s http://127.0.0.1:8080/api/state > /tmp/state_t.json

      LINES=$(for S in A B C D; do wc -l < $(printf "/tmp/trace_t_%s_%s.jsonl" "$S" "$REP") 2>/dev/null || echo 0; done | sort -n | head -1)
      [ -z "$LINES" ] && LINES=0
      if [ "$LINES" -lt 100 ]; then
        echo "校验失败 REP=$REP ATTEMPT=$ATTEMPT：最少一席只有 $LINES 帧，重跑"
        continue
      fi
      python3 - "$MAP" "$LINEUP" "$REP" "$A_REAL" "$((AFTER-BEFORE))" <<'PY'
import json, sys
map_name, lineup, rep, a_real, calls = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
s = json.load(open('/tmp/state_t.json'))
teams = {t['team']: t for t in s['board']['teams']}
def v11(t):
    return 30 * min(t.get('resource_value', 0) / 120, 1) + 15 * min(t.get('effective_damage', 0) / 500, 1) \
           + 5 * t.get('mission_completed', 0)
def key(t):
    return (-v11(t), -t.get('mission_completed', 0), -t.get('resource_value', 0),
            -t.get('effective_damage', 0), t.get('invalid_actions', 0))
rank = next((i + 1 for i, t in enumerate(sorted(teams.values(), key=key)) if t['team'] == 'A'), 0)
a = teams.get('A', {})
trace = [json.loads(l) for l in open('/tmp/trace_t_A_%s.jsonl' % rep, encoding='utf-8')]
last = trace[-1]
print(f"{map_name},{lineup},{rep},{'real' if a_real=='1' else 'mock'},{round(v11(a),2)},{rank},"
      f"{a.get('resource_value')},{a.get('effective_damage')},{a.get('mission_completed')},"
      f"{last.get('buff_moves')},{last.get('buff_attacks')},"
      f"{round(v11(teams.get('B',{})),2)},{round(v11(teams.get('C',{})),2)},{round(v11(teams.get('D',{})),2)},{calls}")
PY
      echo "done $MAP $LINEUP rep=$REP attempt=$ATTEMPT"
      break
    done
  done
  echo "== CSV =="
  cat "$CSV"
  if [ "$A_REAL" = "1" ]; then echo "== 花费 =="; python3 scripts/cost.py /tmp/gw_t.jsonl; fi
  pkill -f contestant.bot_node 2>/dev/null; pkill -f practice.py 2>/dev/null; pkill -f rmtc_gateway 2>/dev/null
} > "$OUT" 2>&1
echo done
