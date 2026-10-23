# RMTC 统一 LLM 网关 + 参赛侧客户端

比赛：《RMTC 机甲大师图灵对决赛》校内赛（规则 v1.1 / 通信协议 v1.1）。
这个仓库把比赛要用到的「AI 通道」两端都写好了：

| 目录 | 是什么 | 谁在用它 |
|---|---|---|
| `rmtc_gateway/` | **网关服务端**：OpenAI 兼容的 `/v1/chat/completions`，只做转发与限额 | 组织方（比赛现场）那一侧的实现，本地也能起一份自己练 |
| `rmtc_client/` | **网关客户端**：异步、超时、降级、事件理解 | 我们的机器人在比赛中**真正要用的**那一半 |
| `contestant/` | **参赛程序**：策略（BFS 贪心）+ Agent（感知→动作）+ ROS 节点 + 第一阶段模拟器 | 上场跑的就是这块 |

一句话：**比赛只允许走主办方的统一网关调 AI**（协议 §5、规则 P5）。
客户端这一半必须自己写对（不然动作窗被等模型堵死、或者额度一用完就崩），
服务端这一半写出来是为了能在本机把整条链路 + 所有错误分支都验证一遍。

## 0. 先跑起来（30 秒）

```bash
cd rmtc-llm-gateway
python3 -m rmtc_gateway --port 8000          # 终端 1：起网关（默认本地模拟上游）
```

你应该看到：

```text
网关已就绪：http://127.0.0.1:8000
席位：A, B, C, D　令牌数：4　默认模型：mock-llm
练习令牌：practice-A  practice-B  practice-C  practice-D
上游：本地模拟（不做真实语义理解，只能用来验证链路与错误处理）。
```

另开一个终端：

```bash
curl -s http://127.0.0.1:8000/healthz | head -c 300        # 服务可用性
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer practice-A' \
  -d '{"messages":[{"role":"user","content":"用一句话说明采集目标"}]}'
```

再跑一个**不用 ROS** 的完整演示（同步调用 + 公告 → 异步理解 → 拿提示）：

```bash
python3 examples/llm_demo.py
```

跑测试（60 个，只用标准库，本机实测约 25 秒——时间主要花在超时/退避那几个用例上）：

```bash
python3 run_tests.py -v      # → Ran 60 tests in 24.940s / OK
```

## 1. 目录结构

三块代码互不依赖，可以分开看、分开测：

```text
rmtc-llm-gateway/
│
├── ① 网关服务端（协议 §5）────────────────────────────
│   rmtc_gateway/
│   ├── config.py              全部参数 + 环境变量（RMTC_GW_*）
│   ├── errors.py              错误码表：状态码 / type / code 三元组
│   ├── limits.py              额度、在途并发、来源 IP 频率
│   ├── upstream.py            上游：本地模拟 / 真实 HTTP 转发（不重试）
│   ├── server.py              两个端点 + 请求校验顺序 + 审计日志
│   └── __main__.py            python3 -m rmtc_gateway --port 8000
│
├── ② 网关客户端（机器人里用）──────────────────────────
│   rmtc_client/
│   ├── llm_client.py          LlmGatewayClient：1.5 s 超时、冷却、预算、按错误码降级
│   ├── event_understanding.py 公告 → 提示（校验场次与时效）
│   └── json_extract.py        从模型输出里抠第一个 JSON（字符串感知）
│
├── ③ 参赛程序（上场跑的）──────────────────────────────
│   contestant/
│   ├── agent.py               感知 → 动作：tick/session 去重、阵亡只能 wait、攻击判定
│   ├── strategy.py            策略：snake（原骨架）/ GreedyBfs（记忆+BFS 绕障）
│   ├── bot_node.py            ROS 2 节点（唯一依赖 rclpy 的一层）
│   ├── sim.py                 第一阶段离线模拟器（按官方 stage1.json 参数）
│   └── run_experiments.py     策略对比实验：官方图 + N 张随机图
│
├── 测试（按上面三块分文件）─────────────────────────────
│   tests/test_gateway.py      ①  26 个：每个错误码、配额、并发、审计
│   tests/test_client.py       ②  19 个：超时/降级/退避/公告理解/JSON 抽取
│   tests/test_contestant.py   ③  15 个：模拟器、策略、Agent、9×9 解析
│   run_tests.py               一条命令跑完 60 个测试
│
├── 文档与脚本
│   docs/PROTOCOL_MAP.md       协议条款 → 代码 → 测试 的对照表
│   docs/STRATEGY_NOTES.md     策略实验记录（每次测了什么、结论）
│   docs/practice_stage1.json  官方练习包的 stage1 地图配置（原样拷入）
│   examples/llm_demo.py       不用 ROS 的网关演示
│   start_gateway.sh           虚拟机里启动网关
└── README.md
```

只跑某一块的测试：

```bash
python3 -m unittest tests.test_gateway -v      # 只测网关
python3 -m unittest tests.test_client -v       # 只测客户端
python3 -m unittest tests.test_contestant -v   # 只测参赛代码
```

## 2. 参赛程序怎么接（这一节是比赛当天要看的）

### 2.1 把现场信息交给程序，不要写死

```bash
export RMTC_SEAT=A                                    # 主办方给的席位
export RMTC_TEAM_TOKEN='本场席位令牌'                  # 主办方给的令牌（每场重新签发）
export RMTC_AI_URL='http://<裁判机 IP>:8000/v1/chat/completions'
```

协议白纸黑字：令牌、场次、地图、任务坐标**都不能写死**，写了上场就废。

### 2.2 十行接进机器人

```python
import os
from rmtc_client import ClientConfig, EventUnderstanding, LlmGatewayClient

client = LlmGatewayClient(ClientConfig(
    url=os.environ["RMTC_AI_URL"],          # 现场地址
    token=os.environ["RMTC_TEAM_TOKEN"],    # 现场令牌
    timeout_s=1.5,                          # 绝不能等到动作窗关闭
    min_interval_s=2.0,                     # 两条公告之间至少隔 2 秒
))
understanding = EventUnderstanding(client, max_age_ticks=20)

# 收到感知时（on_perception 里）：
understanding.offer(session_id, tick, event_text, mission_text=mission_text,
                    position=me["position"], map_size=(mw, mh), seat=seat)
hint = understanding.hint(session_id, tick)     # 拿不到就是 None，本地策略继续跑
```

`offer()` 只做两件事：去重 + 开一个后台线程，**立刻返回**；
`hint()` 永远不阻塞。模型回得慢、回错了、额度没了，都是"没有提示"，不是"卡住"。

### 2.3 客户端替你处理的错误码

| 网关返回 | 客户端行为 | 机器人该做什么 |
|---|---|---|
| `quota_exceeded` (429) | 永久停用 AI 通道 | 继续本地策略（协议要求"额度用尽必须自主降级"） |
| `invalid_token` / `missing_token` (401) | 永久停用 | 报告裁判，程序别崩 |
| `rate_limit` / `concurrency_limit` (429) | 退避 `backoff_s` 后再试 | 不影响本 Tick |
| `audit_unavailable` (503) | 退避，不降级 | 联系裁判复核 |
| `upstream_unavailable` (502) / `upstream_timeout` (504) | 退避，不降级 | 本地策略继续 |
| 网络断 / 超时 | `local_timeout` | 本地策略继续 |

所有失败都是返回值（`LlmResult(ok=False, code=...)`），**不会抛异常**。

## 3. 网关实现了什么（协议 §5 逐条）

| 协议要求 | 实现位置 | 测试 |
|---|---|---|
| `GET /healthz` 可用性检查 | `server.py: do_GET` | `test_healthz_ok_without_token` |
| `POST /v1/chat/completions`，OpenAI 兼容 | `server.py: do_POST` | `test_mock_success_shape` |
| `Authorization: Bearer <令牌>` 必填 | `server.py: _authenticate` | `test_missing_token` / `test_invalid_token` |
| 请求体上限 2097152 字节 → 413 | `server.py: _read_body` | `test_payload_too_large` |
| 带 `Transfer-Encoding` → 501 | `server.py: _read_body` | `test_transfer_encoding_rejected` |
| 每 IP 每 10 秒最多 100 次 POST | `limits.py: RateLimiter` | `test_rate_limit_per_ip` |
| 每令牌最多 2 个在途调用 | `limits.py: TokenState` | `test_concurrency_limit` |
| 只有 2xx 消耗额度、失败释放 | `limits.py: reserve/commit/release` | `test_rejected_request_does_not_consume_quota`、`test_upstream_timeout_is_504_and_free` |
| 额度耗尽 429 `quota_exceeded` | `server.py` + `errors.py` | `test_quota_exhaustion` |
| 网关不重试上游；响应整体缓冲 | `upstream.py` | `test_http_upstream_forwards_and_defaults_model` |
| 上游连不上 502 / 超时 504（默认 30 s） | `upstream.py` | `test_upstream_unreachable_is_502`、`test_upstream_timeout_is_504_and_free` |
| 网关不提供配额查询 | `server.py: health` | `test_healthz_does_not_expose_quota_usage` |
| 令牌不得写进日志 | `server.py: GatewayState.audit` | `test_audit_records_seat_but_never_token` |

表里没写的两条：`not_found`(404)、`invalid_json` / `invalid_messages` / `stream_unsupported`(400)
也都有实现和测试——它们属于"协议没点名、但 OpenAI 兼容接口必须有"的那一类。

## 4. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `RMTC_GW_HOST` | `0.0.0.0` | 监听地址 |
| `RMTC_GW_PORT` | `8000` | 监听端口 |
| `RMTC_GW_TOKENS` | 练习令牌 A–D | `A=令牌A,B=令牌B`，正式比赛按现场令牌填 |
| `RMTC_GW_QUOTA` | `100` | 每令牌额度，`0`=不限 |
| `RMTC_GW_IP_LIMIT` / `RMTC_GW_IP_WINDOW_S` | `100` / `10` | 每 IP 频率 |
| `RMTC_GW_INFLIGHT` | `2` | 每令牌在途上限 |
| `RMTC_GW_UPSTREAM_URL` | 空 | 空=本地模拟上游；填了就是真实转发 |
| `RMTC_GW_UPSTREAM_KEY` | 空 | 上游密钥（若有） |
| `RMTC_GW_MODEL` | `mock-llm` | 请求没带 `model` 时用的默认模型 |
| `RMTC_GW_AUDIT` | 空 | 审计日志 jsonl 路径（只有席位、状态码，没有令牌原文） |
| `RMTC_GW_VERBOSE` | 空 | `1` 打印每条请求 |

## 5. 边界（说清楚不做什么，避免误会）

1. **不做决策。** 网关只转发与限额；提示词、输出解析、动作合法性、回退，全在参赛程序里。
2. **模拟上游不做语义理解。** 默认上游只回一句固定说明，防止有人拿它当"离线版 AI"。
   比赛的随机事件必须由主办方网关的真实模型理解——用正则/关键词/模板顶替是违规的（P5）。
3. **不打印正式令牌。** 只有全部令牌都以 `practice-` 开头时才打印到控制台。
4. **审计日志里没有令牌原文**，只有席位、状态码、错误码、大小、耗时。

## 6. 参赛代码（`contestant/`）：策略怎么选、怎么测

### 6.1 到底改了策略的哪里

原骨架（桌面上的 `rmtc_bot.py`）：**看得见就朝最近资源走，看不见就蛇形扫图**，没有记忆、不认障碍。

现在的 `GreedyBfsStrategy`：

1. **记忆**：凡是在视野里出现过的资源都记下来，看不见了也知道该往哪走（协议允许记住此前合法看到的位置）；
2. **BFS 绕障**：算路时避开障碍和"撞过的格子"，而不是撞墙浪费 Tick；
3. **排序** `priority = 价值 / (距离 + detour)`：detour 小 → 谁近去谁；detour 大 → 谁值钱去谁。

### 6.2 实测（本机，模拟器，官方 stage1 地图参数）

官方练习地图：

| 策略 | 得分 | 采集 |
|---|---|---|
| snake（原骨架） | 28.0 / 40 | 140 / 200 |
| bfs detour=0 / 2 / 6 | **40.0 / 40** | 200 / 200 |
| bfs detour=20（抢高价值） | 38.0 / 40 | 190 / 200 |

100 张等密度随机地图：

| 策略 | 平均 | 最低 | 满分场次 |
|---|---|---|---|
| snake（原骨架） | 19.92 / 40 | 0.0 | 3 / 100 |
| **bfs detour=0（抢最近）** | **33.56 / 40** | 10.0 | **26 / 100** |
| bfs detour=6 | 33.52 / 40 | 10.0 | 20 / 100 |
| bfs detour=20（抢高价值） | 33.52 / 40 | 10.0 | 19 / 100 |

**结论：先抢最近的比先抢值钱的更划算**（10 秒只有 100 步，绕远路去拿 20 分反而少捡两个 10 分）。
所以默认 `detour=0`。要复现：

```bash
python3 -m contestant.run_experiments --maps 100
```

### 6.3 `Agent` 处理的是"丢分点"，不是"聪明点"

比赛里最容易白丢分的几件事，都在 `contestant/agent.py` 里挡住了（都有测试）：

- 同一 Tick 重发 → 不重复决策；旧帧 → 丢弃；
- 新 `session_id` → 清空上一场的定位、记忆、在途 AI 请求；
- 阵亡 → 只能 `wait`；出生保护期间 → 不攻击；
- `self.position`（绝对坐标）和 `visible_entities[].position`（9×9 字符串）**两种解析方式分开**；
- 字段缺失/类型错 → 不崩，这一 Tick 按 `wait` 处理；
- 攻击只在**同一行或同一列、1~2 格内**给方向（斜着打不到）。

### 6.4 真实上场

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=27 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMTC_SEAT=A RMTC_TEAM_TOKEN=<本场令牌>
export RMTC_AI_URL=http://<裁判机 IP>:8000/v1/chat/completions
python3 -m contestant.bot_node --seat A --stage 2          # 先加 --dry-run 看解析对不对
```

> 诚实交代：`bot_node.py` 需要 ROS 2，**本仓库的测试没有跑到它**（Windows 上没有 rclpy）；
> 它的逻辑本体在 `agent.py`，那部分有 15 个测试。第二阶段对抗和随机事件也没有模拟器，
> 只能靠官方练习裁判 + 实机验证。

## 7. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `address already in use` | 8000 被练习裁判占了；练习裁判自己就在 8080/8000 上，换 `--port 8001` |
| 客户端一直 `local_timeout` | 地址/端口不对，或网关没起；先 `curl /healthz` |
| 一直 401 `invalid_token` | 令牌和本场席位不匹配；每场令牌都会重新签发 |
| 一上场就 `quota_exceeded` | 本场额度用完了（只能通过这个错误码知道）；客户端已自动降级 |
| 虚拟机里 `./start_gateway.sh: Permission denied` | vmrun 拷贝会丢文件模式：`chmod +x start_gateway.sh` |
| 想连真实模型验证 | `--upstream https://…/v1/chat/completions --upstream-key "$KEY"` |

## 8. 实测记录（真实输出，不是示意）

写这份代码时在本机跑过的结果，原文粘贴：

```text
$ python3 run_tests.py          # 网关+客户端那轮（45 个）
Ran 45 tests in 24.369s
OK

$ python3 run_tests.py          # 加上参赛代码测试之后（60 个）
Ran 60 tests in 24.940s
OK
```

```text
$ curl -s http://127.0.0.1:8123/healthz
{"status": "ok", "gateway": "rmtc-llm-gateway", "version": "1.0.0", "upstream": "mock",
 "model": "mock-llm", "tokens": 4, "limits": {"ip_window_s": 10.0, "ip_limit": 100,
 "max_inflight_per_token": 2, "max_body_bytes": 2097152, "upstream_timeout_s": 30.0},
 "stats": {"requests": 0, "ok": 0, "rejected": 0, "upstream_errors": 0}, "uptime_s": 4.7}
```

```text
$ curl -s http://127.0.0.1:8123/v1/chat/completions -H 'Authorization: Bearer practice-B' ...
call 1 -> 200
call 2 -> 200
call 3 -> 200
call 4 -> 429
{"error": {"message": "Seat token quota exhausted", "type": "insufficient_quota",
           "param": null, "code": "quota_exceeded"}}
```

```text
$ python3 examples/llm_demo.py --token practice-A
ok(21ms) '{"mock": true, "note": "本地模拟上游：只验证网关链路与错误处理…"}'
（客户端自带 1 秒冷却：两次调用不能贴在一起，省额度也省动作窗）
[info] 事件理解结果（Tick 50）：{"mock": true, "note": "…"}
统计： {'offered': 1, 'finished': 1, 'pending': 0,
       'client': {'attempts': 2, 'ok_calls': 2, 'degraded': '', 'last_code': ''}}
```

审计日志（每请求一行，没有令牌原文）：

```json
{"ts": 1792761557.97, "ip": "127.0.0.1", "path": "/v1/chat/completions", "status": 200,
 "code": null, "model": "mock-llm", "bytes": 45, "seat": "B", "latency_ms": 0.2}
```

## 9. 把仓库弄进虚拟机

宿主机上（Windows，路径按实际情况改）：

```bash
# 方式一：先用 SSH 拉（虚拟机里已有网络即可）
git clone git@github.com:<你的账号>/rmtc-llm-gateway.git ~/rmtc-llm-gateway
chmod +x ~/rmtc-llm-gateway/start_gateway.sh
cd ~/rmtc-llm-gateway && python3 run_tests.py

# 方式二：用 vmrun 直接把目录拷进去（不需要网络）
#   vmrun -T ws -gu rogers -gp <密码> copyFileFromHostToGuest <vmx> <宿主机文件> <虚拟机路径>
#   注意 vmrun 会丢掉文件模式，拷完要 chmod 644 / chmod +x
```

在虚拟机里起网关后，参赛程序用 `127.0.0.1:8000` 就能连（同一台机器），
别的机器要连就把地址换成虚拟机 IP，并确认网关监听在 `0.0.0.0`（默认）。

## 10. 依据的文档

- 《RMTC 机甲大师图灵对决赛赛事规则 v1.1》
- 《RMTC 机甲大师图灵对决赛通信协议 v1.1》§3 感知 / §5 LLM 网关 / §7 联调
- 《裁判测试端使用教程 v1.1》、练习包 `rmtc_practice`

规则若有更新，以官方最终发布为准。
