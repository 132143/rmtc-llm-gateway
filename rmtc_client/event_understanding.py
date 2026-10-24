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

PROMPT_TEMPLATE = """你是机器人比赛的决策助手。只依据下面的公告做理解，用严格 JSON 回答。

当前：席位 {seat}，Tick {tick}，本车位置 {position}，地图 {map_width}x{map_height}。
固定任务：{mission}
公告：{event}

只输出一个 JSON 对象（不要代码块、不要解释），字段含义：
- avoid_cells: **接下来必须避开的格子**，最多 60 个，形如 [[x,y],...]；没有就 []。
  整行/整列/十字形的危险，请把那条线上的格子逐个列出来（地图 18x18，x/y 都会在 0..17）；
- avoid_until_tick: 上面这些格子危险到哪个 Tick 结束（整数），不确定就给公告里的结束 Tick，没有就 null
- danger_level: 这些格子的危险程度，只能是 "fatal"（直接死亡/世界分裂）、"damage"（掉血）或 "none"
- threat_zone: 危险区域的矩形 [[x1,y1],[x2,y2]]（能给就给），没有就 null
- heal_zone: 能回血的矩形或点位 [[x1,y1],[x2,y2]]，没有就 null
- buff_zone: 能给我增益的矩形 [[x1,y1],[x2,y2]]（例如"天下无双"堡垒、需要停留/进入的点位），没有就 null
- buff_until_tick: 这个增益区域存在到哪个 Tick（整数），没有就 null
- direction_reversed: 我的移动方向会被上下左右颠倒吗（布尔）
- reversed_until_tick: 反向效果结束的 Tick（整数），没有就 null
- affects_me: 这件事是否会影响我（布尔）
- target_kind: 接下来这一小段时间的高层意图，**只能**取下列之一：
  "resource"（优先去捡资源）、"mission"（优先去任务点）、
  "engage"（主动交战）、"flee"（脱离接触、别开战）、"none"（没有偏好）
- target_confidence: 你对 target_kind 的置信度，0 到 1 的小数
- advice: 一句话中文建议
公告没提到的一律 null / false / []，不要编造。坐标必须是整数。
target_kind 必须是上面五个词之一，写别的词一律视为无效。
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
              position=None, map_size=None, seat: str = "", event_key: str | None = None) -> bool:
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
        key = (session_id, event_key or hash(event_text))
        now = time.monotonic()
        last = self._asked.get(key)
        if last is not None and now - last < self.min_repeat_s:
            return False
        map_size = map_size or (0, 0)
        prompt = PROMPT_TEMPLATE.format(
            seat=seat or "?", tick=tick, position=position or (0, 0),
            map_width=map_size[0], map_height=map_size[1],
            mission=mission_text or "（无）",
            # 公告是累加的（每 Tick 的 event_text 会重复近期内容）。只送最近一段，
            # 既省 token 也降延迟；更早的信息已经不影响当前决策。
            event=event_text[-800:])
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
