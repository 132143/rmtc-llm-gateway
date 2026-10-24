# 第二阶段 A/B：战斗模式对照

## 怎么测的

- 裁判：官方练习包（`start_referee.sh --stage 2`），陪练车 B/C/D 由裁判启动。
- 比赛：`POST /api/start` 带 `{"stage":2,"tick_ms":100,"max_ticks":200,"bots":["B","C","D"],"config":...}`，
  即 **200 Tick × 100 ms = 20 秒一局**（默认正式节奏是 500 Tick × 500 ms；规则不变，只是快进）。
- 我方：`python3 -m contestant.bot_node --seat A --stage 2 --combat <模式> --reach-after-tick 120`。
- 地图：`docs/sweep_stage2/official.json` 与 `open.json`（资源集中在中路，冲突最多）。
- 脚本：`scripts/vm/stage2_ab.sh <重复次数> 200 100`，分析：`python3 scripts/analyze_ab.py <csv>`。

## 四种模式的差别（代码在 `contestant/agent.py`）

| 模式 | 什么时候打人 |
|---|---|
| `never` | 从不进攻，只采集（但血少仍会防御） |
| `opportunistic` | 敌人进入直线 1~2 格就打 |
| `resource_first` | 资源价值达到 120（资源分满分线）之前不打 |
| `aggressive` | 附近 4 格内有敌人就主动靠过去 |

## 结果（每格 2 次重复，单批干净数据）

| 模式 | official | open（资源集中） |
|---|---|---|
| never | 30.0 / 30.0（rv 60, dmg 0） | 15.0 / 7.5（rv 20→10, dmg 0） |
| opportunistic | **32.5 / 34.0**（rv 60, dmg 50→80） | **20.0 / 20.0**（rv 0, dmg 340） |
| resource_first | 30.0 / 30.0（rv 60, dmg 0） | 17.5 / 17.5（rv 30, m2） |

单局趋势（更早、只跑一次的对照，用来看方差有多大）：同一模式下 open 图曾出现
35.0 / 22.5 / 20.0 三种结果——**对手车挡路与站位会让同一策略差 10 分以上**，
所以单局结论不可信，上表至少是同一批里连续跑出来的。

## 目前能下的结论

1. **主动进攻不会亏**：official 上 rv 没掉（60），伤害白拿 50~80；open 上资源本来就被
   对手堵住，攻击反而把分数从 15 拉到 20。
2. **"先采集再打"（resource_first）在资源被堵的图上更差**：它把前 120 点资源当硬门槛，
   而 open 图里资源根本抢不到，结果既没资源也没伤害。
3. 因此默认模式从 `resource_first` **改回 `opportunistic`**（见 `AgentConfig`）。
   更聪明的"按时间/局势决定开打"还需要一个可靠的剩余 Tick 估计——协议里没有总 Tick 字段，
   只能靠经验值，先不做。

## 还没测的

- 正式节奏（500 Tick × 500 ms）下的完整对局（成本高，只跑过一次 60/60）。
- 真实 LLM（现在全是裁判 mock，不计入正式 AI 资格）。
- 对手是"人类水平策略"而不是裁判参考车。
