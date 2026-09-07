"""
Data models for the email crawler pipeline.
Uses Pydantic for validation and serialization.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# --- Enums ---

class PageType(str, Enum):
    HOMEPAGE = "homepage"
    CONTACT = "contact"
    ABOUT = "about"
    TEAM = "team"
    LEADERSHIP = "leadership"
    AGENT_DIRECTORY = "agent_directory"
    AGENT_PROFILE = "agent_profile"
    BROKER_PROFILE = "broker_profile"
    OFFICE = "office"
    OTHER = "other"


class EmailType(str, Enum):
    PERSONAL = "personal"
    DECISION_MAKER = "decision_maker"
    GENERAL = "general"
    LOW_PRIORITY = "low_priority"
    DISPOSABLE = "disposable"


class ConfidenceLevel(str, Enum):
    EXCELLENT = "excellent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExtractionMethod(str, Enum):
    MAILTO = "mailto"
    REGEX = "regex"
    OBFUSCATED = "obfuscated"
    HTML_ENTITY = "html_entity"
    HTML_ATTRIBUTE = "html_attribute"
    CLOUDFLARE = "cloudflare"
    JSON_LD = "json_ld"
    JAVASCRIPT = "javascript"
    STRUCTURED_DATA = "structured_data"
    SCRAPLING = "scrapling"
    WEBEXTRACTOR = "webextractor"
    PLAYWRIGHT = "playwright"
    CSS_SELECTOR = "css_selector"


class CrawlStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_EMAIL_FOUND = "no_email_found"
    NO_CONTACTS_FOUND = "no_contacts_found"


# --- Input Models ---

class CompanyInput(BaseModel):
    """Input company from Excel."""
    company_name: str
    website: str
    original_row: dict[str, Any] = Field(default_factory=dict)


# --- URL Models ---

class NormalizedURL(BaseModel):
    """Normalized URL information."""
    original: str
    normalized: str
    root_domain: str
    full_url: str


class CrawlURL(BaseModel):
    """URL with priority for crawling."""
    url: str
    priority: int = 0
    page_type: PageType = PageType.OTHER
    source_url: str = ""
    discovered_at: datetime = Field(default_factory=datetime.now)


# --- Extracted Data Models ---

class ExtractedEmail(BaseModel):
    """A single extracted email with full context."""
    email: str
    source_url: str
    source_page_type: PageType = PageType.OTHER
    extraction_method: ExtractionMethod = ExtractionMethod.REGEX
    nearby_text: str = ""
    html_context: str = ""
    person_name: str = ""
    job_title: str = ""
    role: str = ""
    email_type: EmailType = EmailType.GENERAL

    # Validation
    syntax_valid: bool = False
    mx_valid: bool = False
    domain_match: bool = False
    is_disposable: bool = False

    # Scoring
    confidence_score: int = 0
    confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    score_reasons: list[str] = Field(default_factory=list)

    # Dedup tracking
    best_source_url: str = ""
    all_source_urls: list[str] = Field(default_factory=list)


class ExtractedPhone(BaseModel):
    """A single extracted phone number with context."""
    phone: str
    raw_phone: str = ""  # Original format before cleaning
    country_code: str = ""
    source_url: str
    source_page_type: PageType = PageType.OTHER
    extraction_method: ExtractionMethod = ExtractionMethod.REGEX
    nearby_text: str = ""
    person_name: str = ""
    is_fax: bool = False
    is_mobile: bool = False


class ExtractedSocial(BaseModel):
    """A single extracted social media link."""
    platform: str  # "linkedin", "facebook", "instagram", "twitter", "youtube", "tiktok"
    url: str
    raw_url: str = ""  # Original URL before normalization
    profile_name: str = ""  # Extracted profile/page name
    source_url: str
    source_page_type: PageType = PageType.OTHER
    extraction_method: ExtractionMethod = ExtractionMethod.CSS_SELECTOR
    is_company_page: bool = False
    is_personal_profile: bool = False


class PersonInfo(BaseModel):
    """Detected person information near an email."""
    name: str = ""
    job_title: str = ""
    role: str = ""
    tier: int = 0  # 1=decision maker, 2=senior, 3=general, 0=unknown


class ScoreBreakdown(BaseModel):
    """Detailed breakdown of confidence scoring."""
    total_score: int = 0
    level: ConfidenceLevel = ConfidenceLevel.LOW
    reasons: list[str] = Field(default_factory=list)


# --- Company Result Models ---

class CompanyResult(BaseModel):
    """Complete result for a single company."""
    company_name: str
    original_website: str
    normalized_url: str = ""
    root_domain: str = ""
    status: CrawlStatus = CrawlStatus.PENDING

    # Discovered data
    emails: list[ExtractedEmail] = Field(default_factory=list)
    phones: list[ExtractedPhone] = Field(default_factory=list)
    social_links: list[ExtractedSocial] = Field(default_factory=list)

    # Best contact summary
    best_contact_email: str = ""
    best_contact_name: str = ""
    best_contact_role: str = ""
    best_contact_score: int = 0

    # Social media summary
    linkedin_url: str = ""
    facebook_url: str = ""
    instagram_url: str = ""
    primary_phone: str = ""

    # Stats
    pages_crawled: int = 0
    pages_discovered: int = 0
    emails_found: int = 0
    emails_rejected: int = 0
    phones_found: int = 0
    social_links_found: int = 0
    playwright_used: bool = False

    # Timing
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None

    # Error tracking
    error_type: str = ""
    error_message: str = ""
    stage_failed: str = ""


class CrawlState(BaseModel):
    """Persistent state for resumable crawling."""
    company_name: str
    website: str
    status: CrawlStatus = CrawlStatus.PENDING
    pages_crawled: int = 0
    emails_found: int = 0
    last_url: str = ""
    error: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
