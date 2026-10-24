# 官方练习裁判实测记录（真机，不是模拟器）

日期：2026-10-23 晚　环境：虚拟机 `rogers-virtual-machine`（Ubuntu 22.04 + ROS 2 Humble，Python 3.10.12）
裁判：官方练习包 `~/rmtc_practice`（`start_referee.sh`），选手程序：`python3 -m contestant.bot_node`（本仓库）

## 一、结论先摆

| 场次 | 得分 | 资源 | 有效伤害 | 任务 | 非法动作 |
|---|---|---|---|---|---|
| 第一阶段（第一次跑） | **8.0 / 40** | 40 / 200 | — | — | 0 |
| 第一阶段（修完一次） | **40.0 / 40** | 200 / 200 | — | — | 0 |
| 第二阶段（4 车 + AI，对手未动） | **60.0 / 60** | 200 / 200 | 300（/500 计分线） | 3 / 3 | 0 |

第二阶段的 AI：裁判侧记录 **attempt 55 / success 55**（`ai.jsonl` 55 条），模式 `mock`。
**注意**：mock 只验证 HTTP 链路，按协议**不计入正式 AI 资格**，真实事件理解必须换成官方网关。

## 二、第一局为什么只有 8 分（真机才暴露的 bug）

模拟器里同一套策略是 40/40，真机只有 8/40，日志显示最后卡在 `(5,4)`、
`last_action`: `{"action":"move","error":"E_BLOCKED","moved":false}`。

根因两条，都在 `contestant/agent.py`：

1. **给策略的"已探索格子"永远是空集**（模拟器里是真实维护的）。于是
   `GreedyBfsStrategy.explore()` 每次挑"最近的未知格"都挑到相邻格 → 变成原地附近乱走，
   只能靠运气撞到视野内的 4 个资源。
2. **撞墙回执没有回灌给策略**。协议 §4 明说 `move_failed`（E_BLOCKED / E_OOB）
   不增加非法动作计数、车原地不动；但策略不知道这格堵了，会反复撞同一堵墙。

修法：

- 每帧把视野窗口（±vision_radius）内的格子记进 `explored`；
- 记住自己上一步的 `(出发点, 方向)`，下一帧 `last_action.moved == false` 且
  `error ∈ {E_BLOCKED, E_OOB}` 时，把目标格记成障碍（协议允许记住自己的失败）；
- 视野里出现过的 `type=obstacle` 也永久记住。

修完同一条命令、同一张地图：40.0 / 40，200/200，非法动作 0，101 条感知 / 101 条动作。

## 三、怎么跑（下次照抄）

虚拟机里的官方练习裁判是**本机 web 服务**，开局是**两步**——只 `/api/start` 会停在暂停态：

```bash
# 1) 起裁判（stage 1 = 100ms × 100 Tick；stage 2 = 500ms × 500 Tick）
cd ~/rmtc_practice && ./start_referee.sh --stage 1
# 或  ./start_referee.sh --stage 2 --bots B,C,D

# 2) 起自己的车
cd ~/rmtc-llm-gateway && source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=27 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMTC_SEAT=A RMTC_TEAM_TOKEN=practice-A
export RMTC_AI_URL=http://127.0.0.1:8080/v1/chat/completions   # 第二阶段要 AI
python3 -m contestant.bot_node --seat A --stage 2

# 3) 开局：先 start（部署模板，仍暂停），再 resume（真正开始跑）
curl -s -X POST http://127.0.0.1:8080/api/start -H 'Content-Type: application/json' -d '{"stage":2}'
curl -s -X POST http://127.0.0.1:8080/api/resume -H 'Content-Type: application/json' -d '{}'

# 4) 看结果
curl -s http://127.0.0.1:8080/api/state | python3 -m json.tool | head -60
```

其他接口（`practice.py` 里能看到）：`/api/pause`、`/api/stop`、`/api/reset`、`/api/config/1`、`/api/config/2`。
每场日志在 `~/.rmtc-practice/<session_id>/`：`communication.jsonl`（感知/动作原文）、
`ai.jsonl`（AI 调用）、`score.json`、`config.json`、`frames.jsonl`。

## 四、虚拟机环境（2026-10-23 实测）

| 项 | 值 |
|---|---|
| 主机名 | `rogers-virtual-machine` |
| **当前 IP** | **`10.236.131.61`**（不是文档里的 192.168.40.128；网段变了，旧 IP 连不上） |
| 用户 | `rogers`（密码不在仓库里，见本地《交接文档.md》1.3 节） |
| Python / ROS | 3.10.12 / Humble |
| 练习包 | `~/rmtc_practice`（已解压） |
| 仓库副本 | `~/rmtc-llm-gateway`（用 `git archive` 打包拷进去的） |
| **虚拟机能不能上 GitHub** | **不能**（`curl https://github.com` → 000） |

### 操作通道（重要）

- **vmrun 在 DSH 沙箱里会报 "vmrun was unable to start"**——它要写 `C:\ProgramData\VMware`
  的日志/锁；把沙箱放到 `danger-full-access` 就正常了（本次已验证）。
- `vmrun runProgramInGuest` **不回显 stdout**：脚本一律把输出重定向到 `/tmp/xxx.txt`，
  再用 `copyFileFromGuestToHost` 取回。
- 取虚拟机 IP：`vmrun -T ws getGuestIPAddress "D:\Ubuntu 64 位.vmx" -wait`
- 拷贝进虚拟机：`vmrun -T ws -gu rogers -gp *** copyFileFromHostToGuest <vmx> <宿主机文件> <来宾路径>`

## 五、脚本（`scripts/vm/`）

| 脚本 | 作用 |
|---|---|
| `runmatch.sh <stage> [bots]` | 一键：清场 → 起裁判 → 起我们的车 → start+resume → 轮询到结束 → 打印得分/感知数/日志 |
| `runmatch2.sh <stage> [bots]` | 同上，第二阶段专用（带 `RMTC_AI_URL` 与 AI 调用统计） |
| `diag1.sh` | 翻某一场的 `state.json` + `communication.jsonl`（看最后感知/动作、score） |

宿主机侧配合：

```powershell
$vmrun = "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"
$vmx   = "D:\Ubuntu 64 位.vmx"
git archive --format=tar -o repo.tar HEAD
& $vmrun -T ws -gu rogers -gp *** copyFileFromHostToGuest $vmx repo.tar /tmp/repo.tar
& $vmrun -T ws -gu rogers -gp *** copyFileFromHostToGuest $vmx scripts\vm\runmatch.sh /home/rogers/runmatch.sh
& $vmrun -T ws -gu rogers -gp *** runProgramInGuest $vmx /bin/bash /home/rogers/runmatch.sh 1
& $vmrun -T ws -gu rogers -gp *** copyFileFromGuestToHost $vmx /tmp/match_1.txt .\match_stage1.txt
```

## 六、还没验证的（别当成已解决）

1. **第一阶段只跑了 1 局**（官方练习图是固定的，所以可复现，但没有跑不同地图）。
2. **第二阶段只有 1 局**，而且对手是裁判自带的参考车（B/C/D 得分 0，说明它们没被驱动起来），
   **不代表真实对抗强度**；对手强的时候"贪心捡资源 + 顺路打"未必还是满分。
3. **事件理解是 mock**：55 次调用全是模拟响应，模型没有真的读公告，
   所以"事件驱动的决策"这条路一次都没验证过。
4. 第二阶段的攻击/防御/任务坐标优先级是拍的，没有参数扫描。
5. `bot_node.py` 的 `--dry-run` 没在实机用过。

## 七、下次开工的三件事（按收益排序）

1. **接真实 LLM**：`start_referee.sh --stage 2 --ai-mode real` + `PRACTICE_LLM_URL/KEY/MODEL`，
   或者把我们自己的 `rmtc_gateway` 起在 8000 端口并 `--upstream` 指向真实服务，
   让 `RMTC_AI_URL=http://127.0.0.1:8000/v1/chat/completions`——顺便验证网关的 502/504/429 分支。
2. **多跑几局第二阶段**，把"打人 vs 捡资源"的权重做成参数扫一遍（现在只有一局的证据）。
3. 把第一阶段在不同随机地图下的表现，用裁判的 `--config` 自定义地图验一次
   （`/api/config/1` 可以导入完整配置）。


---

## 九、裁判 API 的新发现（2026-10-23 晚）

1. `POST /api/start` 的 body 支持完整场次参数，**可以加速练习**：

```json
{"stage": 2, "tick_ms": 100, "max_ticks": 200, "bots": ["B", "C", "D"],
 "config": { "……完整场地配置……" }}
```

   实测 settings 真的被应用（200 Tick × 100 ms = 20 秒一局）。默认是 500 Tick × 500 ms。

2. `config` 里的 `missions`（例如 reach 的坐标）也能改，改完 `/api/state` 里能看到。

```bash
curl -s -X POST http://127.0.0.1:8080/api/start -H 'Content-Type: application/json' \
  -d '{"stage":2,"tick_ms":100,"max_ticks":200,"bots":["B","C","D"],"config":<stage2 配置>}'
curl -s -X POST http://127.0.0.1:8080/api/resume -H 'Content-Type: application/json' -d '{}'
```

## 十、踩坑：Windows 上 `git archive` 出来的 .sh 是 CRLF

`core.autocrlf=true` 时，`git archive` 产出的 `.sh` 带 CRLF，丢进虚拟机 bash 直接报
`$'\r': command not found`。已加 `.gitattributes`（`*.sh text eol=lf`），脚本里也加了
`sed -i 's/\r$//'` 兜底。

## 十一、虚拟机 GitHub 连通性

插上网线后虚拟机可直连 GitHub（`curl https://github.com` → 200），可以 `git clone/pull`；
但 TLS 偶发失败（`GnuTLS recv error`），所以部署脚本做成"git 失败就用 tar 覆盖"。


## 十二、同一晚的补充验证（2026-10-23 深夜）

| 项 | 结果 |
|---|---|
| `--dry-run` | 裁判 stage 1 上跑 7 秒 → 50 条 `[dry-run]` 动作，字段（tick/session_id/action/direction）与真实发布一致 |
| 失联看门狗 | 无裁判时第 6.0 秒告警："5 秒没收到任何感知：检查 ROS_DOMAIN_ID / ROS_LOCALHOST_ONLY=0 / RMW_IMPLEMENTATION / 席位是否选对 / 裁判是否已开局" |
| `RMTC_TRACE` | 200 Tick 一局写出 201 行 jsonl（tick、位置、血量、score、动作），可逐 Tick 复盘 |
| 严格 A/B | 每格重起裁判+选手后，三次重复结果逐位相同 → 之前的"随机噪声"其实是**进程状态残留** |

### 怎么判断"这局到底通没通"

1. `RMTC_TRACE` 里有 200+ 行、tick 连续 → 感知这条链通了；
2. `/api/state` 的 `checks.A.action_tick` 和 `executed_tick` 都有值 → 动作被接受并结算了；
3. 感知里的 `self.last_action` 反映你上一步的动作（`moved`/`error`）；
4. 第二阶段还要 `ai.A.success_count >= 1`（本场本席位）。

只看 "感知 Tick 在涨" 不算通——那只证明裁判发出去了。


## 十三、批处理脚本的三个"假结果"陷阱（都踩过，已修）

做 A/B 最容易骗自己的不是策略，而是**跑批的脚本**。这三个都实际发生过，并且都在数据里留下过看似正常的分数：

| 陷阱 | 现象 | 现在怎么防 |
|---|---|---|
| 旧裁判还占着 8080 | 2.5 分钟"跑完"两局 500 Tick，两行分数完全一样 | `start_referee` 先等端口真空出来；开局后校验 `finished=false && tick<5`，否则重起重来 |
| 轮询等待写死 60 秒 | 正式节奏（250 秒/局）被截断在 tick≈120，得 25 分 | 等待上限改成 `ticks × tick_ms / 1000 + 90`；结束时再比对 `tick` 是否等于 `max_ticks`，不等就打印"被截断，结果不可信" |
| 两个批次并发 | CSV 行错乱、出现 `None` 字段 | `flock` 锁；宿主机的 pwsh 超时**不会**杀掉虚拟机里的脚本，超时后要先 `pkill` 再重跑 |


## 十四、第四个"假结果"陷阱：验证脚本会杀掉正在跑的实验

做规则合规验证时我在 `verify_fixes.sh` 里写了 `pkill -f practice.py`——而当时后台正有一个
2 次重复的正式节奏批次在跑。结果：

- `never,rep2` 整行缺失（裁判被换掉，`/api/state` 返回空 → JSON 解析失败）；
- `opportunistic,rep1` 记了 40.0 但 `rv=200 / damage=0 / missions=0`（对手没动 + 任务没判），
  一眼就是异常局。

**规范**：任何"真机验证"与"跑批实验"不能并行；验证脚本开头的 `pkill` 只应在确认没有批次在跑时执行。
判断方法：先看 `ps aux | grep -E 'stage2_ab2|practice.py' | grep -v grep` 是否为空，
再决定要不要 `pkill`。批次脚本本身有 `flock`，但 `pkill` 不受它保护。


## 十五、上 GitHub 找过什么、用了什么（2026-10-23 深夜）

先查有没有能直接用的现成方案，结论是**没有可用的"RMTC 机器人"项目**：

| 查到的东西 | 结论 |
|---|---|
| RoboMaster AI Challenge 系列（`LoveThinkinghard/RoboMaster-AI-Challenge-Simulator-2D` 等） | 2018–2019 的 C++/RL 仿真，接口与本赛完全不同，**不可用** |
| `brean/python-pathfinding`（MIT，380★） | 网格 A*/Dijkstra。我们的 BFS 在**等代价网格上已经最优**，引入只增加依赖风险 → 不引入 |
| `openai/openai-python`（Apache-2.0） | 官方客户端，23MB。比赛机要离线可跑，自己写的 urllib 客户端只有 200 行 → 只借它的错误分类思路 |
| `jd/tenacity`（Apache-2.0，8.8k★） | 重试库。借了"**抖动退避**"思路（多队共享网关时固定退避会撞车），自己实现 3 行 |
| `ndcorder/outputguard`（MIT） | LLM 结构化输出的 **13 种 JSON 修复策略**。直接借思路，自己写了 `repair_json_object`（单引号/注释/尾逗号/Python 字面量/截断补全） |

**原则**：比赛机要离线、要可复现、要能被裁判审查，所以能借"思路"就不加"依赖"。
最终 `contestant/`+`rmtc_client/` 仍然只有标准库依赖。

## 十六、踩坑：练习配置校验很严格

改事件日程想压测，第一次直接失败：

```text
{"error": "练习配置无效：events.schedule must contain configured announcement_count entries"}
```

`announcement_count` 必须**等于** `schedule` 的条数，写大了整局都不会部署，
而且失败后程序会继续跑在上一局残留的场次里——**看起来有分数、其实是假数据**。
以后凡是改练习配置，第一步先看 `/api/start` 的返回是不是 `{"ok": true, ...}`。
