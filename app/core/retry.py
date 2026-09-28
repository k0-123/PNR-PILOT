"""Exponential backoff helpers."""
from __future__ import annotations

import random


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Delay before retry number `attempt` (1-based): base * 2^(attempt-1), capped, +/-20% jitter."""
    delay = min(cap, base * (2 ** (attempt - 1)))
    return delay * random.uniform(0.8, 1.2)
