import asyncio
from collections import deque
from functools import lru_cache
from time import monotonic
from typing import Annotated

from fastapi import Depends

from app.core.config import Settings, get_settings


class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def retry_after(self, key: str) -> int | None:
        async with self._lock:
            failures = self._active_failures(key)
            if failures is None or len(failures) < self.max_attempts:
                return None
            return max(1, int(self.window_seconds - (monotonic() - failures[0])))

    async def record_failure(self, key: str) -> None:
        async with self._lock:
            failures = self._active_failures(key)
            if failures is None:
                failures = deque()
                self._failures[key] = failures
            failures.append(monotonic())
            if len(self._failures) > 10_000:
                self._prune_all()

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._failures.pop(key, None)

    def _active_failures(self, key: str) -> deque[float] | None:
        failures = self._failures.get(key)
        if failures is None:
            return None
        cutoff = monotonic() - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)
            return None
        return failures

    def _prune_all(self) -> None:
        cutoff = monotonic() - self.window_seconds
        for key in list(self._failures):
            failures = self._failures[key]
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                del self._failures[key]
        if len(self._failures) > 10_000:
            self._failures.clear()


@lru_cache
def build_login_rate_limiter(
    max_attempts: int, window_seconds: int
) -> LoginRateLimiter:
    return LoginRateLimiter(max_attempts, window_seconds)


def get_login_rate_limiter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginRateLimiter:
    return build_login_rate_limiter(
        settings.login_max_attempts, settings.login_window_seconds
    )
