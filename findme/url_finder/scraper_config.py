"""Configuration loader."""

from pathlib import Path
from typing import Any

import yaml


class Config:
    """Loads and provides access to config.yaml."""

    def __init__(self, config_path: str | Path | None = None):
        if config_path is None:
            config_path = Path(__file__).parent.parent / "config" / "config.yaml"
        self.config_path = Path(config_path)
        self._config = self._load()

    def _load(self) -> dict[str, Any]:
        if self.config_path.exists():
            with open(self.config_path, "r") as f:
                return yaml.safe_load(f) or {}
        return {}

    @property
    def location(self) -> str:
        return self._config.get("LOCATION", "Atlanta, Georgia")

    @property
    def search_queries(self) -> list[str]:
        # Queries are pre-substituted by url_finder_runner before being stored
        # in the config YAML, so return them as-is (no further replacement).
        return self._config.get("SEARCH_QUERIES", [])


    @property
    def search_engines(self) -> list[str]:
        return self._config.get("SEARCH_ENGINES", ["duckduckgo"])

    @property
    def max_results_per_query(self) -> int:
        return self._config.get("MAX_RESULTS_PER_QUERY", 25)

    @property
    def request_delay(self) -> float:
        return self._config.get("REQUEST_DELAY", 2)

    @property
    def min_score(self) -> int:
        return self._config.get("MIN_SCORE", 1)

    @property
    def exclude_domains(self) -> list[str]:
        return self._config.get("EXCLUDE_DOMAINS", [])

    @property
    def min_unique_results(self) -> int:
        return self._config.get("MIN_UNIQUE_RESULTS", 500)

    @property
    def max_retries(self) -> int:
        return self._config.get("MAX_RETRIES", 3)


_config: Config | None = None


def get_config(config_path: str | Path | None = None) -> Config:
    global _config
    if _config is None:
        _config = Config(config_path)
    return _config
