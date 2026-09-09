"""Base search engine interface."""

from abc import ABC, abstractmethod
from typing import Any


class BaseEngine(ABC):
    """Base class for all search engine adapters."""

    name: str = "base"

    @abstractmethod
    def search(self, query: str, max_results: int = 25) -> list[dict[str, Any]]:
        """
        Search for a query and return results.
        
        Each result must have:
            - url: str
            - title: str
            - snippet: str
        """
        pass
