# 比赛方案（封存参数表）

> 这份是"上场就按这个跑"的定稿说明。任何现场改动只能动 `decision_config.json` 的 `decision_type`。

## 1. 程序入口

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=<现场公布>  ROS_LOCALHOST_ONLY=0  RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMTC_SEAT=<A/B/C/D>  RMTC_TEAM_TOKEN=<本场令牌>
export RMTC_AI_URL=<现场网关>/v1/chat/completions
export RMTC_TEAM_NAME='<队名>'
cd <仓库目录>
python3 -m contestant.bot_node --seat $RMTC_SEAT --stage 2
```

第一阶段把 `--stage 2` 换成 `--stage 1`（只发 MOVE/WAIT）。

## 2. 封存参数（默认值，全部在 `contestant/agent.py` 的 `AgentConfig`）

| 参数 | 值 | 理由 |
|---|---|---|
| `strategy` | `greedy`（BFS 贪心 + 记忆） | 第一阶段 9 张图全部 40/40（骨架蛇形只有 8/40） |
| `strategy_detour` | `0` | "谁近去谁"实测最好；20 掉到 40 分 |
| `combat_mode` | `hybrid`（见 decision_type） | 正式节奏实测 45.0 > opportunistic 42.0 > never 30.0 |
| `mission_max_distance` | `8` | 24×24 上资源 60→90、分数 45→52.5；18×18 无影响 |
| `patrol_when_idle` | `False` | 实测不提分（等待少了但更掉血），默认关 |
| `ai_heartbeat_tick` | `3` | 第二阶段第 3 Tick 起主动发一次调用，**保证本场 AI 资格**（否则任务分 M=0） |
| `reach_after_tick` | `250` | 第二阶段 500 Tick，后半程去任务坐标 |
| 客户端 `timeout_s` | `4.0` | 真实延迟中位 1.0~1.3s、最大 2.7s；异步不堵动作窗 |
| 调用间隔 | `0.8` 秒 | 事件窗口只有 10~16 Tick，2 秒间隔会整段错过（实测 `effects=0`） |
| 客户端 `max_calls` | `90` | 规则暂定每场 100，留 10 次余量；配合 0.8s 间隔才能在 10~16 Tick 的事件窗口内响应（**超时也会吃额度**） |
| `json_mode` | `True` | `response_format={"type":"json_object"}`，5/5 合法 |

## 3. 决策类型（现场唯一允许切换的东西）

`decision_config.json`：

```json
{"decision_type": "combat"}
```

| `decision_type` | 行为 | 什么时候选 |
|---|---|---|
| `collect` | 完全不主动进攻 | 只想稳资源分、或对抗明显打不过 |
| `balanced` | 顺路打（进射程才打） | 对手很弱/资源分散 |
| `combat`（**默认**） | 顺路打 + 资源抢完/看不见时主动找人 | 常规对抗场次；正式节奏实测最高 |

切换要**暂停期间**改文件 → 重启程序 → 请裁判登记（每场最多 2 次）。

## 4. 预期成绩（实测，练习裁判）

| 场景 | 分数 | 依据 |
|---|---|---|
| 18×18 练习图（四队争夺） | **45.0 / 60** | 资源 60（15）+ 伤害封顶（15）+ 任务 3/3（15），稳定复现 |
| 24×24（正式赛尺寸） | 中位 **47.5**、均值 **40.7**、区间 20~52.5（5 局真实 LLM） | 方差来自资源争夺；资源为 0 的局能拿满 20（到达+对抗） |
| 事件密集 | 45.0 | 与 mock 相同，全程存活 |

## 5. 赛季必须带走的三条风险

1. **AI 资格**：练习端 mock 不算正式资格；封存前必须用真实网关跑通一次（我们已用真实 DeepSeek 验证
   `ai.A.success_count=14/14`）；
2. **额度**：每场 100 次，客户端按"发出的请求"计数（60），超时也吃额度；
3. **地图尺寸**：正式第二阶段按规则是 24×24，代码不写死尺寸（从感知的 `map` 读）。
