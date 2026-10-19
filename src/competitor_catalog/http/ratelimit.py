from __future__ import annotations

import threading
import time
from collections.abc import Callable


class MinIntervalRateLimiter:
    """Guarantees a minimum delay between the *starts* of consecutive requests.

    Used to honour a site's crawl-delay (Antalis' robots.txt asks for 10s). Thread-safe,
    although the engine currently issues requests sequentially.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.min_interval = max(0.0, float(min_interval_seconds))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_start: float | None = None

    def wait(self) -> float:
        """Block until a request may start; returns the time slept."""
        with self._lock:
            now = self._clock()
            slept = 0.0
            if self._last_start is not None:
                remaining = self._last_start + self.min_interval - now
                if remaining > 0:
                    self._sleep(remaining)
                    slept = remaining
                    now = self._clock()
            self._last_start = now
            return slept
