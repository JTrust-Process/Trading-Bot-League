# crypto_bot/utils/retry.py
#
# Fix from audit:
#   - Issue 13: last_exc could be None if max_attempts <= 0 — now handled

import time
import functools


def retry(max_attempts: int = 3, delay: float = 2.0):
    """
    Decorator that retries a function on exception.

    Usage:
        @retry(max_attempts=3, delay=2)
        def flaky_api_call(): ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_attempts:
                        print(
                            f"[retry] {func.__name__} attempt {attempt}/{max_attempts} "
                            f"failed: {e} — retrying in {delay}s"
                        )
                        time.sleep(delay)
            # Issue 13: guard against max_attempts <= 0
            if last_exc is None:
                raise RuntimeError(f"retry: {func.__name__} called with max_attempts={max_attempts}")
            raise last_exc
        return wrapper
    return decorator