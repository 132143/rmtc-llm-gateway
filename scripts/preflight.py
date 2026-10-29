"""现场自检（开赛前 5 分钟跑一遍，把"能不能打"变成一张可截图的表）。

覆盖六件最容易在现场翻车的事：
  1. 环境变量：RMTC_SEAT / RMTC_TEAM_TOKEN / RMTC_AI_URL（缺就明确报错，不猜默认值）
  2. 网关连通：GET <网关基址>/healthz（不同实现可能没有该端点，失败只告警不算错）
  3. 真实 AI 一次成功调用：用与比赛完全一致的请求体（thinking 关闭 / 512 tokens / 不填 model），
     并打印"哪一级字段组合成功"——这就是 AI 资格的证据
  4. decision_type：必须是注册表里的 6 类或 3 个别名
  5. 封存一致性：git HEAD 与 SEALED_VERSION.txt 里记录的 commit 一致
  6. 令牌安全：只打印末 4 位，绝不打印全文

用法：
    export RMTC_SEAT=A RMTC_TEAM_TOKEN=... RMTC_AI_URL=...
    python -m scripts.preflight
    python -m scripts.preflight --skip-ai     # 不想花额度时跳过真实调用
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contestant.decision import DECISION_TYPES, DEFAULT_DECISION_TYPE  # noqa: E402
from rmtc_client.llm_client import (ClientConfig, LlmGatewayClient,  # noqa: E402
                                   _normalize_chat_url)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK, WARN, BAD = "[OK]", "[WARN]", "[BAD]"


def mask(token: str) -> str:
    token = (token or "").strip()
    if len(token) <= 4:
        return "****"
    return "****" + token[-4:]


def check_env():
    seat = os.environ.get("RMTC_SEAT", "").strip()
    token = os.environ.get("RMTC_TEAM_TOKEN", "").strip()
    url = os.environ.get("RMTC_AI_URL", "").strip()
    rows = [
        (OK if seat in ("A", "B", "C", "D") else BAD,
         "RMTC_SEAT = {0}".format(seat or "(未设置)"), "必须 A/B/C/D"),
        (OK if token else BAD, "RMTC_TEAM_TOKEN = {0}".format(mask(token)),
         "本场令牌，只显示末 4 位"),
        (OK if url else BAD,
         "RMTC_AI_URL = {0}".format(_normalize_chat_url(url) if url else "(未设置)"),
         "网关地址（基址 / 多写 v1 / 完整地址都行，自动补全）"),
    ]
    if os.environ.get("RMTC_MODEL"):
        rows.append((WARN, "RMTC_MODEL = {0}".format(os.environ["RMTC_MODEL"]),
                     "官方要求统一默认模型；bot_node 不读这个变量，请不要设"))
    return rows, token, url


def check_healthz(url: str):
    base = _normalize_chat_url(url).split("/v1/")[0]
    endpoint = base.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(endpoint, timeout=5) as resp:
            return True, "{0} → HTTP {1}".format(endpoint, resp.status)
    except urllib.error.HTTPError as exc:
        return True, "{0} → HTTP {1}".format(endpoint, exc.code)
    except Exception as exc:
        return False, "{0} → {1}（有些实现没有 healthz，可不处理）".format(
            endpoint, type(exc).__name__)


def check_ai(token: str, url: str, timeout_s: float = 8.0):
    cfg = ClientConfig(url=url, token=token, timeout_s=timeout_s, min_interval_s=0.0,
                       max_calls=5, max_tokens=512)
    client = LlmGatewayClient(cfg)
    started = time.perf_counter()
    result = client.ask('请只输出 JSON：{"ok": true}')
    elapsed = (time.perf_counter() - started) * 1000.0
    return result, elapsed, client


def check_decision_type():
    path = os.path.join(ROOT, "decision_config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        return BAD, "读不到 decision_config.json：{0}".format(exc), "必须存在且有 decision_type"
    extra = [key for key in payload if key != "decision_type"]
    value = payload.get("decision_type", "")
    spec = DECISION_TYPES.get(str(value))
    if spec is None:
        resolved = "未登记（会回落到默认 {0}）".format(DEFAULT_DECISION_TYPE)
    else:
        resolved = "planner={0} / combat={1}{2}".format(
            spec.get("planner"), spec.get("combat_mode"),
            "（别名→{0}）".format(spec["alias_of"]) if spec.get("alias_of") else "")
    registered = str(value) in DECISION_TYPES
    return (OK if (not extra and registered) else BAD,
            "decision_type = {0} -> {1}".format(value, resolved),
            "现场唯一允许改的文件；只能有 decision_type 一个键（多余键：{0}）".format(extra or "无"))


def check_sealed():
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception as exc:
        return WARN, "不是 git 仓库或取不到 HEAD：{0}".format(exc), "交付 zip 里没有 .git 时可忽略"
    sealed = ""
    try:
        with open(os.path.join(ROOT, "SEALED_VERSION.txt"), encoding="utf-8") as handle:
            text = handle.read()
        found = re.search(chr(96) + "([0-9a-f]{7,40})" + chr(96), text)
        sealed = found.group(1) if found else ""
    except OSError:
        pass
    same = bool(sealed) and head.startswith(sealed[:12])
    return (OK if same else WARN,
            "HEAD {0} ／ 封存 {1}".format(head[:12] or "?", sealed[:12] or "?"),
            "现场启动的代码必须与检录前提交的一致" + ("" if same else "（若只多了文档提交可接受）"))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-ai", action="store_true", help="跳过真实 AI 调用（不花额度）")
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)

    rows = []
    env_rows, token, url = check_env()
    rows.extend(env_rows)
    if url:
        ok, detail = check_healthz(url)
        rows.append((OK if ok else WARN, detail, "网关存活检查（没有该端点不算错）"))
    if token and url and not args.skip_ai:
        result, elapsed, client = check_ai(token, url, args.timeout)
        stats = client.stats()
        rows.append((OK if result.ok else BAD,
                     "真实调用：{0}，{1:.0f} ms，thinking={2}，max_tokens={3}".format(
                         "成功" if result.ok else "失败（{0}）".format(result.code),
                         elapsed, stats.get("thinking"), stats.get("max_tokens")),
                     "AI 资格证据：本场至少 1 次成功调用（规则 §10.2）"))
        if result.ok:
            rows.append((OK, "模型输出片段：{0}".format((result.text or "")[:60].replace("\n", " ")),
                         "确认返回 JSON 而不是空内容"))
    rows.append(check_decision_type())
    rows.append(check_sealed())

    print("")
    print("现场自检（{0}）".format(time.strftime("%Y-%m-%d %H:%M:%S")))
    print("-" * 92)
    bad = 0
    for status, detail, hint in rows:
        bad += 1 if status == BAD else 0
        print("{0} {1}".format(status, detail))
        print("     +- {0}".format(hint))
    print("-" * 92)
    print("结论：{0}".format("可以上场" if bad == 0 else
                            "有 {0} 项必须修好再上场".format(bad)))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
