"""把"随机事件公告"变成可用提示的一个小盒子。

协议要求（§3「公开事件」）：
 - 事件正文必须交给大语言模型理解，不许用正则/关键词/固定模板顶替；
 - 模型输出必须由参赛程序自己校验，再执行自主决策；
 - 结果必须核对所属场次与当前时效，过期或跨场次的结果不能直接用。

这个类只负责"发问、收答案、判时效"，不替你决定动作。
"""

from __future__ import annotations

import time

from .json_extract import extract_first_json_object

PROMPT_TEMPLATE = """你是机器人比赛的决策助手。请只依据下面这条公告做理解，并用严格 JSON 回答。

当前状态：席位 {seat}，Tick {tick}，本车位置 {position}，地图 {map_width}x{map_height}。
固定任务：{mission}
随机事件公告：{event}

要求：
1. 只输出一个 JSON 对象，不要代码块，不要多余解释；
2. 字段：threat_zone（受影响的矩形 [[x1,y1],[x2,y2]] 或 null）、
   affects_me（布尔）、advice（一句话中文应对建议）、urgency_ticks（整数或 null）；
3. 公告没提到的影响，按 null / false 处理，不要编造。
"""


class EventUnderstanding:
    def __init__(self, client, max_age_ticks: int = 20, min_repeat_s: float = 5.0,
                 logger=None):
        self.client = client
        self.max_age_ticks = max_age_ticks
        self.min_repeat_s = min_repeat_s
        self._logger = logger
        self._session_id = ""
        self._pending: list = []
        self._asked: dict = {}
        self._hint: dict | None = None
        self.offered = 0
        self.finished = 0

    # ---------- 注入一条公告 ----------
    def offer(self, session_id: str, tick: int, event_text: str, mission_text: str = "",
              position=None, map_size=None, seat: str = "") -> bool:
        event_text = (event_text or "").strip()
        if not event_text:
            return False
        if session_id != self._session_id:
            # 换场次：清空上一场的提问记录、在途任务和旧提示（协议明确要求）
            self._session_id = session_id
            self._asked.clear()
            self._pending.clear()
            self._hint = None
        if not self.client.healthy:
            return False
        key = (session_id, hash(event_text))
        now = time.monotonic()
        last = self._asked.get(key)
        if last is not None and now - last < self.min_repeat_s:
            return False
        map_size = map_size or (0, 0)
        prompt = PROMPT_TEMPLATE.format(
            seat=seat or "?", tick=tick, position=position or (0, 0),
            map_width=map_size[0], map_height=map_size[1],
            mission=mission_text or "（无）", event=event_text)
        self._asked[key] = now
        self._pending.append(self.client.ask_async(prompt, session_id=session_id, tick=tick))
        self._pending = self._pending[-4:]
        self.offered += 1
        return True

    # ---------- 取当前可用提示 ----------
    def hint(self, session_id: str, tick: int) -> dict | None:
        self._pump()
        hint = self._hint
        if not hint:
            return None
        if hint["session_id"] != session_id:
            return None
        if tick - hint["tick"] > self.max_age_ticks:
            return None
        return hint

    def _pump(self) -> None:
        for pending in list(self._pending):
            result = pending.poll()
            if result is None:
                continue
            self._pending.remove(pending)
            self.finished += 1
            if not result.ok:
                self._log("warn", f"事件理解未拿到结果（{result.code}），继续本地策略")
                continue
            parsed = extract_first_json_object(result.text)
            advice = ""
            if isinstance(parsed, dict):
                advice = str(parsed.get("advice") or "")
            if not advice:
                advice = result.text.strip()[:200]
                parsed = parsed if isinstance(parsed, dict) else None
            self._hint = {"session_id": pending.session_id, "tick": pending.tick,
                          "parsed": parsed, "advice": advice, "raw": result.text}
            self._log("info", f"事件理解结果（Tick {pending.tick}）：{advice[:120]}")

    def stats(self) -> dict:
        return {"offered": self.offered, "finished": self.finished,
                "pending": len(self._pending), "client": self.client.stats()}

    def _log(self, level: str, text: str) -> None:
        if self._logger is not None:
            try:
                self._logger(level, text)
            except Exception:
                pass
