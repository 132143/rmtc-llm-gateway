# 规则对照与上场手册（赛事规则 v1.1 + 通信协议 v1.1）

> 这份文档回答三个问题：**代码能不能直接上场？LLM 怎么接？现在做什么、比赛时做什么？**
> 每条结论都标了依据（规则条款号）。规则原文以官方发布为准。

---

## 0. 先给结论

**能接入的部分**：ROS 话题名/QoS、感知解析（绝对坐标 + 9×9 相对位置）、动作格式与
tick/session 回填、动作窗语义、复活/保护、AI 调用的异步与降级、错误码处理——这些按协议实现了，
并且在官方练习裁判上跑过（第一阶段 9 张图全 40/40；第二阶段 500 Tick 正式节奏 45.0）。

**之前不能直接上场、现已改掉的四处**（都直接对应条款）：

| # | 问题 | 依据 | 改法 |
|---|---|---|---|
| 1 | 只在 `event_text` 非空时才调 AI；若本场没有事件，就**一次成功调用都没有 → 任务分 M=0（直接少 15 分）** | 规则 §10.2"每场最终结算前必须有一次属于本场、本席位的真实统一 AI 成功调用，否则 M=0" | `AgentConfig.ai_heartbeat_tick`：开局第 3 Tick 起，若本场还没有成功调用就主动发一次心跳请求 |
| 2 | 现场策略切换靠环境变量/命令行参数，**不是规则认可的机制** | 规则 §2.3"现场只允许修改 decision_type，取值只能来自已登记枚举；统一命名 decision_config.json" | 新增 `contestant/decision.py` + `decision_config.json`，三种已登记类型：`collect` / `balanced` / `combat`；CLI 参数只留作赛前练习 |
| 3 | 攻击"最近敌车"时没看它是否处于出生保护——**打上去 0 伤害且挡住后面目标，等于白扔一个 Tick** | 规则 §6.2 | 目标 `protected` 时不攻击 |
| 4 | 调了 LLM 却**没用它的结论**（只记了日志） | 规则 §11.1"AI 可用于任务解析、目标选择、战术建议…允许 AI 做高层决策、本地执行" | `_threat_escape`：把模型返回的 `threat_zone` 校验后用于"我在威胁区里就先撤出去" |

**还差、只能靠你现场/赛前做的**（代码替代不了）：

- 正式网关地址、本场席位令牌、ROS 域号 —— 都是**每场现场公布**的，必须现场填（见 §4）；
- 第二阶段"真实 AI 资格"必须用**主办方网关**拿到，练习 mock 不算（规则 §10.2 / §2.4）；
- 材料递交（代码/镜像/技术报告/运行截图）与前置检录（规则 §2.2）；
- `decision_type` 的三种类型必须在**封存前**实现完并向裁判**登记**（表在 §3.3）。

---

## 1. 逐条对照表

### 1.1 接口与消息（通信协议 §1–§3、规则 §4/§12）

| 条款要求 | 代码位置 | 状态 |
|---|---|---|
| 订阅 `/team_X/perception`、发布 `/team_X/action`，`std_msgs/msg/String` | `bot_node.py` | ✅ 话题名按席位拼，不写死 |
| 感知/动作 QoS：RELIABLE + VOLATILE + KEEP_LAST(1) | `bot_node.qos_profile()` | ✅ |
| 注册话题 `/team_X/registration` 的 QoS：RELIABLE + **TRANSIENT_LOCAL** + 1 | `bot_node.publish_registration()` | ✅ 每 5 秒重发（保持发布者存活） |
| 只能访问本席位公开话题；不得读别队感知/裁判内部接口 | `bot_node` 只建这 3 个话题 | ✅（`scripts/vm/*` 是**测试台**，读 `/api/state` 看全图；程序本身不读） |
| `self.position` 是绝对坐标；`visible_entities[].position` 是 9×9 字符串 | `agent.decode_relative` / `_resources` / `_enemies` | ✅ 两种解析分开，有测试 |
| 字段类型/缺失不得让进程退出 | `bot_node.on_perception` 双层 try + `Agent` 防御式取值 | ✅ |
| 同 Tick 重发不重复推进；早于当前 Tick 的帧丢弃 | `if tick <= self.last_tick: return None` | ✅ |
| 换 `session_id` 清空上一场缓存（定位、记忆、在途 AI） | `Agent._start_session` | ✅ |
| 视野半径来自感知 `vision_radius`（0–4） | `AgentConfig.vision_radius`（默认 4）/ `_mark_explored` | ⚠️ 用配置值，未逐帧读感知字段（练习端恒为 4；正式场若有变化需读字段——已记在 §5 待办） |

### 1.2 动作与结算（协议 §4、规则 §5/§6/§7）

| 条款要求 | 代码位置 | 状态 |
|---|---|---|
| 动作字段小写 move/attack/defend/wait；方向 up/down/left/right | `ATTACK_ACTIONS` / `DIRECTIONS` 白名单 | ✅ 非法一律降级 `wait` |
| `tick` 整数、`session_id` 字符串，原样回填 | `payload = {"tick":…, "session_id":…}` | ✅ |
| 单条动作 ≤ 4096 字节、每 Tick 最多 128 条 | 我们 ~80 字节/条、每 Tick 1 条 | ✅ |
| 第一阶段只允许 MOVE/WAIT | `stage >= 2` 才进战斗分支 | ✅ 有测试 |
| 阵亡期间只能 WAIT | `alive is False → wait` | ✅ |
| 出生保护期间不能 ATTACK（可 MOVE） | `not me.get("protected")` | ✅ |
| 攻击只命中"指定方向直线 1–2 格内最近的存活敌车" | `_line_direction` + 取最近 | ✅ |
| 最近目标处于出生保护时挡住后面目标、自身 0 伤害 | 目标 protected 不攻击 | ✅ 新增 |
| E_BLOCKED / E_OOB 不计 invalid_actions（车原地不动） | 不重试同一步，把那格记成"堵住" | ✅ |
| 阵亡每次扣 60 有效伤害，最低 0；资源与已完成任务保留 | 只读感知里的 `score`，不自行加减 | ✅ |
| 普通复活 3 Tick + 2 Tick 保护；世界分裂立即复活 | 只消费感知字段 | ✅ |

### 1.3 计分与同分（规则 §10）

| 条款 | 代码/文档 | 状态 |
|---|---|---|
| 第一阶段：`40 × R1/200` | 模拟器 `sim.run_stage1` | ✅ |
| 第二阶段：`30 × min(R2/120,1) + 15 × min(D/**500**,1) + 5 × M` | **对抗分封顶是 500**（v1.1 从 300 改成 500） | ✅ 文档已按 500 更正；代码里没有"300"门槛逻辑 |
| AI 资格：本场至少一次真实成功调用，否则 M=0（资源/对抗分保留） | `AgentConfig.ai_heartbeat_tick` + `client.ok_calls` | ✅ 新增心跳 |
| 同分比较用调整后有效伤害（含击杀奖励与死亡扣除） | 只读感知 `score.effective_damage` | ✅ |

### 1.4 AI 服务（协议 §5、规则 §11）

| 条款 | 代码/流程 | 状态 |
|---|---|---|
| 唯一外部 AI 通道 = 统一网关；不得用本地小模型或其他服务 | `rmtc_client` 只向 `RMTC_AI_URL` 发请求 | ✅ **前提：现场把 `RMTC_AI_URL` 指向主办方地址；绝不能指向本机的 `rmtc_gateway`** |
| `POST /v1/chat/completions`，`Authorization: Bearer <本场令牌>` | `LlmGatewayClient.ask` | ✅ |
| `messages` 非空、`stream` 省略或 false | 客户端从不设 stream；服务端另有 400 校验 | ✅ |
| 请求体 ≤ 2 MiB，用 Content-Length | urllib 默认带 Content-Length | ✅ |
| 额度：暂定每席位 100 次成功调用；429 `quota_exceeded` 是唯一信号 | `ClientConfig.max_calls=60`（留余量）+ 收到 429 永久降级 | ✅ |
| 必须异步，不能阻塞动作窗；超时要兜底 | `ask_async` + `EventUnderstanding`，1.5 s 超时 | ✅ |
| 输出必须自己解析校验，失败回退本地策略 | `extract_first_json_object` + 失败即无提示 | ✅ |
| 随机事件必须由 LLM 理解，禁止正则/关键词/模板/预设时间表 | 事件文本只进 prompt，代码里没有关键词表 | ✅ |
| AI 只做高层决策，动作仍由本地程序生成 | 模型只返回 `threat_zone/advice`，动作在 `agent` 里产生 | ✅ |

### 1.5 现场行为限制（规则 §2.3 / §13）

| 条款 | 我们的做法 | 状态 |
|---|---|---|
| 现场禁止改源代码/算法/Prompt/其他参数 | 所有可调项集中在 `decision_config.json` 的 `decision_type` | ✅ |
| `decision_config.json` 只能改 `decision_type`，不得夹带坐标/动作/参数 | `load_decision_config` 只读该字段，其他字段**显式忽略并打日志** | ✅ 有测试 |
| 策略类型必须赛前封存并登记 | 登记表见 §3.3（三种） | ⚠️ 需要你在赛前提交给裁判 |
| 每场最多申请 2 次切换，须暂停期间提出 | 流程要求，非代码 | ⚠️ 记在 §5 现场清单 |
| 每阶段最多 2 次重启，必须用封存版本 | —— | ⚠️ 记在 §5 现场清单 |
| P1 人工实时发动作 / P5 绕过网关 / P2 读全图 | 程序不读 `/api/state`、不连别的 AI、不提供人工动作入口 | ✅（测试台脚本不上场） |

---

## 2. LLM 到底怎么接（这一段是核心）

### 2.1 数据流（比赛现场长这样）

```text
裁判机 ──ROS 话题──▶ 你的程序 ├─ /team_A/perception  JSON（含 event_text）
                            ├─ /team_A/action      JSON（tick + session_id + 动作）
                            └─ HTTP POST ──▶ 主办方统一网关 <GATEWAY>/v1/chat/completions
                                             Authorization: Bearer <本场席位令牌>
```

- 网关**只做转发与限额**，不做决策；提示词、解析、校验、降级都是你的程序（协议 §5 开头）。
- 我们仓库里的 `rmtc_gateway` 是**给你本地练链路**用的，正式比赛**不能用它**（那是 P5）。

### 2.2 三条硬要求（少一条就丢分/违规）

1. **每场第二阶段至少要成功调用一次**（本场、本席位），否则任务分 M=0 —— 已经用"心跳调用"兜住：
   第 3 Tick 起若还没有成功记录就主动问一次（`AgentConfig.ai_heartbeat_tick`）。
2. **只能走统一网关**：`RMTC_AI_URL` 必须填现场公布的地址；令牌用本场发的那一个。
3. **必须异步 + 有降级**：任何失败都只是"没有 AI 提示"，继续本地策略（`rmtc_client` 已保证不抛异常）。

### 2.3 现场要怎么看"AI 到底通没通"

不是看你日志里有一条请求，而是：

- 你的日志出现 `ok(...)` 或 `client.ok_calls >= 1`；
- 裁判侧（练习端网页 / 正式裁判记录）显示本席位**成功调用次数 ≥ 1**；
- 注意：**mock 模式的成功不算正式资格**（规则 §2.4）。

### 2.4 额度怎么省

规则暂定 100 次/席位/场。我们的默认预算 60（`ClientConfig.max_calls`），
两次调用最小间隔 2 秒；第二阶段 250 秒最多约 125 次——所以预算必须设，
否则中途 `quota_exceeded` 后就没有 AI 可用了（虽然还能降级打，但事件理解没了）。

---

## 3. 现在该做什么（10/31 20:00 材料截止前）

### 3.1 必做清单

- [ ] **封存前把三种 decision_type 都跑一遍**，确认 `decision_config.json` 改了真的会变行为；
- [ ] 用**主办方/练习包的真实服务**跑一次第二阶段（`--ai-mode real` 或自备 OpenAI 兼容服务），
      确认"真实调用成功 ≥ 1"这条能达成；
- [ ] 录一段**运行截图/录屏**用于前置检录：能看见 ①收到感知 ②动作被接受（`executed_tick` 有值）
      ③下一帧 `self.last_action` 是你的动作 ④第二阶段真实 AI 调用成功；
- [ ] 写技术报告（语雀）：整体设计、分工、日程、AI 协同、测试截图、运行命令；
      可直接引用 `docs/CONTESTANT_MAP.md`、`docs/STAGE2_AB.md`、`docs/MATCH_LOG.md` 的表格；
- [ ] 代码仓库保持"现场启动的就是这个 commit"，README 里写清楚运行命令与目录结构；
- [ ] 把 §3.3 的登记表交给裁判（类型标识、作用、读取方式）。

### 3.2 赛前一天自检（10 分钟）

```bash
cd ~/rmtc-llm-gateway
python3 run_tests.py                     # 应 80 个测试全绿
cat decision_config.json                 # 确认 decision_type 合法
python3 -m contestant.bot_node --seat A --stage 1 --dry-run   # 只看解析，不发布
```

### 3.3 决策类型登记表（交给裁判的文本）

| 类型标识 | 行为 | 读取方式 |
|---|---|---|
| `collect` | 只采集，不主动进攻（第二阶段也只用 MOVE/WAIT 级别的行为） | 程序启动时读同目录 `decision_config.json` 的 `decision_type` |
| `balanced` | 采集为主，进入直线射程才顺路攻击（默认） | 同上 |
| `combat` | 采集为主；没有资源可捡时主动靠近/攻击对手拿对抗分 | 同上 |

现场切换只能在**暂停期间**把 `decision_type` 改成上面三个之一，程序**重启后生效**
（每场最多申请 2 次，须裁判批准并登记）。

---

## 4. 比赛时该做什么（每场照着做）

### 4.1 上场前 5 分钟

1. 从裁判/公告拿到：**席位**、**ROS_DOMAIN_ID**、**网关地址**、**本场令牌**（可能还有模型名与输出限制）；
2. 填环境变量（**只改环境变量，不改代码**）：

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=<现场值>
export ROS_LOCALHOST_ONLY=0
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMTC_SEAT=<A/B/C/D>
export RMTC_TEAM_TOKEN=<本场令牌>
export RMTC_AI_URL=<现场的 /v1/chat/completions 完整地址>
export RMTC_TEAM_NAME='<队名>'
```

3. 确认 `decision_config.json` 里的 `decision_type` 是你要的类型；
4. 启动：`python3 -m contestant.bot_node --seat <席位> --stage 2`
   （第一阶段用 `--stage 1`，此时只发 MOVE/WAIT）；
5. 若想先只看解析：加 `--dry-run`，确认 `[dry-run]` 里的 tick/session 与动作都正常，再去掉重跑。

### 4.2 开局后 30 秒内核对四件事（少一件都别放心）

| 检查 | 怎么看 |
|---|---|
| ① 收到感知 | 日志没有"5 秒没收到任何感知"告警；加 `RMTC_TRACE=trace.jsonl` 能看到 tick 连续增长 |
| ② 动作被接受 | 裁判侧 `executed_tick` 有值（练习端网页"执行 Tick"；正式场看裁判诊断） |
| ③ 动作有回执 | 下一帧 `self.last_action` 是你的动作；撞墙会显示 `E_BLOCKED`（正常，不扣分） |
| ④ AI 真实可用（第二阶段） | 裁判记录本席位成功调用 ≥ 1；我们默认会在第 3 Tick 主动发一次心跳 |

### 4.3 出问题怎么办（按规则）

| 现象 | 处理 |
|---|---|
| 程序崩了 | 可登记重启（每阶段最多 2 次，用封存版本、不补时） |
| 想换策略 | 暂停期间改 `decision_type` → 重启 → 请裁判登记（每场最多 2 次） |
| AI 401/429 | 401 说明令牌不对（找裁判）；429 `quota_exceeded` 说明额度用完 → 程序已自动降级，继续本地策略打分 |
| 断线/晚加入 | 不作为申诉理由（规则 §14）；用最新感知的 tick/session 恢复即可 |
| 觉得计分/判罚有问题 | 本场结束后 **5 分钟内**申诉，写清场次、Tick、事实、条款、想复核的数据 |

---

## 5. 已知待办（不影响上场，但值得在封存前补）

1. `vision_radius` 目前用配置值（4），没有逐帧读感知字段；正式场若公布的不是 4 需要改成读字段。
2. 第二阶段"资源被抢光后"的行为只做了"追最近的敌人"，没考虑对方血量/保护期/我方 HP。
3. 事件理解只用了 `threat_zone` 一个字段；其他事件（幻术反向、易伤倍率、堡垒 5×5 攻击）没有建模。
4. 对抗/采集的权重只在 official 图跑过 1 局/模式，样本偏少。


---

## 6. 本轮修复的真机验证（官方练习裁判，2026-10-23 深夜）

| 验证项 | 操作 | 结果 |
|---|---|---|
| `decision_config.json` 生效 | `{"decision_type":"collect"}` → 启动 stage 1 | 日志：`决策类型：collect（decision_type=collect）→ strategy=greedy, combat=never；队伍名 队伍A`；第一阶段仍 **40.0/40，资源 200/200，非法动作 0** |
| 注册话题发布者 | `ros2 topic info /team_A/registration` | `Publisher count: 1`（RELIABLE + TRANSIENT_LOCAL，每 5 秒重发） |
| **AI 资格心跳**（最关键） | `events.enabled=false` 的第二阶段配置 → `event_text` 长度为 0 | 裁判侧 `ai.A.success_count = 1`、`attempt_count = 1` —— **没有事件也能拿到本场 AI 资格**，任务分不会再被清零 |
| 无事件场次的成绩 | 同上（无陪练） | A 队 **55.0 / 60**（资源 30 + 对抗 + 任务 15） |
| 决策轨迹 | `RMTC_TRACE` | 100 Tick 一局写出 101 行 jsonl |

复现脚本：`scripts/vm/`（`verify_*` 两个脚本在宿主机工作区，逻辑与这里描述一致）。
