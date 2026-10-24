# RMTC 机甲大师图灵对决赛 · 完整交接文档

> 最后更新：2026-10-23 深夜　|　适用规则：**赛事规则 v1.1 + 通信协议 v1.1（v1.0 已作废）**
> 读者：队友 / 接手的人 / 未来的自己。**先看第 0 节，再看第 3、6 节。**

---

## 0. 一分钟速览

| 项目 | 状态 |
|---|---|
| 代码仓库 | https://github.com/132143/rmtc-llm-gateway（默认分支 main） |
| 交付包 | `rmtc-competition-2026-10-23.zip`（71 个文件 / 约 180 KB，含本文件） |
| 测试 | **119 个用例全绿** + 32 个 `.py` 全量语法检查 |
| 第一阶段（独立采集） | **40/40**，9 张不同地图全部满分（骨架蛇形 8/40） |
| 第二阶段（对练习陪练） | **45.0 / 60**（v1.1 口径同值） |
| 第二阶段（四席同算法自博弈，最难） | **42.5 ×5 局，无低分**（该环境理论上限 45） |
| 第二阶段（24×24 真实 LLM） | 均值 **45.3~48.1**，区间 **41.7~52.5** |
| AI 资格 | 裁判端 `--ai-mode real` 实测 `success_count = 14/14` |
| 累计 API 花费 | ≈ **¥4.6 / ¥10** |

**一句话**：能跑、练习场能满分、强对手环境下稳定在中上（第 1~2 名）；
剩下的提升空间在"抢资源份额"，不在正确性。

---

## 1. 仓库结构（三块，互不依赖）

| 块 | 目录 | 作用 |
|---|---|---|
| ① 网关服务端 | `rmtc_gateway/` | 协议 §5 的 OpenAI 兼容网关（鉴权/额度/限流/错误码/审计），练习验证用 |
| ② 网关客户端 | `rmtc_client/` | 调 AI 的那半：异步、4s 超时、预算、降级、JSON 修复、事件理解 |
| ③ 参赛程序 | `contestant/` | `agent.py`（感知→动作）、`strategy.py`（BFS 贪心）、`bot_node.py`（ROS 节点）、`decision.py`（决策类型）、`sim.py`（第一阶段模拟器） |
| 测试 | `tests/` | 网关 26 / 客户端 20 / 参赛 40+ / 决策 5 |
| 文档 | `docs/` | 见第 10 节索引 |
| 脚本 | `scripts/` | 地图/配置生成、A/B、打包、会话分析；`scripts/vm/` 是虚拟机专用（不进交付包） |

入口只有两个：

```bash
python3 -m contestant.bot_node --seat A --stage 2   # 参赛程序（Ubuntu + ROS 2 Humble）
python3 -m rmtc_gateway --port 8000                 # 本地网关（任意系统）
python run_tests.py                                  # 119 个测试 + 全量语法检查
```

---

## 2. 上场前 3 分钟自检

1. `python run_tests.py` → 必须全绿；
2. `python3 -m contestant.bot_node --seat A --stage 2 --dry-run` → 能打印动作且不发布；
3. 正式跑一局练习裁判，核对四件事：① 收到感知（首帧 `tick=0`）② 网页"执行 Tick"有值
   ③ 下一帧 `self.last_action` 是你的动作 ④ 第二阶段本席位**真实 AI 成功 ≥1**（第 3 Tick 有心跳）。

---

## 3. 比赛方案（封存参数，现场只改 decision_type）

### 3.1 启动

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=<现场公布>  ROS_LOCALHOST_ONLY=0  RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMTC_SEAT=<A/B/C/D>  RMTC_TEAM_TOKEN=<本场令牌>
export RMTC_AI_URL=<现场网关>/v1/chat/completions
export RMTC_TEAM_NAME='<队名>'
cd <仓库目录>
python3 -m contestant.bot_node --seat $RMTC_SEAT --stage 2
# 第一阶段：把 --stage 2 换成 --stage 1（只发 MOVE/WAIT）
```

**必须开局前启动**：实测晚 join 26 Tick → 开局资源被抢光（资源 0、任务 0/3、8.1 分）。

### 3.2 参数表（`AgentConfig` 默认值）

| 参数 | 值 | 依据 |
|---|---|---|
| `strategy` | `greedy`（BFS + 记忆 + 临时障碍） | 第一阶段 9 图 40/40（蛇形 8/40） |
| `strategy_detour` | `0` | 24×24：0→47.5、6→47.5、20→40.0 |
| `combat_mode` | `hybrid` | 正式节奏 45.0 > 42.0 > 30.0 |
| `mission_max_distance` | `8` | 24×24：45→52.5；18×18 无影响 |
| `patrol_when_idle` | `False` | 实测不提分、更易掉血 |
| `ai_heartbeat_tick` | `3` | 保证本场真实 AI 调用（否则任务分 M=0） |
| `reach_after_tick` | `250` | 大图另有无资源提前去规则（§3.4） |
| 客户端 `timeout_s` | `4.0` | 实测延迟中位 1.0~1.3s、最大 2.7s；异步不堵动作窗 |
| 客户端 `max_calls` | `90` | 规则暂定 100/场；**超时也吃额度**，按"发出的请求"计 |
| 客户端 `min_interval_s` | `0.8` | 事件窗口仅 10~16 Tick，2 秒会整段错过 |
| `json_mode` | `True` | `response_format=json_object`，5/5 合法 |

### 3.3 决策类型（现场唯一允许切换，规则 §2.3）

`decision_config.json` → `{"decision_type": "combat"}`

| 类型 | 行为 | 何时用 |
|---|---|---|
| `collect` | 完全不主动进攻 | 只想稳资源分 |
| `balanced` | 顺路打 | 对手很弱 / 资源分散 |
| `combat`（默认） | 顺路打 + 没资源时主动找人 | 常规对抗 |

切换须**暂停期间**改文件 → 重启 → 请裁判登记（每场最多 2 次）。

### 3.4 地图尺寸自适应

规则：第二阶段 24×24，练习包 18×18。以下只在 **≥20×20** 生效：

- 第 150 Tick 后"确实见过资源、现在一个都没有" → 立刻去任务坐标；
- 任务点反复撞不开（≥4 次 E_BLOCKED）→ 先做别的 15 Tick 再回来。

小图保持原行为（强行启用会从 42.5 掉到 39.2）。

---

## 4. 成绩与实验证据（全部 v1.1 口径）

| 实验 | 结论 |
|---|---|
| 第一阶段 9 张地图 | greedy 全 40/40；蛇形 8/40（96/101 步都在往右撞墙） |
| 战斗模式（500 Tick 正式节奏） | hybrid 45.0 > opportunistic 42.0 > never 30.0 |
| 任务资源距离门限 | 24×24：45.0→52.5；18×18 无变化 |
| 事件密集（8 事件/27 公告） | 真实 LLM：危险区暴露 −21%、死亡 −22%、无效移动 −21%（分数不变，买稳定性） |
| 开局 30 Tick 三种微策略 | 全部同分（资源归属由出生几何决定） |
| 四席同算法自博弈 | 修防御死锁后 27.3 → **42.5（5 局无低分）**，最多一次第 1 |
| 24×24 真实 LLM | 最早 39.9（最低 18.8）→ 现在 **45.3~48.1（最低 41.7）** |

---

## 5. 踩过的坑（已修）与"不要再试"

### 5.1 真 bug（都已修 + 有测试）

| 现象 | 根因 | 修复 |
|---|---|---|
| 第一阶段只有 8 分 | 策略"已探索格子"永远空集；撞墙回执没回灌 | 按视野维护 explored；E_BLOCKED 记障碍 |
| 415/500 Tick 在 WAIT | 资源第 52 Tick 被分完，程序认为没事做 | hybrid：没资源转压制 |
| 血量 20 时连续 251 帧 DEFEND | 把防御当免伤 | 最多隔一帧防一次 |
| reach 任务被追击挤掉 | 战斗分支在任务分支之前 | 任务点在存活时优先 |
| 卡在任务点相邻格 91 帧 | E_BLOCKED 被当永久障碍（实为占位） | 临时堵塞 8 Tick 过期 + 撞不开先打占位者 |
| 预算 60 却打了 63 次 | 只在成功后计数 | 准入即占位，按"发出的请求"计 |
| 偶发 JSON 解析失败 | 模型吐注释/单引号/截断 | 6 类修复策略 |
| Windows 起不来 | GBK 控制台 / 无 rclpy | UTF-8 输出 + 友好提示 |

### 5.2 被数据否掉（别重复尝试）

| 做法 | 数据 |
|---|---|
| `aggressive` 主动追人 | 42.5 < hybrid 45.0 |
| `patrol_when_idle` 空闲巡逻 | 47.5 vs 50.0，且更易掉血 |
| 开局三种微策略 | 9 局同分 |
| `resource_first` 先采集再打 | 资源被堵图上更差 |
| `detour`=20 抢高价值 | 40.0 vs 47.5 |
| "没记忆就去任务点" | 3.1 / 21.4 分 |
| "晚开局就冲任务点" | 11.9 / 17.15 分 |

---

## 6. 提交材料清单（规则 v1.1）

| 材料 | 形式/数量 | 对应物 |
|---|---|---|
| 代码 | 1 份仓库 + 1 份压缩包 | GitHub + 交付包 |
| 目录与运行说明 | 写在仓库里 | `README.md` + 本文件 §3 |
| 技术报告 | **语雀文档，一队一份** | `docs/TECH_REPORT.md` |
| 运行截图/录屏 | 前置检录四要素 | `docs/SUBMISSION_CHECKLIST.md` §3 |
| 镜像 | 可选 | 清单 §4 |
| decision_type 登记 | 赛前登记 3 个枚举 | §3.3 |

时间线：队伍调整 10/30 23:59 → **材料 10/31 20:00** → 11/1 比赛 → 11/2 公示。

---

## 7. 队友合并指南

阅读顺序：`PROJECT_MANIFEST.json` → `docs/CONTESTANT_MAP.md` →
`docs/HANDOVER_FOR_TEAMMATE.md` → `python run_tests.py`。

**不能破**：话题名/QoS、动作字段与 tick/session 回填、同 Tick 去重、换场次清空、
阵亡只 WAIT、出生保护不攻击、AI 只走统一网关、事件必须 LLM 理解。
**可以换**：`strategy.py`、`agent.py` 的决策与调参。

---

## 8. 从零复现

```bash
unzip rmtc-competition-2026-10-23.zip && cd rmtc-competition-2026-10-23
python3 run_tests.py
python -m rmtc_gateway --port 8000 &
python examples/llm_demo.py
python -m contestant.run_experiments --maps 100
# 官方练习裁判（虚拟机）
cd ~/rmtc_practice && ./start_referee.sh --stage 2 --bots B,C,D
```

---

## 9. 环境与工具链

| 项 | 值 |
|---|---|
| 虚拟机 | Ubuntu 22.04 + ROS 2 Humble，用户 `rogers`，IP 随网络变（`vmrun getGuestIPAddress`） |
| 操作通道 | `vmrun`（沙箱需允许写 `C:\ProgramData\VMware`）；`runProgramInGuest` 不回显 stdout，脚本重定向到 `/tmp` 再拷回 |
| 裁判接口 | `POST /api/start`（支持 tick_ms/max_ticks/bots/config）→ `POST /api/resume`；`GET /api/state` |
| 每场日志 | `~/.rmtc-practice/<session_id>/`：communication.jsonl、ai.jsonl、score.json |
| 花费追踪 | 网关审计 jsonl + `python scripts/cost.py <文件>` |

---

## 10. 文档索引

| 文档 | 用途 |
|---|---|
| `docs/COMPETITION_PLAN.md` | 封存参数、decision_type、预期成绩、现场流程 |
| `docs/SUBMISSION_CHECKLIST.md` | 提交清单（形式/数量/截图/镜像） |
| `docs/TECH_REPORT.md` | 语雀技术报告模板 |
| `docs/COMPLIANCE.md` | 规则/协议逐条对照、真实 LLM 验证、额度经验 |
| `docs/CONTESTANT_MAP.md` | 感知/动作/任务/事件 → 代码 → 测试 |
| `docs/EXPERIMENTS.md` | 7 组实验的完整数据 |
| `docs/MATCH_LOG.md` | 真机对局记录、裁判 API、跑批陷阱 |
| `docs/PORTABILITY.md` | Windows/Ubuntu 双系统注意 |
| `docs/HANDOVER_FOR_TEAMMATE.md` | 合并规范 |
| `PROJECT_MANIFEST.json` | 机器可读清单 |

---

## 11. 未完成 / 下一步

1. 真实赛场的 24×24 与对手强度无法在本地验证（练习陪练偏弱）；
2. 24×24 上再提分只能靠资源份额（rv 90→120 多 7.5 分）：可试"按剩余时间估算收益的资源优先级"（唯一未被数据否掉的方向）；
3. 事件理解只用了 threat_zone / avoid_cells / danger_level / buff_zone / direction_reversed；
   天使恩泽（回血）、洁身自好（反伤）、潘潘的神（易伤）尚未利用；
4. 语雀技术报告需按 `docs/TECH_REPORT.md` 补队内分工与截图。
