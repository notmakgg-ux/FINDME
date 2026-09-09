"""Single-engine scraper: searches DuckDuckGo for real estate company websites."""

import time
import random
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from scraper_config import Config
from engines.duckduckgo import DuckDuckGoEngine


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _get_domain(url: str) -> str:
    try:
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def _is_junk_domain(url: str, exclude_domains: list[str]) -> bool:
    domain = _get_domain(url)
    junk = [
        "youtube.com", "facebook.com", "twitter.com", "x.com",
        "instagram.com", "linkedin.com", "pinterest.com", "tiktok.com",
        "wikipedia.org", "reddit.com", "quora.com",
        "google.com", "bing.com", "duckduckgo.com", "yahoo.com",
        "apple.com", "microsoft.com", "amazon.com",
        "craigslist.org", "indeed.com", "glassdoor.com",
        "mapquest.com",
    ]
    all_excluded = set(junk) | set(exclude_domains)
    return any(d in domain for d in all_excluded)


def _score_result(url: str, title: str, snippet: str, location: str) -> int:
    combined = (title + " " + snippet).lower()
    domain = _get_domain(url)
    score = 0

    # Positive: real estate keywords
    for term in ["real estate", "realtor", "property", "brokerage", "broker",
                 "realty", "properties", "homes", "apartments", "rental",
                 "property management", "home buyers", "investors"]:
        if term in combined or term in domain:
            score += 2

    # Positive: location
    for part in location.lower().replace(",", "").split():
        if len(part) > 2 and part in combined:
            score += 1

    # Positive: company indicators
    for term in ["llc", "inc", "group", "partners", "associates",
                 "company", "corp", "services", "solutions"]:
        if term in combined:
            score += 1

    # Positive: homepage
    path_parts = [p for p in urlparse(url).path.split("/") if p]
    if len(path_parts) <= 1:
        score += 3
    elif len(path_parts) <= 2:
        score += 1

    # Negative: directories/aggregators
    for term in ["top 10", "top 20", "top 50", "top 100", "best real estate",
                 "list of", "directory", "reviews of", "compare",
                 "find a realtor", "agent finder", "top-rated",
                 "most powerful", "best commercial"]:
        if term in combined:
            score -= 3

    agg_domains = ["goodfirms.co", "f6s.com", "clutch.co", "sortlist.com",
                   "expertise.com", "bark.com", "thumbtack.com", "homeadvisor.com",
                   "angi.com", "porch.com", "fixr.com", "inven.ai", "retyn.ai",
                   "proptechbuzz.com", "smartguy.com", "bestinhood.com",
                   "propertymanagementlist.com", "belonghome.com",
                   "builtinnyc.com", "easyleadz.com", "realtrends.com",
                   "houzeo.com", "homelight.com", "fastexpert.com",
                   "themanifest.com", "realestatebees.com", "accio.com",
                   "allpropertymanagement.com", "managemyproperty.com",
                   "realestate.usnews.com", "findrealestate.com",
                   "propertymanagement.com", "smartlocalmove.com",
                   "movoto.com", "homezero.com", "webuycash.com",
                   "carrot.com", "placester.com", "ziprealty.com",
                   "sulekha.com", "justdial.com"]
    if any(d in domain for d in agg_domains):
        score -= 6

    # Negative: blog/list/news pages
    if "/blog/" in url or "/lists/" in url or "/ranking/" in url:
        score -= 4

    # Negative: government
    if ".gov" in domain:
        score -= 10

    # Negative: job/recruiting sites
    for term in ["jobs", "careers", "hiring", "salary", "resume"]:
        if term in combined:
            score -= 5

    return score


# ---------------------------------------------------------------------------
# Engine registry — DuckDuckGo only
# ---------------------------------------------------------------------------

ENGINE_MAP = {
    "duckduckgo": DuckDuckGoEngine,
}


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

# Extra query templates used in retry rounds to find more unique results.
_RETRY_QUERY_TEMPLATES = [
    # Round 2 — area/suburb/neighborhood variants
    [
        "real estate agents near {location}",
        "property management near {location}",
        "realtor office {location}",
        "realty office {location}",
        "real estate company in {location}",
        "property company in {location}",
        "homes for sale {location}",
        "real estate services {location}",
        "house selling agency {location}",
        "rental agency {location}",
        "commercial property agents {location}",
        "residential property agency {location}",
        "estate agency {location}",
        "property consultants {location}",
        "real estate investing {location}",
        "landlord services {location}",
        "property development {location}",
        "real estate valuation {location}",
        "property appraisal {location}",
        "home staging company {location}",
    ],
    # Round 3 — long-tail / niche / alternative terms
    [
        "independent real estate agency {location}",
        "boutique real estate {location}",
        "local realtor {location}",
        "property sales {location}",
        "house buying company {location}",
        "cash home buyers {location}",
        "real estate brokerage firm {location}",
        "real estate office {location}",
        "new home builder {location}",
        "custom home builder {location}",
        "home inspection {location}",
        "home renovation {location}",
        "kitchen remodeling {location}",
        "general contractor {location}",
    ],
]


class RealEstateScraper:
    """Playwright-based scraper for real estate company websites."""

    def __init__(self, config: Config, on_new_result=None, control=None):
        self.config = config
        self.found_urls: dict[str, dict[str, Any]] = {}
        self.seen_domains: set[str] = set()           # FIX #1: O(1) domain dedup
        self.engine_stats: dict[str, int] = {}
        self.on_new_result = on_new_result
        self.control = control

    def _init_engines(self) -> list:
        engines = []
        for name in self.config.search_engines:
            cls = ENGINE_MAP.get(name)
            if cls:
                engines.append(cls(request_delay=self.config.request_delay))
        if not engines:
            engines = [DuckDuckGoEngine(request_delay=self.config.request_delay)]
        return engines

    def _run_queries(self, queries: list[str], engines: list, max_per: int) -> int:
        """Run a set of queries across engines. Returns number of new unique URLs found."""
        total_runs = len(queries) * len(engines)
        run_num = 0
        new_count = 0

        for engine in engines:
            print(f"\n  --- Engine: {engine.name.upper()} ---\n")

            for i, query in enumerate(queries, 1):
                # Cooperative stop / pause check
                if self.control:
                    if not self.control.check():
                        print("  [URL Finder: Stop requested, exiting query loop]")
                        return new_count
                    if not self.control.wait_sync_if_paused():
                        print("  [URL Finder: Stop requested while paused, exiting]")
                        return new_count

                run_num += 1
                short_query = query[:50]
                print(f"  [{run_num}/{total_runs}] {short_query}")

                try:
                    results = engine.search(query, max_results=max_per)
                    count = 0
                    skipped = 0

                    for r in results:
                        if self.control and not self.control.check():
                            return new_count

                        url = r.get("url", "")
                        if not url:
                            continue

                        domain = _get_domain(url)
                        if not domain or _is_junk_domain(url, self.config.exclude_domains):
                            skipped += 1
                            continue

                        score = _score_result(
                            url, r.get("title", ""), r.get("snippet", ""), self.config.location
                        )
                        if score < self.config.min_score:
                            skipped += 1
                            continue

                        # FIX #1: O(1) domain dedup using seen_domains set
                        if domain in self.seen_domains:
                            continue

                        if url not in self.found_urls:
                            self.found_urls[url] = {
                                "website": url,
                                "domain": domain,
                                "title": r.get("title", ""),
                                "snippet": r.get("snippet", "")[:200],
                                "source_query": query,
                                "source_engine": engine.name,
                                "location": self.config.location,
                                "score": score,
                                "found_at": datetime.now().isoformat(),
                            }
                            self.seen_domains.add(domain)   # track domain
                            count += 1
                            new_count += 1

                            # Real-time callback — save each new URL to DB immediately
                            if self.on_new_result:
                                try:
                                    self.on_new_result(self.found_urls[url])
                                except Exception as e:
                                    print(f"           [realtime save error: {e}]")

                    self.engine_stats[engine.name] = (
                        self.engine_stats.get(engine.name, 0) + count
                    )
                    print(f"           -> +{count} new ({len(self.found_urls)} total) | skipped: {skipped}")

                except Exception as e:
                    print(f"           -> Error: {e}")

                # Delay between queries (cooperative sleep)
                # DuckDuckGo rate-limits aggressively, so use a wider jitter band.
                if i < len(queries) or engine != engines[-1]:
                    delay = self.config.request_delay + random.uniform(2, 5)
                    slept = 0.0
                    while slept < delay:
                        if self.control:
                            if not self.control.check():
                                return new_count
                            if not self.control.wait_sync_if_paused():
                                return new_count
                        step = min(0.2, delay - slept)
                        time.sleep(step)
                        slept += step

        return new_count

    def run(self) -> list[dict[str, Any]]:  # FIX #2: sync, not async
        """Run the scraper synchronously and return found URLs."""
        queries = self.config.search_queries
        engines = self._init_engines()
        max_per = self.config.max_results_per_query
        target = self.config.min_unique_results
        max_retries = self.config.max_retries

        print(f"\n{'=' * 60}")
        print(f"  REAL ESTATE COMPANY SCRAPER  (DuckDuckGo)")
        print(f"{'=' * 60}")
        print(f"  Location:       {self.config.location}")
        print(f"  Queries:        {len(queries)}")
        print(f"  Engines:        {', '.join(e.name for e in engines)}")
        print(f"  Target:         {target}+ unique company websites")
        print(f"  Max retries:    {max_retries}")
        print(f"{'=' * 60}\n")

        # === Round 1: primary queries ===   FIX #10: store return value
        print(f"\n  === ROUND 1: Primary queries ({len(queries)} queries) ===")
        round1_count = self._run_queries(queries, engines, max_per)
        print(f"\n  Round 1 complete: {len(self.found_urls)} unique URLs found (+{round1_count} new)")

        # === Retry rounds if under target ===
        for attempt in range(max_retries):
            if self.control and not self.control.check():
                print("  [URL Finder: Stop requested, skipping retry rounds]")
                break
            if self.control and not self.control.wait_sync_if_paused():
                break

            if len(self.found_urls) >= target:
                break

            remaining = target - len(self.found_urls)
            print(f"\n  === ROUND {attempt + 2}: Need {remaining} more (have {len(self.found_urls)}/{target}) ===")

            if attempt < len(_RETRY_QUERY_TEMPLATES):
                retry_queries = [
                    q.replace("{location}", self.config.location)
                    for q in _RETRY_QUERY_TEMPLATES[attempt]
                ]
            else:
                retry_queries = self._generate_variant_queries(queries)

            print(f"  Running {len(retry_queries)} retry queries...")
            new = self._run_queries(retry_queries, engines, max_per)
            print(f"\n  Round {attempt + 2} complete: +{new} new | Total: {len(self.found_urls)} unique URLs")

            if new == 0:
                print("  No new results found. Stopping retries.")
                break

            if self.control and not self.control.check():
                break
            time.sleep(1)

        results = list(self.found_urls.values())
        results.sort(key=lambda x: x.get("score", 0), reverse=True)

        print(f"\n{'=' * 60}")
        print(f"  DONE -- {len(results)} unique company websites found")
        if len(results) < target:
            print(f"  Note: Below target of {target} — search exhausted for this location")
        print(f"  Engine breakdown:")
        for eng, cnt in self.engine_stats.items():
            print(f"    {eng}: {cnt} URLs")
        print(f"{'=' * 60}\n")

        return results

    def _generate_variant_queries(self, base_queries: list[str]) -> list[str]:
        """Generate variant queries by shuffling and recombining base queries."""
        location = self.config.location
        variants = []
        parts = [p.strip() for p in location.replace(".", "").split(",")]
        city = parts[0] if parts else location

        suffixes = ["area", "metro", "county", "region", "district", "suburbs"]
        for suffix in suffixes:
            variants.append(f"real estate company {city} {suffix}")
            variants.append(f"property management {city} {suffix}")
            variants.append(f"realtor {city} {suffix}")

        shuffled = base_queries[:]
        random.shuffle(shuffled)
        variants.extend(shuffled[:10])

        return variants[:25]
