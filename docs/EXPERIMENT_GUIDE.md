# 实验流程（SOP）：怎么安全地改、怎么判断该不该采纳

> 适用对象：队内任何人 + 他的 AI agent。
> 一条铁律：**只看均值就宣布胜利 = 这个项目里踩过最多的坑**。判断标准是"均值 + 下限 + 低分概率"三件套。

---

## 0. 每次实验前的 30 秒自检

```bash
cd rmtc-llm-gateway
python run_tests.py            # 必须 Ran 253 tests → OK
python -m scripts.repo_scan    # 必须 5 行 [OK]
```

任何一项不过 → **先修环境，别开实验**（脏基线会让所有对比作废）。

---

## 1. 做一个改动：三步走

### 1.1 先做成开关（默认关或默认等于现状）

在 `contestant/agent.py` 的 `AgentConfig` 里加参数，默认值 = **现状**，例如：

```python
def __init__(self, ..., my_new_rule=False):     # 默认关
    self.my_new_rule = bool(my_new_rule)
```

行为代码里用 `if self.cfg.my_new_rule:` 包住。**不要直接改死 Behavior**。

### 1.2 注册成实验分组（放在 `contestant/eval_lab.py` 的 GROUPS）

```python
"guarded_mine": {"kind": "combo", "planner": "guarded", "combat_mode": "hybrid",
                "memory_falsify": True, "my_new_rule": True},
```

### 1.3 写测试（必须）

至少 3 条：**开关关 = 旧行为**、**开关开 = 新行为**、**边界不崩**。放 `tests/`，跑 `python -m unittest tests.test_你的文件 -v`。

---

## 2. 跑对照（这是实验的主体）

### 2.1 标准批次（96 局/组，约 6 分钟）

```bash
python -m contestant.eval_lab \
  --groups guarded_hybrid,guarded_mine \
  --envs open24,dense24,maze24,practice18 \
  --opponents probe_mirror,probe_weak,probe_farmer,probe_strong \
  --reps 6 --ticks 500 \
  --out docs/experiments/mine
```

| 参数 | 含义 | 怎么选 |
|---|---|---|
| `--groups` | 被测方案 | 一次只改一个变量；A = 现状，B = 你的方案 |
| `--envs` | 4 张图 | open24 空旷 / dense24 障碍密 / maze24 走廊 / practice18 官方尺寸 |
| `--opponents` | 对手 | **`probe_mirror` 是对等对手（与封存默认逐字段相同）**；其余是更弱的基线/内核/扫图/只采集 |
| `--reps` | 每格重复次数 | 6 → 96 局/组；要更稳就 10 以上 |
| `--events` + `--hint-cache docs/events_cache.json` | 打开事件注入（用真实模型判断回放） | 改的东西和事件有关就必须加 |

### 2.2 三把尺子（跑完必须都看）

```bash
# ① 成对比较：均值差 + 95% 置信区间 + 符号检验
python -m scripts.analyze_lab docs/experiments/mine/lab_raw.json guarded_hybrid guarded_mine

# ② 丢任务概率（任务分是 15 分的大头）
python -m scripts.loss_report docs/experiments/mine/lab_raw.json guarded_hybrid guarded_mine

# ③ 自己算 P10 / P(score<30) / 最低分（别只看均值）
python - <<'PY'
import json, statistics as st
rows=[r for r in json.load(open("docs/experiments/mine/lab_raw.json",encoding="utf-8")) if r["group"]=="guarded_mine"]
ss=sorted(r["group_score"] for r in rows); n=len(ss)
print("均值 %.2f 最低 %.1f P10 %.1f P(<30) %.1f%%" % (st.fmean(ss), ss[0], ss[int(0.1*n)-1], 100*sum(1 for x in ss if x<30)/n))
PY
```

### 2.3 结果在哪看

| 文件 | 内容 |
|---|---|
| `docs/experiments/<名字>/lab_summary.md` | 一张表：均值/最低/P10/名次/资源/伤害/任务/死亡/无效/撞墙/异常/提示命中 |
| `.../lab_raw.json` | 逐局原始数据（配对统计的数据源） |
| `.../lab_summary.json` | 机器可读汇总 |

---

## 3. 判定：三条全过才能改默认值

| 指标 | 通过条件 | 为什么 |
|---|---|---|
| 均值 | 不低于现状（噪声内可接受，但要说明） | 惯性 |
| **最低分 / P10** | **不能变差** | 下限决定你第 4 名还是第 1 名 |
| **P(score<30)** | **不能变差** | 低分概率才是真正的风险 |
| 无效动作 / 异常 | 仍然是 0 / 0 | 违规就是 0 分 |
| 单测 | 全绿 | 回归保护 |

**任一条不过 → 不采纳**，把代码留在开关后面（默认关）并记录原因。

---

## 4. 已在用的分组与对手（别再重复造）

### 4.1 分组（GROUPS）

| 名字 | 是什么 |
|---|---|
| `baseline_hybrid` | 原版基线（planner=legacy） |
| `guarded_hybrid` | **封存默认档**（guarded + hybrid + 记忆证伪 + 方案A） |
| `guarded_prev` | 方案A 之前的封存行为（center_pull/econ_guard 都关） |
| `guarded_center_only` / `guarded_econ_only` | 方案A 的两条机制单开（度量各自贡献） |
| `guarded_noidle` | 关掉空闲巡逻 |
| `guarded_cap50/70/100` | 封顶后低血不接敌（已证否，默认关） |
| `sweeper_bot` | 扫图型机器人本身 |

### 4.2 对手（OPPONENTS）

| 名字 | 强度 | 说明 |
|---|---|---|
| `probe_mirror` | **= 我们** | 对等对手，**测期望分与下限只用它** |
| `probe_mid` | 更弱 | 原版基线（注意：不是"我们自己"） |
| `probe_strong` | 更弱 | 收益表内核 |
| `probe_weak` / `probe_farmer` | 明显更弱 | 只打 / 只采集，用来测极端情形 |
| `probe_sweep` | 弱但很烦 | 扫图型，测"被打断时还稳不稳" |

---

## 5. 真机验证（改动影响 IO / AI / 时序时必做）

离线通过 **不等于** 真机可用。真机流程见 `docs/COMPETITION_DAY.md`，最短版本：

```bash
# 1) 裁判（真实模型）
cd ~/rmtc_practice && ./start_referee.sh --stage 2 --ai-mode real
# 2) 三个陪练（新终端）
for s in B C D; do RMTC_TEAM_TOKEN=practice-$s ./start_bot.sh $s "陪练$s" & sleep 2; done
# 3) 自检 + 起程序（新终端）
cd ~/run/rmtc-competition-2026-10-29
python3 -m scripts.preflight
export RMTC_TRACE=/tmp/trace_A.jsonl && python3 -m contestant.bot_node --seat A --stage 2
# 4) 开赛
curl -s -X POST http://127.0.0.1:8080/api/start -d '{"stage":2,"tick_ms":500,"max_ticks":500}'
curl -s -X POST http://127.0.0.1:8080/api/resume -d '{}'
```

**判据**：无效动作 0、异常 0、跑满 500 片、AI 成功次数 > 0（日志里有 `[ai:info] AI 请求参数：thinking=disabled`）。

---

## 6. 常用诊断（不用改代码）

| 想知道 | 命令 |
|---|---|
| 这局它怎么跑的 | `python -m scripts.trace_view /tmp/trace_A.jsonl --summary`（加 `--every 25` 看每帧） |
| 练习分数换算成正式口径 | `python -m scripts.rescore /tmp/state.json` |
| 场地地图能不能用 | `python -m scripts.map_tool check --file map.json` |
| 压力扫描（畸形感知/确定性） | `python -m scripts.stress_sweep --stage1 40 --stage2 16 --events-both --junk 1200` |

---

## 7. 一个完整例子（照抄就能跑）

假设你想试"开局前 30 片多拿普通资源、少拿高价值"：

1. `AgentConfig` 加 `prefer_normal_early=0`（默认 0 = 现状）；
2. 在 `_decide` 的资源选择处加 `if tick < cfg.prefer_normal_early: 降权高价值`；
3. GROUPS 加 `"guarded_pn30"`；
4. 写 3 条单测；
5. 跑 §2.1 的 96 局（`--groups guarded_hybrid,guarded_pn30 --opponents probe_mirror`）；
6. 跑 §2.2 三把尺子；
7. 三条判定（§3）全过 → 改默认值、重新封存、重新打包；否则留开关 + 写进 FUSION_REPORT 的"被否清单"。
