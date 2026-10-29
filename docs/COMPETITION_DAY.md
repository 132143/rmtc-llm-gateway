# 比赛日操作手册（一步步照着做）

> 适用：规则 v1.2 ｜ 封存 `seal-v1.2-20261029c` ｜ 只要 5 个命令，全是复制粘贴。
> 两件事分开看：**“我想看它跑”**（第 1 节）和**“比赛那天怎么操作”**（第 2 节）。

---

## 1. 想“看到”机器人跑起来——三种看法，从快到真

### 看法 A：离线回放（10 秒，不用 ROS，不联网，不花钱）

先跑一局并把轨迹写下来，再在终端里“放”出来：

```bash
cd rmtc-llm-gateway
python -m contestant.eval_lab --groups guarded_hybrid --reps 1 --out docs/results/demo

# 用一局真实轨迹生成可视化（离线仿真也会写同样的 trace 格式）
python - <<'PY'
import sys; sys.path.insert(0, ".")
from contestant.agent import Agent, AgentConfig
from contestant.eval_lab import ENVIRONMENTS, OPPONENTS, make_brain
from contestant.stage2_sim import Stage2Simulation
from contestant.events import default_script
open("/tmp/trace_A.jsonl", "w").close()
m = ENVIRONMENTS["open24"](3)
brains = {"A": Agent(AgentConfig(seat="A", stage=2), trace_path="/tmp/trace_A.jsonl")}
for s in ("B", "C", "D"):
    brains[s] = make_brain(s, OPPONENTS["probe_mid"])
Stage2Simulation(m, brains, ticks=500, session_id="demo", events=default_script(m)).run()
PY

python -m scripts.trace_view /tmp/trace_A.jsonl --every 25
```

你会看到：每 25 片打印一张 24×24 的图（`A` 是车、`o` 是走过的路、`#` 是撞墙格），
外加这一片的位置、血量、资源、伤害、任务数。加 `--play` 就变成终端动画。

### 看法 B：本地网关 + 客户端演示（验证 AI 链路，30 秒）

```bash
python -m rmtc_gateway --port 8000 &            # 本地 OpenAI 兼容网关（模拟上游）
python examples/llm_demo.py --url http://127.0.0.1:8000/v1/chat/completions --token practice-A
```

### 看法 C：练习裁判（最接近现场，能看到裁判视角的分数与动作回执）

在 Ubuntu 虚拟机里：

```bash
cd ~/rmtc_practice && ./start_referee.sh --stage 2 --ai-mode mock
# 浏览器打开 http://127.0.0.1:8080 ：能实时看到「感知 Tick / 动作 Tick / 执行 Tick」和四队分数
```

再看自己的轨迹（这条最有信息量）：

```bash
export ROS_DOMAIN_ID=27 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMTC_SEAT=A RMTC_TEAM_TOKEN=practice-A
export RMTC_AI_URL=http://127.0.0.1:8080/v1/chat/completions
export RMTC_TRACE=/tmp/trace_A.jsonl          # ← 想复盘就加这一行
python3 -m contestant.bot_node --seat A --stage 2
# 跑完后：python -m scripts.trace_view /tmp/trace_A.jsonl --every 50
```

---

## 2. 比赛日流程（按时间倒着写，照做即可）

### 阶段 0：赛前 1 天（做完了就不用管）

| 步 | 做什么 | 判据 |
|---|---|---|
| 0.1 | 提交技术报告（语雀）与代码仓库/压缩包 | 链接发群里，队长确认 |
| 0.2 | 把 `decision_type` 登记表交给裁判 | 裁判确认收到 |
| 0.3 | 本地跑 `python run_tests.py` | 看到 `Ran 249 tests` + `OK` |
| 0.4 | 记录封存版本 | `git rev-parse HEAD` 与 `SEALED_VERSION.txt` 一致 |

### 阶段 1：到场后（约 T-60 分钟）

| 步 | 做什么 | 判据（不对就别上场） |
|---|---|---|
| 1.1 | 拷代码到参赛机：解压交付包，或 `git clone` 后 `git checkout seal-v1.2-20261029c` | 目录里有 `contestant/`、`run_tests.py` |
| 1.2 | `python run_tests.py` | `Ran 249 tests` → `OK` |
| 1.3 | `python -m scripts.preflight --skip-ai` | 只允许 `[WARN]`（healthz/封存），不能有 `[BAD]` |

### 阶段 2：填写现场信息（约 T-45 分钟）

| 步 | 做什么 | 说明 |
|---|---|---|
| 2.1 | `export RMTC_SEAT=A`（或现场给我们的席位） | 席位错了就发到别人的话题上 |
| 2.2 | `export RMTC_TEAM_TOKEN=<本场令牌>` | 只从环境变量给；程序没有默认值，缺了会直接报错退出 |
| 2.3 | `export RMTC_AI_URL=<现场网关>` | 基址 / 带 `v1` / 完整地址三种写法都行，程序会自动补全 |
| 2.4 | `export ROS_DOMAIN_ID=<现场域号> ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp` | 域号必须与裁判一致 |
| 2.5 | **不要** `export RMTC_MODEL` | 官方要求用统一默认模型，填了会告警 |
| 2.6 | `python -m scripts.preflight`（带真实调用） | 期望看到 `[OK] 真实调用：成功 …ms，thinking=disabled，max_tokens=512` |

> 这一步的输出就是**“AI 资格”的第一份证据**，截图留存。

### 阶段 3：启动程序 + 四张检录截图（约 T-30 分钟）

| 步 | 做什么 | 要截的图 |
|---|---|---|
| 3.1 | `export RMTC_TRACE=/tmp/trace_$RMTC_SEAT.jsonl`（可选，强烈建议） | — |
| 3.2 | 第一阶段：`python3 -m contestant.bot_node --seat A --stage 1` | **图 1**：启动日志（含「席位 A 就绪…」） |
| 3.3 | 看裁判页面「感知 Tick」在涨 | **图 2**：感知 Tick 增长 |
| 3.4 | 看裁判页面「动作 Tick / 执行 Tick」都有值 | **图 3**：动作被接受 + 回执 |
| 3.5 | 第二阶段：`--stage 2`，观察日志里 `[ai:info] AI 请求参数：thinking=disabled…` 与裁判端「LLM 成功 N 次」 | **图 4**：真实 AI 成功（第二阶段） |

> `--dry-run` 只打印动作、不真发，**不能**用来检录。

### 阶段 4：开赛前 5 分钟

| 步 | 检查 |
|---|---|
| 4.1 | `cat decision_config.json` → 只有 `decision_type`，值是登记的 `balanced`（=pressure/hybrid） |
| 4.2 | 首帧感知 `tick=0` 已收到（晚 join 会丢开局资源，实测晚 26 片就崩） |
| 4.3 | 终端里没有红色 traceback；`RMTC_TRACE` 文件在长 |
| 4.4 | 若裁判要求改席位/令牌/地址：只改环境变量，**不改代码** |

### 阶段 5：比赛进行中（你只做两件事）

1. **盯异常**：终端里出现 traceback、或裁判页面「执行 Tick」不动了 → 立刻举手报告裁判；
2. **不碰代码、不重启**（重启＝重新 join，会丢资源；除非裁判明确要求）。

程序自己会：抢最近资源 → 开局 60 片不换血 → 到点做任务 → 战争期巡逻/压制 → 封顶后照常打。

### 阶段 6：一局结束（60 秒内做完）

| 步 | 做什么 |
|---|---|
| 6.1 | 截「最终比分页」；把 `/tmp/trace_A.jsonl` 拷到 U 盘/网盘 |
| 6.2 | 记一行：时间 / 对手 / 分数 / 名次 / AI 成功次数 / 无效动作 |
| 6.3 | 看 `python -m scripts.trace_view /tmp/trace_A.jsonl --summary` 的摘要，判断这局输在哪（资源？任务？死亡？） |

---

## 3. 出问题怎么办（照这张表切换）

| 现象 | 最可能原因 | 当场怎么做 |
|---|---|---|
| 启动就报「缺少 RMTC_TEAM_TOKEN」 | 忘了 export | 补上令牌再启动（这是刻意的快速失败） |
| 启动就报「缺少 RMTC_AI_URL」 | 同上 | 补网关地址 |
| 裁判页面感知 Tick 不涨 | 域号/QoS 不一致，或没 `source /opt/ros/humble/setup.bash` | 先 source ROS，再核对 `ROS_DOMAIN_ID` |
| AI 一直失败但程序照跑 | 令牌错 / 网关地址错 | `python -m scripts.preflight` 看具体错误码；`invalid_token` 立刻找裁判 |
| 分数明显偏低（<30） | 该图走廊密/被针对 | 经裁判同意后把 `decision_type` 改 `turtle`（保命优先）再开下一局 |
| 想回退到上一版行为 | — | `decision_type` 改 `economy` → 方案A 两条机制立即失效 |
| 程序崩溃 | — | 重启即可（同一 session 内会重新 join，别改代码） |

---

## 4. 一句话记住的三件事

1. **令牌和网关只从环境变量给**，缺了程序会直接拒绝启动（不会静默 401 丢 15 分）；
2. **开赛前跑一次 `python -m scripts.preflight`**，全绿再上场，输出就是 AI 资格证据；
3. **现场只允许改两个东西**：环境变量、`decision_config.json` 的 `decision_type`。
