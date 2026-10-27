# 参赛侧协议对照表：感知/动作/任务/事件 → 代码 → 测试

依据《RMTC 机甲大师图灵对决赛赛事规则 v1.2》与《通信协议 v1.1》§3 感知、§3.1 任务类型、§4 动作、§5 LLM 网关、§7 联调。
网关服务端那一侧的对照表在 [PROTOCOL_MAP.md](PROTOCOL_MAP.md)。

## 1. 感知（§3）

| 协议原文 | 代码 | 测试 |
|---|---|---|
| `tick` 是整数、`session_id` 是字符串，缺了不能用 | `agent.on_perception` 的类型校验（bool 也当非法） | `test_malformed_perception_does_not_crash` |
| `self.position` 是**绝对坐标数组** | `agent.on_perception` 直接取 `[x, y]` | `test_moves_towards_visible_resource` |
| `visible_entities[].position` 是**9×9 相对位置字符串**（不是数组） | `agent.decode_relative` + `_resources` / `_enemies` | `test_decode_relative`、`test_stage2_attacks_enemy_in_line` |
| 中心保留 `1`，物体是 `2`，位移 = (列-5, 行-5) | `decode_relative`（0 基索引即 `column-4, row-4`） | `test_decode_relative` |
| 解析失败不能让进程退出 | `bot_node.on_perception` 两层 try/except；`Agent` 内部只做防御式取值 | `test_malformed_perception_does_not_crash` |
| 同一 Tick 重发不重复推进策略 | `if tick <= self.last_tick: return None` | `test_duplicate_tick_is_ignored` |
| 收到新 `session_id` 清空上一场缓存 | `Agent._start_session` | `test_new_session_resets_state`、`test_new_session_clears_old_hint` |
| `self.last_action` 是上一 Tick 的回执 | `_learn_from_last_action` 用它把 `E_BLOCKED` 记成障碍 | `test_blocked_is_learned_from_last_action` |
| `score.effective_damage` 直接取最新感知，不要自行加减 | 只读 `score.resource_value` 做战斗开关；不改写 | `test_resource_first_*` |
| `event_text` 是纯文本，多条以换行分隔 | 原样交给 `EventUnderstanding.offer`（不再二次切分） | `test_hint_from_fenced_json` |
| `missions[]` 与随机事件文本分别处理 | `_reach_target` 只读 `missions` 里的 reach | `Agent` 相关分支 |

## 2. 动作（§4）

| 协议原文 | 代码 | 测试 |
|---|---|---|
| `action` ∈ move/attack/defend/wait | `ATTACK_ACTIONS` 白名单，越界一律降级成 wait | `test_first_perception_produces_valid_action` |
| `direction` ∈ up/down/left/right，move 与 attack 必需 | `DIRECTIONS` 校验；非法方向 → wait | 同上 |
| `tick` 与 `session_id` 必须原样回填 | `payload = {"tick": tick, "session_id": session_id}` | `test_first_perception_produces_valid_action` |
| 阵亡期间只能 wait | `if me.get("alive") is False: return wait` | `test_dead_robot_only_waits` |
| 出生保护期内攻击无效（协议 E_STATE） | `if not me.get("protected")` 才攻击 | `test_protected_robot_does_not_attack` |
| 攻击只打**直线 1~2 格**最近的敌人 | `_line_direction` 只在同一行/列给方向 | `test_stage2_attacks_enemy_in_line`、`test_ignores_enemy_not_in_line` |
| 单条动作 ≤ 4096 字节 | 我们的动作 JSON ≈ 80 字节，远低于上限 | （不需要测试） |
| 每 Tick 只发一条（不要刷） | 每个感知 Tick 发布 1 条 | `test_duplicate_tick_is_ignored` |
| `E_BLOCKED`/`E_OOB` 不计非法动作，车原地不动 | 不重试同一步，把那格记成障碍 | `test_blocked_is_learned_from_last_action` |
| 第一阶段只能移动/等待 | `stage >= 2` 才走战斗分支 | `test_stage1_never_attacks` |

## 3. 任务（§3.1）

| 协议原文 | 代码 | 测试 |
|---|---|---|
| `reach` 的 `position` 是"至少一次到达"，不是可重复得分次数 | `reach_done` 标志，到过一次就不再回头 | `Agent._decide` 分支 |
| 已完成任务不因数值下降而撤销 | 只读感知里的 `mission_completed_ids`，不自己维护 | — |
| 资源任务靠采集自然完成 | 采集策略本身不区分任务 | 第一阶段扫描 9 图全清 |

## 4. LLM 网关（§5）与事件理解

| 协议原文 | 代码 | 测试 |
|---|---|---|
| 比赛只能走统一网关 | 客户端只向 `RMTC_AI_URL` 发请求，没有任何其它外部调用 | 代码审查（`rmtc_client/llm_client.py`） |
| 必须异步、不能让模型等待阻塞动作窗 | `ask_async` + `EventUnderstanding.offer/hint` | `test_timeout_is_a_value_not_an_exception` |
| 额度用尽必须自主降级 | `quota_exceeded` → 永久停用 AI 通道 | `test_quota_exceeded_degrades_permanently` |
| 429 rate_limit/concurrency 可退避重试 | `_BACKOFF_CODES` | `test_rate_limit_backs_off_then_recovers` |
| 模型输出必须自己校验后再决策 | `extract_first_json_object` + 字段兜底 | `TestJsonExtract` 5 个用例 |
| 事件结果要核对场次与时效 | `EventUnderstanding.hint` 校验 session + max_age_ticks | `test_stale_hint_is_dropped` |
| 禁止用正则/关键词解析事件 | 事件文本只进 prompt，代码里没有关键词表 | 代码审查 |
| 要求本场真实成功调用（第二阶段） | 客户端记录 `ok_calls`；本场用 mock 时它会得到 200 但按协议不计正式资格 | `test_success` |

## 5. 联调（§7）

| 协议原文 | 代码 | 说明 |
|---|---|---|
| 收到感知只证明单向可用 | `RMTC_TRACE` 输出每 Tick 的位置/分数 | 复盘时看 `last_action` 是否真的 exec |
| 必须核对 `self.last_action` | `_learn_from_last_action` 已经在用 | 撞墙回执就是一个例子 |
| 换场次后旧的动作/AI 结果不能直接用 | `_start_session` 清空 + `hint` 校验 session | 有测试 |
