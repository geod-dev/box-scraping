from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable


class MinIntervalRateLimiter:
    """Guarantees a minimum delay between the *starts* of consecutive requests.

    A request that took longer than the interval is followed at once; a faster one waits
    for the rest of it. With ``jitter_seconds`` each gap is drawn afresh as
    ``interval + uniform(-jitter, +jitter)``, clamped at 0. Thread-safe, although the engine
    currently issues requests sequentially.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        jitter_seconds: float = 0.0,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.min_interval = max(0.0, float(min_interval_seconds))
        self.jitter = max(0.0, float(jitter_seconds))
        self._rng = rng or random.Random()
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_start: float | None = None

    def _next_interval(self) -> float:
        if not self.jitter:
            return self.min_interval
        return max(0.0, self.min_interval + self._rng.uniform(-self.jitter, self.jitter))

    def wait(self) -> float:
        """Block until a request may start; returns the time slept."""
        with self._lock:
            now = self._clock()
            slept = 0.0
            if self._last_start is not None:
                remaining = self._last_start + self._next_interval() - now
                if remaining > 0:
                    self._sleep(remaining)
                    slept = remaining
                    now = self._clock()
            self._last_start = now
            return slept
