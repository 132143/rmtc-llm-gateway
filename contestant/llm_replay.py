"""把"真实模型的一次判断"录下来，离线按同样的 prompt 回放。

为什么要这么做：规则 §8.1/§11 要求事件语义只能由大模型给出。离线评测台要么不跑事件
（第一轮就是），要么就得伪造模型输出（等于自欺）。这里给出第三条路：
**真机录一次 → 之后每一次仿真都用同一份真实输出**，既符合规则，又可复现、零费用。

回放只做"取缓存"，不做任何补全：缓存里没有这条公告，就返回失败，
让 agent 走它自己的降级路径（这与现场网关 5xx 完全同一条代码路径）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from rmtc_client.llm_client import LlmResult


def last_line(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


# 公告块在 prompt 里的结束标记：模板在这之后开始写"输出要求"。
# 只截公告本身，才不会把字段说明也算进指纹（否则缓存永远命中不了）。
BLOCK_END_MARKERS = ("\n\n只输出", "\n只输出", "\n\n字段含义")


def announcement_block(prompt):
    """从 prompt 里取出这一段公告（EventUnderstanding 就是这么拼的）。"""
    text = str(prompt or "")
    marker = "公告："
    index = text.rfind(marker)
    if index < 0:
        return ""
    block = text[index + len(marker):]
    for end in BLOCK_END_MARKERS:
        cut = block.find(end)
        if cut >= 0:
            block = block[:cut]
    return block


def key_for_prompt(prompt):
    """用最后一条公告当键：公告是累加的，最新一条决定了这次判断的内容。"""
    block = announcement_block(prompt)
    return hashlib.sha1(last_line(block).encode("utf-8")).hexdigest()[:16]


def key_for_text(text):
    return hashlib.sha1(last_line(text).encode("utf-8")).hexdigest()[:16]


class ReplayPending:
    __slots__ = ("prompt", "session_id", "tick", "result", "_done")

    def __init__(self, prompt, session_id, tick, result):
        self.prompt = prompt
        self.session_id = session_id
        self.tick = tick
        self.result = result
        self._done = False

    def done(self):
        return self._done

    def poll(self):
        if self._done:
            return None
        self._done = True
        return self.result


class ReplayLlmClient:
    """LlmGatewayClient 的回放替身：同样的 ask_async / ok_calls / healthy 接口。

    缓存缺失时返回 ok=False（code=not_recorded），绝不编造内容。
    """

    def __init__(self, cache=None, cache_path=None, logger=None):
        self._logger = logger
        self._cache = dict(cache or {})
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if key.startswith("_"):
                        continue
                    self._cache[key] = value
        self._asked = 0
        self._hit = 0
        self._miss = 0

    @property
    def healthy(self):
        return True

    @property
    def ok_calls(self):
        return self._hit

    @property
    def attempts(self):
        return self._asked

    def stats(self):
        return {"asked": self._asked, "hit": self._hit, "miss": self._miss,
                "cached": len(self._cache)}

    def ask_async(self, prompt, session_id="", tick=-1, messages=None):
        self._asked += 1
        key = key_for_prompt(prompt)
        text = self._cache.get(key)
        if isinstance(text, str) and text.strip():
            self._hit += 1
            result = LlmResult(ok=True, text=text, status=200, latency_ms=0.0)
        else:
            self._miss += 1
            result = LlmResult(ok=False, code="not_recorded", status=0,
                               message="缓存里没有这条公告的模型输出")
            self._log("warn", "回放缓存未命中（{0}），本次按无提示处理".format(key))
        return ReplayPending(prompt, session_id, tick, result)

    def ask(self, prompt, messages=None, timeout_s=None, max_tokens=None):
        pending = self.ask_async(prompt, tick=0)
        return pending.poll()

    def _log(self, level, text):
        if self._logger is not None:
            try:
                self._logger(level, text)
            except Exception:
                pass


def build_prompt(template_kwargs):
    from rmtc_client.event_understanding import PROMPT_TEMPLATE
    return PROMPT_TEMPLATE.format(**template_kwargs)


def recording_plan(game_map, events_script, seat="A"):
    """给录制脚本用：列出仿真过程中会出现的每一条 prompt。

    agent 只在公告内容变化时发问，所以每个公告点对应一条 prompt。
    """
    plan = []
    for event in events_script.events:
        transcript = events_script.transcript(event.announce)
        prompt = build_prompt({
            "seat": seat, "tick": event.announce, "position": [1, 1],
            "map_width": game_map.width, "map_height": game_map.height,
            "mission": "累计收集至少 2 个普通资源；累计收集至少 1 个高价值资源；"
                       "在比赛结束前至少一次到达 [{0},{1}]".format(*game_map.reach),
            "event": transcript[-800:],
        })
        plan.append((key_for_prompt(prompt), prompt, event.text))
    return plan


def save_cache(path, entries, meta=None):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = dict(entries)
    payload["_meta"] = dict(meta or {})
    payload["_meta"].setdefault("recorded_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    return path