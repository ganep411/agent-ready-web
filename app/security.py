"""Shared authentication, bounded process-local rate limiting and redacted audit events."""
import hmac
import logging
import threading
import time
from collections import OrderedDict

from fastapi import HTTPException

logger = logging.getLogger("agent_ready.audit")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()  # stderr; stdout belongs to the MCP protocol.
    handler.addFilter(lambda record: not logging.getLogger().handlers)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def audit(operation, outcome):
    # Only server-defined labels; never include arguments, headers, tokens, or errors.
    logger.info("operation=%s outcome=%s", operation, outcome)


class Guard:
    def __init__(self, api_key=None, requests_per_minute=60, clock=time.monotonic):
        if api_key is not None and (len(api_key) < 32 or not api_key.isascii() or any(c.isspace() for c in api_key)):
            raise ValueError("AGENT_API_KEY must contain at least 32 non-whitespace ASCII characters")
        if requests_per_minute < 1:
            raise ValueError("Rate limit must be positive")
        self._api_key = api_key
        self.limit, self.clock = requests_per_minute, clock
        self._buckets = OrderedDict()
        self._lock = threading.Lock()

    def check(self, credential, identity):
        # Compare bytes: malformed Unicode headers must not produce server errors.
        if self._api_key and not hmac.compare_digest((credential or "").encode(), self._api_key.encode()):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
        now = self.clock()
        with self._lock:
            start, count = self._buckets.get(identity, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            if count >= self.limit:
                raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": str(max(1, int(60 - (now - start))))})
            self._buckets[identity] = (start, count + 1)
            self._buckets.move_to_end(identity)
            if len(self._buckets) > 1024:
                self._buckets.popitem(last=False)
