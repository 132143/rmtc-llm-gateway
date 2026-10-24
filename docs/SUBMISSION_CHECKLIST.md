# 提交清单（按赛事规则 v1.1 / 通信协议 v1.1 逐项核对）

> 规则 v1.0 已作废，以下**只按 v1.1** 执行：对抗分 = `15 × min(D/500, 1)`，资源分 = `30 × min(R2/120, 1)`，
> 任务分 = `5 × M`（M 需本场真实 AI 调用资格）。练习裁判仍按旧口径算分，**看分数要换算**。

## 1. 递交什么、以什么形式、多少份

| 材料 | 形式 / 数量 | 我们要交的东西 | 就绪 |
|---|---|---|---|
| 代码 | 代码仓库（1 份）+ 可运行压缩包（1 份） | GitHub 仓库 + `rmtc-competition-2026-10-23.zip` | ✅ |
| 目录与运行说明 | 写在仓库 README 里（规则要求"目录结构和运行指令清晰"） | `README.md` + `docs/COMPETITION_PLAN.md` §1 | ✅ |
| 技术报告 | **语雀文档，一队一份**，协作填写 | 模板见 `docs/TECH_REPORT.md`（可直接粘进语雀） | ⬜ 待填队内分工/截图 |
| 运行截图/录屏 | 前置检录用（数量不限，能证明四件事即可） | 见 §3 的四张截图脚本 | ⬜ 待你在虚拟机里录 |
| 镜像 | **可选**（Q&A：会 docker 就交，不会不做要求） | 未提供；如需，按 §4 现场构建 | ⬜ 可选 |
| decision_type 登记 | 赛前向裁判登记类型标识/作用/读取方式 | `collect` / `balanced` / `combat`（`docs/COMPETITION_PLAN.md` §3） | ⬜ 赛前提交 |

**时间点**：队伍调整 10/30 23:59 截止 → **材料 10/31 20:00 截止** → 11/1 线下比赛 → 11/2 23:59 成绩公示。

## 2. 代码侧"提交即现场"的一致性

- 封存用的 commit 必须与现场启动的一致：`git rev-parse HEAD` 记下来（当前 `b1d6a16` 之后的提交）；
- `python run_tests.py` 必须全绿（当前 110 个用例 + 全量语法检查）；
- 现场只改这三类：环境变量（席位/令牌/网关地址/域号）、`decision_config.json` 的 `decision_type`、
  以及裁判批准的两次策略切换；**不改源码**。

## 3. 前置检录：四张截图（缺一不可）

| # | 截图内容 | 怎么取证 |
|---|---|---|
| 1 | 程序启动成功 | 终端里 `python3 -m contestant.bot_node --seat A --stage 2` 的启动日志（含"决策类型：..."一行） |
| 2 | 收到感知 | `RMTC_TRACE=/tmp/trace_A.jsonl` 后 `wc -l` 显示 tick 连续增长；或网页上感知 Tick 在涨 |
| 3 | 动作被接受 + 有回执 | 网页"动作 Tick"与"执行 Tick"都有值；轨迹里 `ack.moved=true`；下一条感知的 `self.last_action` 是你的动作 |
| 4 | 真实 AI 成功（第二阶段） | 裁判侧显示本席位成功调用 ≥1（我们第 3 Tick 会主动发心跳）；或网关审计里 200 + usage |

> 提示：`--dry-run` 只验证解析，不算检录；检录必须真发动作。
> **开局前必须先启动程序**：实测晚启动 26 Tick 就会丢掉全部开局资源（那一局资源 0、任务 0/3、8.1 分）。
> 开局后第一件事是确认感知的第一帧 `tick=0`。

## 4. 如果要交镜像（可选）

在 Ubuntu 上二选一：

```bash
# 方式 A：直接交虚拟机（Q&A 允许）
#   把 /home/rogers/rmtc-llm-gateway 和 ROS 2 Humble 环境一起打包导出

# 方式 B：Docker（需自行验证，本仓库未提供已验证的镜像）
FROM ros:humble-ros-base
RUN apt-get update && apt-get install -y python3-pip && rm -rf /var/lib/apt/lists/*
COPY . /opt/rmtc
WORKDIR /opt/rmtc
CMD ["python3", "-m", "contestant.bot_node", "--seat", "A", "--stage", "2"]
```

## 5. 赛前 30 分钟现场核对

1. `git rev-parse HEAD` 与封存版本一致；
2. `decision_config.json` 里 `decision_type` 是登记的枚举之一；
3. 环境变量：`ROS_DOMAIN_ID` / `ROS_LOCALHOST_ONLY=0` / `RMW_IMPLEMENTATION` / `RMTC_SEAT` / `RMTC_TEAM_TOKEN` / `RMTC_AI_URL`；
4. `curl <网关>/healthz` 返回 200；
5. 先跑一次 `--dry-run` 看解析，再正式启动，按 §3 四张截图逐一核对。
