#!/bin/bash
# 令牌/网关只从环境变量读：缺了就立刻停下。
# 以前这里写死了 练习令牌 + 本地地址，正式比赛直接跑会静默 401 → 本场无 AI 资格 → 任务分 0。
: "${RMTC_TEAM_TOKEN:?必须设置 RMTC_TEAM_TOKEN（练习用裁判打印的令牌）}"
: "${RMTC_AI_URL:?必须设置 RMTC_AI_URL（练习用本地裁判的 /v1/chat/completions）}"

OUT=/tmp/match1_diag.txt
{
  echo "== /api/state 全文（截断 3000）=="
  head -c 3000 /tmp/state.json; echo
  echo
  echo "== 本场 session 目录 =="
  SID=$(python3 -c "import json;print(json.load(open('/tmp/state.json')).get('session_id',''))" 2>/dev/null)
  echo "session_id=$SID"
  D=~/.rmtc-practice/$SID
  ls -la "$D" 2>/dev/null | head
  echo "== 感知/动作条数 =="
  grep -c perception "$D/communication.jsonl" 2>/dev/null
  grep -c action "$D/communication.jsonl" 2>/dev/null
  echo "== 最后一条感知的 score / self =="
  grep '"kind": "perception"' "$D/communication.jsonl" 2>/dev/null | tail -1 | python3 -c "
import json,sys
line=sys.stdin.read().strip()
if not line: print('no perception'); raise SystemExit
d=json.loads(line)['data']
print('tick', d.get('tick'), 'score', d.get('score'))
print('self', {k: d['self'].get(k) for k in ('position','hp','alive','last_action')})
print('entities', len(d.get('visible_entities') or []))
"
  echo "== 最后 3 条动作 =="
  grep '"kind": "action"' "$D/communication.jsonl" 2>/dev/null | tail -3 | cut -c1-200
} > "$OUT" 2>&1
cat "$OUT"
