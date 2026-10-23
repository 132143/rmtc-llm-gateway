"""限额账本：额度、在途并发、来源 IP 频率。全部对应协议 §5「请求频率与配额」。

协议要点（实现必须一致）：
- 额度按令牌全生命周期累计，不按时间窗重置；只有成功的 2xx 转发才消耗；
- 在途调用占名额但不计成功次数，失败立即释放；
- 每个来源 IP 每 10 秒最多 100 次 POST；每个席位令牌最多 2 个在途调用。
"""

from __future__ import annotations

import threading
import time
from collections import deque


class TokenState:
    """一个席位令牌的账本。quota=0 表示不限额。"""

    def __init__(self, seat: str, quota: int):
        self.seat = seat
        self.quota = quota
        self.used = 0        # 成功的 2xx 调用次数（消耗额度）
        self.pending = 0     # 已受理、结果未知的调用（预留额度，防止并发超发）
        self.inflight = 0    # 正在转发给上游的调用
        self._lock = threading.Lock()

    @property
    def remaining(self):
        """剩余额度；不限额时返回 None。"""
        with self._lock:
            if self.quota == 0:
                return None
            return max(0, self.quota - self.used - self.pending)

    def reserve_quota(self) -> bool:
        """受理前先占一个额度位；失败（已用完）返回 False。"""
        with self._lock:
            if self.quota != 0 and self.used + self.pending >= self.quota:
                return False
            self.pending += 1
            return True

    def commit_quota(self) -> None:
        """上游 2xx 后调用：这一次算成功，正式扣额度。"""
        with self._lock:
            self.pending = max(0, self.pending - 1)
            self.used += 1

    def release_quota(self) -> None:
        """失败/被拒后调用：把预留位还回去，不扣额度。"""
        with self._lock:
            self.pending = max(0, self.pending - 1)

    def acquire_slot(self, limit: int) -> bool:
        with self._lock:
            if limit > 0 and self.inflight >= limit:
                return False
            self.inflight += 1
            return True

    def release_slot(self) -> None:
        with self._lock:
            self.inflight = max(0, self.inflight - 1)

    def snapshot(self) -> dict:
        with self._lock:
            return {"seat": self.seat, "quota": self.quota, "used": self.used,
                    "pending": self.pending, "inflight": self.inflight}


class RateLimiter:
    """滑动窗口频率限制：每个 key（这里用来源 IP）每 window 秒最多 limit 次。"""

    def __init__(self, limit: int, window_s: float):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic() if now is None else now
        cutoff = now - self.window_s
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 4096:
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True
