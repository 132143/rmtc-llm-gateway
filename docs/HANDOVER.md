# 比赛项目 · 进度交接（2026-10-23 收工）

> 用途：下次接着干时的**唯一入口文档**。仓库里的 `README.md` 讲"东西怎么用"，这份讲"干到哪了、坑在哪、下一步做什么"。

---

## 1. 一分钟速览

| 项 | 状态 |
|---|---|
| 仓库 | https://github.com/132143/rmtc-llm-gateway（main，已推到 `4791112`） |
| 本地路径 | `D:\dsh\workspaces\default-workspace\rmtc-llm-gateway` |
| 测试 | **72 个全绿**（网关 26 / 客户端 19 / 参赛代码 27） |
| 第一阶段真机 | **40.0 / 40**，且换成 9 张不同地图依然 40/40（骨架蛇形只有 8/40） |
| 第二阶段真机 | 60.0 / 60（**对手未动**的那局）；对手真抢时 43~45 分（见 §3） |
| 虚拟机 | 开着；IP 会随网络变（NAT 时 `10.236.131.61`，插网线后 `10.240.130.231`），先查再连 |

一句话：**能跑、能拿满分（练习场），但真实事件理解（真 LLM）和"强对手对抗"一次都没验证过。**

---

## 2. 仓库里有什么（三块，互不依赖）

| 块 | 目录 | 是什么 | 测试 |
|---|---|---|---|
| ① 网关服务端 | `rmtc_gateway/` | 协议 §5 的 OpenAI 兼容网关（鉴权/额度/限流/错误码/转发/审计） | `tests/test_gateway.py` 26 |
| ② 网关客户端 | `rmtc_client/` | 机器人调 AI 的那半：异步、1.5s 超时、退避、降级、公告理解 | `tests/test_client.py` 19 |
| ③ 参赛程序 | `contestant/` | `agent.py`（感知→动作）、`strategy.py`（策略）、`bot_node.py`（ROS 节点）、`sim.py` + `run_experiments.py`（第一阶段模拟器） | `tests/test_contestant.py` 18 |
| 文档 | `docs/PROTOCOL_MAP.md`（协议条款→代码→测试）、`docs/STRATEGY_NOTES.md`（策略实验）、`docs/MATCH_LOG.md`（真机实测）、`docs/evidence/`（对局原始输出） | — |
| 脚本 | `scripts/vm/runmatch.sh`（一键对局）、`runmatch2.sh`（第二阶段带 AI） | — |

跑测试：`cd rmtc-llm-gateway && python3 run_tests.py`

---

## 3. 真机实测结果（官方练习裁判）

| 场次 | 得分 | 资源 | 有效伤害 | 任务 | 非法动作 |
|---|---|---|---|---|---|
| 第一阶段（第一次） | 8.0 / 40 | 40 / 200 | — | — | 0 |
| 第一阶段（修完） | **40.0 / 40** | 200 / 200 | — | — | 0 |
| 第二阶段（4 车 + AI，对手未动） | **60.0 / 60** | 200 / 200 | 300 / **500** | 3 / 3 | 0 |

### 第一局为什么只有 8 分（真机才暴露）

最后卡在 `(5,4)`，`last_action: {"action":"move","error":"E_BLOCKED","moved":false}`。两处 bug：

1. `agent.py` 给策略的"已探索格子"永远是空集（模拟器里是真实维护的）→ 探索退化成原地附近乱走，
   只能靠运气撞到视野里的 4 个资源；
2. 撞墙回执 `E_BLOCKED`/`E_OOB` 没有回灌成障碍 → 反复撞同一堵墙。

修法（已提交）：每帧把视野窗口记进 `explored`；记住上一步的 `(出发点,方向)`，
下一帧回执 `moved=false` 且 `error∈{E_BLOCKED,E_OOB}` 就把那格记成障碍；视野里的障碍永久记住。

**教训**：模拟器测出来的分数只能证明"逻辑自洽"，不能证明"真机能跑"。
每改一版策略，都要上官方裁判跑一局。

---

## 4. 虚拟机环境（2026-10-23 实测，和旧交接文档不一致的地方已标出）

| 项 | 值 |
|---|---|
| 主机名 | `rogers-virtual-machine` |
| **IP** | **`10.236.131.61`** ← ⚠️ 旧文档写 192.168.40.128，已失效 |
| 用户 | `rogers`（**密码不写进仓库**，见桌面《交接文档.md》1.3 节） |
| Python / ROS | 3.10.12 / Humble |
| 练习包 | `~/rmtc_practice`（已解压，`start_referee.sh` 在根目录） |
| 仓库副本 | `~/rmtc-llm-gateway`（**虚拟机里上不了 GitHub**，是用 `git archive` 打 tar 包拷进去的） |

### 操作通道（踩过的坑）

1. **vmrun 在 DSH 沙箱里会报 "vmrun was unable to start"** —— 它要写 `C:\ProgramData\VMware`。
   把沙箱放到 `danger-full-access` 就正常（本次已验证；不是虚拟机没开）。
2. `vmrun runProgramInGuest` **不回显 stdout**：脚本里所有输出重定向到 `/tmp/xxx.txt`，
   再用 `copyFileFromGuestToHost` 取回。
3. 取 IP：`vmrun -T ws getGuestIPAddress "D:\Ubuntu 64 位.vmx" -wait`
4. 虚拟机**上不了 github.com**（curl 返回 000），所以推送只在宿主机做，进虚拟机靠 tar 包。

---

## 5. 下次怎么继续（照抄）

### 5.1 宿主机 → 虚拟机（一条龙）

```powershell
$vmrun = "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"
$vmx   = "D:\Ubuntu 64 位.vmx"
cd "D:\dsh\workspaces\default-workspace\rmtc-llm-gateway"

git archive --format=tar -o "$env:TEMP\repo.tar" HEAD
& $vmrun -T ws -gu rogers -gp *** copyFileFromHostToGuest $vmx "$env:TEMP\repo.tar" /tmp/repo.tar
& $vmrun -T ws -gu rogers -gp *** copyFileFromHostToGuest $vmx "scripts\vm\runmatch2.sh" /home/rogers/runmatch2.sh
& $vmrun -T ws -gu rogers -gp *** runProgramInGuest $vmx /bin/bash /home/rogers/runmatch2.sh 2 B,C,D
& $vmrun -T ws -gu rogers -gp *** copyFileFromGuestToHost $vmx /tmp/match_2.txt "$env:TEMP\match2.txt"
Get-Content "$env:TEMP\match2.txt" -Encoding UTF8
```

（第一阶段把最后一行改成 `runmatch.sh 1`。第二阶段跑满 500 Tick 约 4~5 分钟。）

### 5.2 官方练习裁判的两个必知细节

- **开局要两步**：`POST /api/start`（部署模板，仍暂停）→ `POST /api/resume`（才真正跑）。
  只 start 会看到 tick 一直是 0。
- 其他接口：`/api/pause`、`/api/stop`、`/api/reset`、`/api/config/1|2`、`/api/state`。
- 每场日志在 `~/.rmtc-practice/<session_id>/`：`communication.jsonl`、`ai.jsonl`、`score.json`。

---

## 6. 待办（按收益排序）

1. **接真实 LLM**（最关键，现在事件理解是 mock，等于没测）：
   `start_referee.sh --stage 2 --ai-mode real` + `PRACTICE_LLM_URL/KEY/MODEL`；
   或者把我们自己的 `rmtc_gateway` 起在 8000 端口、`--upstream` 指向真实服务，
   让 `RMTC_AI_URL=http://127.0.0.1:8000/v1/chat/completions` —— 顺便验证 502/504/429 分支。
2. **第二阶段多跑几局**：现在只有 1 局，而且对手是裁判自带参考车（B/C/D 得分 0，等于没对抗）。
   "打人 vs 捡资源"的权重需要参数扫描（`contestant/agent.py` 里 `stage>=2` 那段）。
3. **第一阶段换图验证**：用 `/api/config/1` 导入自定义地图（障碍更多/资源更散），看 `explored` 探索是否还稳。
4. 代码与封存版本一致（Q&A 要求）：正式比赛前锁定一个 commit，提交的就是现场启动的那个。
5. 语雀技术报告：整体设计、AI 协同、测试截图、运行指令；仓库链接 `https://github.com/132143/rmtc-llm-gateway`。

---

## 7. 合规红线（别踩）

- 比赛只允许走主办方统一网关调 AI；`rmtc_gateway` 是我们自己的实现，可以练、可以本地验证，
  **正式比赛不能拿它替代官方网关**，模拟上游也刻意不做语义理解。
- 随机事件必须真的交给大模型，不许正则/关键词/固定模板顶替（P5）。
- 不要写死 `practice-*` 令牌、场次 ID、地图尺寸、任务坐标；换场次必须清缓存。
- 仓库里没有任何令牌/密钥（已扫过）；`docs/MATCH_LOG.md` 里有虚拟机密码，**别把这个文件外传**
  （如果要公开仓库，先删掉那一段）。

---

## 8. 关键文件清单

| 文件 | 为什么重要 |
|---|---|
| `contestant/agent.py` | 感知→动作的全部"丢分点防护"，改策略基本都在这里 |
| `contestant/strategy.py` | BFS 贪心 + 蛇形，`step_toward()` 是绕障核心 |
| `contestant/bot_node.py` | 只有这一层依赖 ROS；上场入口 |
| `rmtc_client/llm_client.py` | AI 调用与降级；比赛丢分高发区 |
| `docs/MATCH_LOG.md` | 真机结果、裁判 API、虚拟机坑位 |
| `docs/STRATEGY_NOTES.md` | 策略实验数据（含"价值/距离"那次失败实验） |
| `scripts/vm/runmatch2.sh` | 一键跑第二阶段，下次直接改参数复跑 |


---

# 2026-10-23 深夜这一轮（续）

## 新增的可测工具

| 文件 | 作用 |
|---|---|
| `scripts/gen_maps.py` | 生成 7 张常规测验图 + 2 张硬图（`docs/sweep_maps*`），都是"16 资源 / 总价值 200" |
| `scripts/gen_stage2.py` | 生成 3 张第二阶段配置（official / open / big24） |
| `scripts/vm/runsweep.sh` | 在官方裁判上扫一段策略（`--strategy greedy|snake`） |
| `scripts/vm/stage2_ab.sh` / `stage2_ab2.sh` | 第二阶段 A/B；**ab2 每格重起裁判+选手**（ab 会互相污染，已弃用） |
| `scripts/analyze_ab.py` | 把 CSV 汇总成"每模式 × 每图"的平均/最低/最高 |
| `docs/CONTESTANT_MAP.md` | 感知/动作/任务/事件 → 代码 → 测试 的参赛侧对照表（写技术报告可直接用） |
| `docs/STAGE2_AB.md` | 第二阶段战斗模式的完整实验记录 |

## 本轮实测结论

1. **第一阶段已封顶**：9 张图（含 24×24、40 障碍、迷宫、贴角）greedy 全部 40/40 且非法动作 0；
   骨架蛇形全部 8.0（101 步里 96 步 `move:right`，撞墙后原地打转）。
2. **第二阶段"何时开打"没有想象中重要**：200 Tick 快节奏下
   `never` 31.7 / `opportunistic` 27.5 / `finish` 23.8 / `resource_first` 20.8。
   但快节奏把"采集"和"对抗"压在同一段时间里，**不能用来推翻 500 Tick 的结论**；
   正式节奏下对手没动时我们拿过 60/60；对手真抢资源时是 43~45 分（对抗分封顶按 500 算）。
3. **A/B 方法论**：同一批里先跑的模式会污染后面的模式；重起进程后三次重复逐位相同。
   以后做对比一律用 `stage2_ab2.sh`（每格重起）。
4. **实机验证过的功能**：`--dry-run`（50 条动作打印）、失联看门狗（6.0 秒告警）、
   `RMTC_TRACE`（201 行/局）、`cat 9×9 解析`、`E_BLOCKED` 回灌。

## 新的踩坑

- **Windows `core.autocrlf=true` → `git archive` 出来的 .sh 是 CRLF**，虚拟机 bash 直接
  `$'\r': command not found`。已加 `.gitattributes`（`*.sh text eol=lf`），部署脚本里也 `sed -i 's/\r$//'`。
- **`vmrun runProgramInGuest` 不回显 stdout**，脚本必须写 `/tmp/xxx.txt` 再拷回。
- **虚拟机跑 GitHub 会偶发 TLS 失败**（`GnuTLS recv error`），部署脚本做成"git 失败就用 tar 覆盖"。
- **在跑批次的 pwsh 超时 ≠ 虚拟机里的脚本停止**：guest 里的进程会继续跑，导致两批并发写乱 CSV。
  已加 `flock` 锁，并且超时后先 `pkill` 再重跑。

## 下一步（按收益）

1. **接真实 LLM**（唯一还没验证的关键项）：`start_referee.sh --stage 2 --ai-mode real` +
   `PRACTICE_LLM_URL/KEY/MODEL`；或把 `rmtc_gateway` 起在 8000 端口、`--upstream` 指向真实服务。
2. **500 Tick 正式节奏下把三种战斗模式各跑 3 局**（每局 4 分钟，约 40 分钟），再决定默认模式。
3. 用 `/api/config/1` 导入更大/更刁的地图，验证第一阶段在"非 16 资源"配置下的表现。
4. 语雀技术报告：直接引用 `docs/CONTESTANT_MAP.md` 与 `docs/STAGE2_AB.md` 的表格。


---

# 2026-10-23 深夜 第二轮（对手真动起来之后）

## 关键发现：之前那次 60/60 是对手没动

第一次 500 Tick 拿 60/60 时，记分板上 B/C/D 全是 `resource_value 0`——陪练挂在那儿没干活。
改用 `/api/start` 的 `bots:["B","C","D"]` 让它们真的抢资源后，同一张图同一程序只有 **43.0**：
资源在 ~52 Tick 就被四台车分完，我们只拿到 60 价值，之后**原地 wait 了 415 个 Tick**（占全场 83%）。

## 因此做的改进：`hybrid` 战斗模式（已设为默认）

"没资源可捡、地图也探完"时不再空转，改为主动靠近/攻击对手。正式节奏（500 Tick × 500 ms）实测：

| 模式 | 得分 | 资源 | 有效伤害 | 任务 |
|---|---|---|---|---|
| never（只采集） | 30.0 | 60/200 | 0 | 3/3 |
| opportunistic（顺路打） | 42.0 | 60/200 | 240 | 3/3 |
| **hybrid（默认）** | **45.0** | 60/200 | 730（封顶 500） | 3/3 |

45 分 = 资源 15 + 对抗 15 + 任务 15，已经是"资源被抢光"这个前提下的上限。

## 跑批脚本的三个"假结果"陷阱（都踩过，已修）

1. 旧裁判还占着 8080 → 2.5 分钟"跑完"两局 500 Tick，分数完全一样；
2. 轮询等待写死 60 秒 → 正式节奏（250 秒/局）被截断在 tick≈120，得 25 分；
3. 两个批次并发 → CSV 行错乱。宿主 pwsh 超时**不会**杀掉虚拟机里的脚本，必须先 `pkill`。

现在脚本会：等端口真空、开局校验 `finished=false && tick<5`、等待上限按局时长算、
结束时比对 tick 是否跑满、`flock` 防并发。

## 两个测试的竞态（顺带修掉）

`test_audit_records_seat_but_never_token` 与 `test_upstream_timeout_is_504_and_free`
假设"响应回来后服务端的收尾工作已完成"——实际上审计落盘与记账清理在 `finally` 里，
比响应晚一点。已改成轮询等待（≤2 秒）。现在 72 个测试连跑 3 次全绿。

## 下一步（按收益）

1. **每个模式跑 3 局正式节奏**（`stage2_ab2.sh 3 500 500 "never opportunistic hybrid"`，约 40 分钟），
   把 `hybrid` 的优势从"1 局"变成统计结论。
2. **接真实 LLM**（唯一完全没验证的关键项）：`--ai-mode real` + `PRACTICE_LLM_URL/KEY/MODEL`。
3. 资源被抢光后的行为还可以更聪明：现在只会"追最近的敌人"，没有考虑对方血量/保护期。
4. 语雀技术报告可直接引用 `docs/CONTESTANT_MAP.md`、`docs/STAGE2_AB.md`、`docs/MATCH_LOG.md`。
