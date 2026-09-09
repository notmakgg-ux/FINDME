"""DuckDuckGo search engine adapter."""

import time
import random
from typing import Any

from ddgs import DDGS

from src.engines.base import BaseEngine


class DuckDuckGoEngine(BaseEngine):
    """Search via DuckDuckGo using the ddgs package."""

    name = "duckduckgo"

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
                    time.sleep((attempt + 1) * 3 + random.uniform(0, 2))
                else:
                    print(f"    DuckDuckGo failed after 3 attempts: {e}")
                    return []
