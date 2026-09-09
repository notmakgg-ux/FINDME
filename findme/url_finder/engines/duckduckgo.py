"""DuckDuckGo search engine adapter."""

import time
import random
from typing import Any

from ddgs import DDGS

from engines.base import BaseEngine


class DuckDuckGoEngine(BaseEngine):
    """Search via DuckDuckGo using the ddgs package."""

    name = "duckduckgo"

    def __init__(self, request_delay: float = 2.0):
        self._delay = request_delay

    def search(self, query: str, max_results: int = 25) -> list[dict[str, Any]]:
        ddgs = DDGS()
        for attempt in range(3):
            try:
                results = ddgs.text(query, max_results=max_results, region="us-en")
                return [
                    {
                        "url": r.get("href", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("body", "")[:200],
                    }
                    for r in results
                    if r.get("href")
                ]
            except Exception as e:
                if attempt < 2:
                    # Wider backoff for DuckDuckGo rate limiting + respect configured delay
                    time.sleep((attempt + 1) * max(3, self._delay) + random.uniform(1, 4))
                else:
                    print(f"    DuckDuckGo failed after 3 attempts: {e}")
                    return []
