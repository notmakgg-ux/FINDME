# Real Estate Company Scraper

Scrapes search engines to find real estate / realtor / property dealer company websites for any US location.

## How It Works

```
config/config.yaml
    │
    ├── LOCATION: "New York, NY"  ← Change this one variable
    │
    └── SEARCH_QUERIES:           ← These get the location injected
         ├── "real estate company {location}"
         ├── "realtor {location}"
         ├── "property dealer {location}"
         └── ... (10 queries total)
                │
                ▼
         DuckDuckGo Search API
         (10 queries × 25 results each = 250 raw results)
                │
                ▼
         Score & Filter
         ├── Score = real estate relevance
         ├── Filter out junk domains (YouTube, Facebook, etc.)
         ├── Filter out aggregators (GoodFirms, Clutch, etc.)
         ├── Deduplicate by domain
         │
         ▼
         Output
         ├── output/latest_websites.csv   (full metadata)
         ├── output/latest_websites.txt   (URLs only)
         └── output/latest_websites.json  (structured data)
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Change location (edit config/config.yaml)
#    Set LOCATION: "Los Angeles, CA"

# 3. Run
python main.py

# Or override location from CLI:
python main.py --location "Miami, FL"
```

## Files

| File | Purpose |
|------|---------|
| `config/config.yaml` | Location + search queries |
| `main.py` | Entry point |
| `src/config.py` | Config loader |
| `src/scraper.py` | DuckDuckGo search + URL extraction |
| `src/cli.py` | CLI + output formatting |
| `requirements.txt` | Dependencies |
| `output/latest_websites.csv` | Latest results (CSV) |
| `output/latest_websites.txt` | Latest results (URLs only) |
| `output/latest_websites.json` | Latest results (JSON) |

## Changing Location

Edit `config/config.yaml`:

```yaml
LOCATION: "Chicago, IL"
```

Or from CLI:

```bash
python main.py --location "Houston, TX"
```

## Search Queries

The config has 10 search queries. Each gets `{location}` replaced:

```yaml
SEARCH_QUERIES:
  - "real estate company {location}"
  - "realtor {location}"
  - "property dealer {location}"
  - "real estate agency {location}"
  - "property management company {location}"
  - "commercial real estate company {location}"
  - "residential real estate company {location}"
  - "real estate developer {location}"
  - "property dealer {location}"
  - "real estate firm {location}"
```

## Output Format

### CSV (latest_websites.csv)
```
website,domain,title,source_query,score,location,found_at
https://www.corcoran.com,corcoran.com,The Corcoran Group,"real estate company New York, NY",11,"New York, NY",2026-08-21T12:25:42
```

### TXT (latest_websites.txt)
```
https://www.corcoran.com
https://serhant.com
https://www.elliman.com
```

### JSON (latest_websites.json)
```json
[
  {
    "website": "https://www.corcoran.com",
    "domain": "corcoran.com",
    "title": "The Corcoran Group: Luxury International Real Estate",
    "snippet": "...",
    "source_query": "real estate company New York, NY",
    "score": 11,
    "location": "New York, NY",
    "found_at": "2026-08-21T12:25:42.294022"
  }
]
```

## Scoring System

Each result gets a score based on relevance:

| Signal | Points |
|--------|--------|
| Real estate keywords in title/domain | +2 each |
| Location keywords | +1 each |
| Company-type terms (LLC, group, etc.) | +1 each |
| Homepage URL (short path) | +3 |
| One-level-deep URL | +1 |
| Aggregator domain | -6 |
| Directory/listing terms | -3 |
| News/blog/list pages | -4 |
| Government sites | -10 |

Higher score = more likely an actual real estate company website.

## Dependencies

- Python 3.11+
- `ddgs` (DuckDuckGo Search API)
- `pyyaml` (config parsing)

## Notes

- Uses DuckDuckGo (no API key required)
- Includes retry logic with jitter for rate limiting
- Results are deduplicated by domain
- Timestamped files prevent overwriting previous runs
