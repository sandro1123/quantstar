"""统一重试策略"""

import random
import time
from dataclasses import dataclass


@dataclass
class RetryPolicy:
    max_retries: int = 3
    backoff_base: float = 2.0
    backoff_max: float = 30.0
    jitter: float = 0.1
    retryable_exceptions: tuple = (ConnectionError, TimeoutError)


RETRY_DEFAULTS = {
    "data_source": RetryPolicy(max_retries=3, backoff_base=2.0),
    "llm_call":    RetryPolicy(max_retries=2, backoff_base=1.0),
    "delivery":    RetryPolicy(max_retries=3, backoff_base=5.0),
    "pipeline":    RetryPolicy(max_retries=2, backoff_base=30.0),
}


def retry_with_backoff(fn, policy: RetryPolicy, *args, **kwargs):
    last_exception = None
    for attempt in range(policy.max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exception = e
            if not isinstance(e, policy.retryable_exceptions):
                raise
            if attempt < policy.max_retries - 1:
                sleep_time = min(
                    policy.backoff_base ** attempt,
                    policy.backoff_max,
                )
                jitter_amount = sleep_time * policy.jitter * random.random()
                time.sleep(sleep_time + jitter_amount)
    raise last_exception
