# 现场运行手册（只含上场必需的东西）

> 这个目录 = **比赛现场只需要的东西**。没有测试、没有文档、没有实验台。
> 对应提交版仓库里的 `contestant/` + `rmtc_client/` + `scripts/preflight.py`。

## 0. 前置条件（一次性）

```bash
python3 -c "import rclpy; print(\"rclpy OK\")"   # 没有就装 ROS 2 Humble
```

## 1. 现场四步（每场开始前 3 分钟）

```bash
# ① 环境变量（只改这四个；不要设 RMTC_MODEL）
source /opt/ros/humble/setup.bash
export RMTC_SEAT=A                          # 现场给的席位
export RMTC_TEAM_TOKEN=<本场令牌>            # 缺了会直接报错退出
export RMTC_AI_URL=<现场网关>                # 基址 / 带 v1 / 完整地址都可
export ROS_DOMAIN_ID=<现场域号> ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp

# ② 自检（会真调一次模型，输出就是 AI 资格证据）
cd <本目录>
python3 -m scripts.preflight

# ③ 起程序（第一阶段 / 第二阶段分别跑）
export RMTC_TRACE=/tmp/trace_A.jsonl        # 可选：留轨迹用于复盘
python3 -m contestant.bot_node --seat A --stage 1
python3 -m contestant.bot_node --seat A --stage 2

# ④ 跑完复盘（可选）
python3 -m scripts.trace_view /tmp/trace_A.jsonl --summary
```

## 2. 判据（不对就别上场）

| 检查 | 应该看到 |
|---|---|
| preflight | `[OK] 真实调用：成功 … thinking=disabled` + `结论：可以上场` |
| 启动日志 | `席位 A 就绪：订阅 /team_A/perception…` 与 `[ai:info] AI 请求参数：thinking=disabled` |
| 裁判端/诊断页 | 感知 Tick 在涨、动作 Tick 有值、**执行 Tick 有值**、LLM 成功 ≥1 |

## 3. 现场允许改什么

| 允许 | 禁止 |
|---|---|
| 环境变量（席位 / 令牌 / 网关 / 域号） | 改源码、算法、Prompt、其他决策参数 |
| `decision_config.json` 的 `decision_type`（须经裁判批准并登记，每场≤2 次） | 人工逐 Tick 发动作 |
| 按规定重启（每阶段≤2 次，用同一封存版本） | 用观赛画面/录屏喂给程序 |

切换决策类型即回滚：`{"decision_type":"economy"}`（旧行为）或 `turtle`（保命优先）。

## 4. 这个目录里有什么

```text
contestant/          参赛程序（bot_node + agent + 策略 + 事件 + 离线仿真）
rmtc_client/         AI 客户端（thinking 关闭 / 字段降级 / 预算与退避）
scripts/preflight.py 现场自检（六项：环境变量/网关/真实 AI/decision_type/封存/令牌不外泄）
scripts/trace_view.py 轨迹回放（可选，复盘用）
decision_config.json 现场唯一允许改的文件
SEALED_VERSION.txt   封存记录（commit / tag / 一键回滚）
```
