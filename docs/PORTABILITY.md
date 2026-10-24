# 移植性说明（Windows / Ubuntu 双系统）

这份代码定位于"**队友的电脑上原样能跑**"，所以刻意做到：

## 1. 没有任何写死的绝对路径

- 代码里所有路径都从 `__file__` 推导（`REPO_ROOT`、`ROOT`），或者由命令行/环境变量给出；
- `decision_config.json` 的查找顺序：**命令行 `--decision-config` → 当前目录 → 仓库根目录**，
  所以不管从哪个目录启动都能找到；
- 仓库里出现过的绝对路径只有"文档里的示例"，不影响运行。

自查命令（在任何系统都能跑）：

```bash
python - <<'EOF'
import os, re
bad = re.compile(r"[A-Za-z]:\\\\|/home/|/Users/")
for base, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
    for name in files:
        if name.endswith((".py", ".sh", ".json")):
            path = os.path.join(base, name)
            for i, line in enumerate(open(path, encoding="utf-8", errors="replace"), 1):
                if bad.search(line) and "http" not in line:
                    print(path, i, line.strip()[:80])
EOF
```

## 2. 依赖只有标准库

`contestant/`、`rmtc_client/`、`rmtc_gateway/` 只 import 标准库；
唯一的例外是 `contestant/bot_node.py` 需要 `rclpy`，而它**只在 Ubuntu + ROS 2 Humble 上存在**：

| 用途 | Windows | Ubuntu（有 ROS 2） |
|---|---|---|
| 跑测试 `python run_tests.py` | ✅ | ✅ |
| 起本地网关 `python -m rmtc_gateway` | ✅ | ✅ |
| 不用 ROS 的演示 `python examples/llm_demo.py` | ✅ | ✅ |
| 参赛节点 `python -m contestant.bot_node` | ❌（会打印提示并退出，不是堆栈） | ✅ |

## 3. 换行与编码

- `.gitattributes` 锁死 `.sh`/`.py` 为 LF：Git 在 Windows 上默认 `core.autocrlf=true`，
  否则脚本进 Linux 会报 `$'\r': command not found`；
- 所有文件读写都显式 `encoding="utf-8"`；
- `run_tests.py` 和网关入口会把 stdout/stderr 切到 UTF-8：Windows 控制台默认 GBK，
  打印中文测试说明会直接抛 `UnicodeEncodeError`。

## 4. Python 版本

需要 **Python 3.10+**（用了 `str | None` 这种类型写法）。
Ubuntu 22.04 自带 3.10，Windows 装 3.10~3.12 都行。

## 5. 环境变量与命令行是唯一配置入口

不靠"文件固定放在哪"，而是：

```bash
# Ubuntu（参赛）
export RMTC_SEAT=A
export RMTC_TEAM_TOKEN=<本场令牌>
export RMTC_AI_URL=<现场网关>/v1/chat/completions
python -m contestant.bot_node --seat A --stage 2

# Windows（本地调试网关）
set RMTC_GW_PORT=8000
python -m rmtc_gateway --port 8000
```

## 6. 合并（和队友项目对齐）时的注意事项

- 不要引入需要联网安装的第三方包（比赛机可能离线）；
- 不要把路径、令牌、场次 ID 写进代码；
- 改完先在**两个系统**都跑一遍 `python run_tests.py`（当前 102 个测试）；
- 动作/感知的接口不要动（协议级），要改就改策略与决策；
- 详细合并规范见 [HANDOVER_FOR_TEAMMATE.md](HANDOVER_FOR_TEAMMATE.md) 与 [COMPETITION_PLAN.md](COMPETITION_PLAN.md)。
