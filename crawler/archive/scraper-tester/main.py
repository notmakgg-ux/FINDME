#!/usr/bin/env python3
"""
Real Estate Company Scraper
============================
Scrapes search engines to find real estate / realtor / property dealer
company websites for a given US location.

HOW IT WORKS
------------
1. Open config/config.yaml
2. Set LOCATION to any US city/state
3. Run:  python main.py
4. The scraper searches DuckDuckGo for real estate companies in that
   location and extracts their website URLs.
5. Results are saved to output/latest_websites.csv and .txt

CHANGE LOCATION
---------------
Edit config/config.yaml -> set LOCATION: "Los Angeles, CA"
Or override from CLI:    python main.py --location "Miami, FL"

FLOW
----
  config.yaml (LOCATION)
       |
       v
  Build search queries
  ("real estate company New York, NY", etc.)
       |
       v
  For each query:
       |
       +-> DuckDuckGo search (25 results per query)
       +-> Filter out junk domains (YouTube, Facebook, etc.)
       +-> Score each result by relevance
       +-> Keep only score >= 1
       +-> Deduplicate by domain
       |
       v
  Save to output/
    +-- latest_websites.csv   (full metadata)
    +-- latest_websites.txt   (URLs only)
    +-- latest_websites.json  (structured data)
"""

from src.cli import main

if __name__ == "__main__":
    main()
