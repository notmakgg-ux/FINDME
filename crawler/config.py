"""
Configuration management for Email Crawler.
All settings loaded from .env file with sensible defaults.
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from .env file."""

    # --- Concurrency ---
    max_concurrent_requests: int = Field(default=50, description="Max concurrent HTTP requests")
    max_concurrent_per_domain: int = Field(default=3, description="Max concurrent requests per domain")

    # --- HTTP ---
    http_timeout: int = Field(default=20, description="HTTP request timeout in seconds")
    http_follow_redirects: bool = Field(default=True, description="Follow HTTP redirects")

    # --- Crawl Limits ---
    max_priority_pages: int = Field(default=30, description="Max pages with priority keywords")
    max_agent_pages: int = Field(default=20, description="Max individual agent/broker pages")
    max_total_pages_per_domain: int = Field(default=50, description="Max total pages per domain")

    # --- Playwright ---
    enable_playwright_fallback: bool = Field(default=True, description="Use Playwright for JS-heavy pages")

    # --- Retries ---
    max_retries: int = Field(default=3, description="Max retries for failed requests")
    retry_backoff: float = Field(default=2, description="Backoff multiplier for retries")

    # --- Confidence Thresholds ---
    confidence_excellent: int = Field(default=90, description="Excellent confidence threshold")
    confidence_high: int = Field(default=75, description="High confidence threshold")
    confidence_medium: int = Field(default=50, description="Medium confidence threshold")

    # --- Scoring Weights ---
    score_mailto: int = Field(default=30, description="Score for mailto link extraction")
    score_contact_page: int = Field(default=20, description="Score for contact page source")
    score_team_page: int = Field(default=25, description="Score for team page source")
    score_agent_page: int = Field(default=30, description="Score for individual agent page")
    score_domain_match: int = Field(default=20, description="Score for domain match")
    score_mx_valid: int = Field(default=15, description="Score for valid MX records")
    score_person_detected: int = Field(default=15, description="Score for person name detected")
    score_decision_maker: int = Field(default=30, description="Score for decision-maker role")
    score_relevant_role: int = Field(default=20, description="Score for relevant broker/agent role")
    penalty_personal_domain: int = Field(default=-5, description="Penalty for personal email domain")
    penalty_different_domain: int = Field(default=-30, description="Penalty for unrelated domain")
    penalty_low_priority: int = Field(default=-30, description="Penalty for low-priority email type")
    penalty_disposable: int = Field(default=-100, description="Penalty for disposable domain")
    penalty_noreply: int = Field(default=-100, description="Penalty for noreply address")

    # --- PostgreSQL Database ---
    db_host: str = Field(default="localhost", description="PostgreSQL host")
    db_port: int = Field(default=5432, description="PostgreSQL port")
    db_name: str = Field(default="findme", description="PostgreSQL database name")
    db_user: str = Field(default="postgres", description="PostgreSQL user")
    db_password: str = Field(default="", description="PostgreSQL password")
    db_auto_create: bool = Field(default=True, description="Auto-create tables on startup")

    # --- Paths ---
    input_dir: Path = Field(default=Path("input"), description="Input directory")
    output_dir: Path = Field(default=Path("output"), description="Output directory")

    model_config = {"env_file": "../.env", "env_file_encoding": "utf-8"}


# Global settings instance
settings = Settings()


# --- Priority URL Keywords ---
PRIORITY_KEYWORDS = {
    # Priority 100 - Contact pages
    100: ["contact", "contact-us", "contact_us"],
    # Priority 95 - Team pages
    95: ["team", "our-team", "our_team", "meet-the-team", "meet_the_team"],
    # Priority 90 - Agent/Broker pages
    90: [
        "agents", "agent", "realtors", "realtor", "brokers", "broker",
        "staff", "people", "leadership", "management", "our-agents", "our_agents",
        "find-an-agent", "find_an_agent", "agent-directory", "agent_directory",
        "brokerage", "our-brokers", "our_brokers", "real-estate-agents",
        "real_estate_agents", "realtor-directory", "realtor_directory",
        "office", "locations",
    ],
    # Priority 85 - About pages
    85: ["about", "about-us", "about_us", "company", "our-story", "our_story"],
    # Priority 50 - Other useful pages
    50: ["services", "listings", "properties"],
    # Priority 20 - Low priority
    20: ["privacy", "terms", "legal", "disclaimer", "sitemap"],
}

# Low-priority URL patterns to avoid
LOW_PRIORITY_PATTERNS = [
    "/blog/",
    "/news/",
    "/search",
    "/tag/",
    "/category/",
    "/page/",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".css",
    ".js",
    ".xml",
    ".zip",
    ".doc",
    ".docx",
]

# --- Page Type Keywords ---
PAGE_TYPE_KEYWORDS = {
    "contact": ["contact", "get-in-touch", "reach-us", "email-us"],
    "team": ["team", "our-team", "staff", "people", "leadership", "meet-the-team", "agents", "brokers"],
    "about": ["about", "about-us", "company", "our-story", "mission"],
    "agent_directory": ["agents", "find-an-agent", "agent-directory", "our-agents", "realtors"],
    "agent_profile": [],  # Detected by URL pattern like /agents/name or /team/name
    "office": ["office", "locations", "offices"],
}

# --- Role Classification Keywords ---
TIER_1_ROLES = [
    "owner", "co-owner", "founder", "co-founder", "ceo", "chief executive officer",
    "president", "principal", "partner", "managing partner", "broker owner",
    "broker-owner", "managing broker", "director", "vice president", "vp",
    "chief operating officer", "coo", "chief financial officer", "cfo",
]

TIER_2_ROLES = [
    "broker", "associate broker", "broker associate", "team leader",
    "sales manager", "office manager", "regional manager", "realtor",
    "real estate agent", "agent", "licensed agent", "sales associate",
    "real estate professional",
]

TIER_3_ROLES = [
    "info", "hello", "contact", "office", "admin", "sales", "support",
    "general", "inquiries",
]

LOW_PRIORITY_ROLES = [
    "privacy", "legal", "compliance", "careers", "jobs", "support",
    "help", "noreply", "no-reply", "donotreply", "do-not-reply",
    "unsubscribe", "marketing", "billing",
]

# --- Disposable Email Domains ---
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net",
    "tempmail.com", "throwaway.email", "temp-mail.org",
    "fakeinbox.com", "sharklasers.com", "grr.la",
    "dispostable.com", "yopmail.com", "yopmail.fr",
    "maildrop.cc", "trashmail.com", "trashmail.net",
    "guerrillamailblock.com", "grr.la", "guerrillamail.info",
    "10minutemail.com", "mintemail.com", "mohmal.com",
    "getnada.com", "emailondeck.com", "33mail.com",
    "mytemp.email", "harakirimail.com", "tmail.io",
    "tmpmail.net", "tmpmail.org", "mailnesia.com",
}
