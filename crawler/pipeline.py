"""
Pipeline orchestrator — coordinates crawling, extraction, validation, scoring,
and output for each company.
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from config import settings
from models.schemas import (
    CompanyResult, ExtractedEmail, ExtractedPhone, ExtractedSocial,
    CrawlStatus, PageType, ExtractionMethod,
    NormalizedURL, CrawlURL,
)
from utils.urls import normalize_url, get_root_domain
from utils.priority import prioritize_urls, assign_priority, classify_page_type
from utils.link_discovery import extract_internal_links
from utils.deduplication import deduplicate_emails
from crawler.http_crawler import HTTPCrawler
from crawler.playwright_crawler import render_page
from crawler.sitemap import discover_sitemap_urls
from extraction.email_extractor import extract_all_emails
from extraction.phone_extractor import extract_all_phones
from extraction.social_extractor import extract_all_social
from extraction.webextractor import webextract_all
from extraction.context_extractor import extract_person_context
from classification.page_classifier import classify_page
from classification.role_classifier import classify_person_role
from classification.contact_ranker import rank_contacts, select_best_contact
from validation.email_validator import validate_email_comprehensive
from scoring.confidence import score_all

logger = logging.getLogger(__name__)


async def process_companies(
    companies: list[dict],
    concurrency: int = 50,
    progress_callback=None,
    batch_callback=None,
    result_callback=None,
    control=None,
) -> list[CompanyResult]:
    """
    Process all companies through the full pipeline.
    
    Args:
        companies: list of dicts with company_name, website
        concurrency: max concurrent crawls
        progress_callback: optional callback(current, total, status_message)
        batch_callback: optional callback(results) called after each batch completes
        result_callback: optional callback(result) called after EACH company finishes
                        (for real-time per-company DB saves)
        control: optional ControlEvent for cooperative pause/cancel
    
    Returns:
        list of CompanyResult objects
    """
    results = []
    total = len(companies)

    # Process companies in batches to manage resources
    batch_size = min(concurrency, 10)
    
    for i in range(0, total, batch_size):
        if control:
            if not control.check():
                logger.info("Email Crawler: Stop requested before batch, halting.")
                break
            if not await control.wait_async_if_paused():
                logger.info("Email Crawler: Stop requested while paused, halting.")
                break

        batch = companies[i:i + batch_size]
        tasks = []
        
        for company in batch:
            task = asyncio.create_task(
                asyncio.wait_for(
                    _process_single_company(
                        company["company_name"],
                        company["website"],
                        company.get("extra_columns", {}),
                    ),
                    timeout=120,  # 2 min max per company
                )
            )
            tasks.append((company["company_name"], task))

        for name, task in tasks:
            try:
                result = await task
                results.append(result)
                contacts = result.emails_found + result.phones_found + result.social_links_found
                status = f"[OK] {name}: {result.emails_found} emails, {result.phones_found} phones, {result.social_links_found} social" if contacts else f"[--] {name}: no contacts"
                logger.info(status)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout processing {name}")
                results.append(CompanyResult(
                    company_name=name,
                    original_website=company.get("website", ""),
                    status=CrawlStatus.FAILED,
                    error_type="TimeoutError",
                    error_message="Company processing timed out after 120s",
                    stage_failed="pipeline",
                ))
            except Exception as e:
                logger.error(f"Failed to process {name}: {e}")
                results.append(CompanyResult(
                    company_name=name,
                    original_website=company.get("website", ""),
                    status=CrawlStatus.FAILED,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    stage_failed="pipeline",
                ))

            # Real-time per-company callback — save to DB as soon as each company finishes
            if result_callback:
                try:
                    result_callback(results[-1])
                except Exception as e:
                    logger.warning(f"Result callback error for {name}: {e}")

            if progress_callback:
                progress_callback(len(results), total, f"Processed {name}")

        # Save progress after each batch completes
        if batch_callback:
            try:
                batch_callback(results)
            except Exception as e:
                logger.warning(f"Batch callback error: {e}")

    return results


async def _process_single_company(
    company_name: str,
    website: str,
    extra_columns: dict = None,
) -> CompanyResult:
    """Process a single company through the full pipeline."""
    start_time = time.time()

    result = CompanyResult(
        company_name=company_name,
        original_website=website,
        normalized_url=normalize_url(website),
        root_domain=get_root_domain(normalize_url(website)),
        started_at=datetime.now(),
        status=CrawlStatus.PROCESSING,
    )

    http_crawler = HTTPCrawler()
    pages_crawled = 0

    try:
        base_url = result.normalized_url

        # === STAGE 1: Discover URLs ===
        logger.info(f"[{company_name}] Discovering URLs from {base_url}")

        # Fetch homepage
        homepage_resp = await http_crawler.fetch(base_url)
        if homepage_resp is None or homepage_resp.status_code >= 400:
            result.status = CrawlStatus.FAILED
            result.error_type = "fetch_error"
            result.error_message = f"Could not fetch homepage: HTTP {homepage_resp.status_code if homepage_resp else 'None'}"
            result.stage_failed = "homepage_fetch"
            return result

        homepage_html = homepage_resp.text

        # === STAGE 1b: Extract contacts from homepage immediately ===
        all_emails_raw = []
        all_phones_raw = []
        all_social_raw = []
        _extract_contacts_from_html(homepage_html, base_url, "homepage",
                                    all_emails_raw, all_phones_raw, all_social_raw)

        # Extract links from homepage
        internal_links = extract_internal_links(homepage_html, base_url)
        logger.info(f"[{company_name}] Found {len(internal_links)} internal links from homepage")

        # Discover sitemap URLs
        sitemap_urls = []
        try:
            sitemap_urls = await discover_sitemap_urls(base_url, httpx_client=await http_crawler._get_httpx_client())
            logger.info(f"[{company_name}] Found {len(sitemap_urls)} URLs from sitemaps")
        except Exception as e:
            logger.debug(f"[{company_name}] Sitemap discovery failed: {e}")

        # Merge and prioritize all URLs
        all_urls = list(set(internal_links + sitemap_urls))
        prioritized = prioritize_urls(all_urls, base_url)
        result.pages_discovered = len(prioritized)

        logger.info(f"[{company_name}] Prioritized {len(prioritized)} URLs")

        # === STAGE 2: Crawl priority pages ===
        pages_crawled = 0
        max_pages = settings.max_total_pages_per_domain

        # Limit crawl to max pages
        urls_to_crawl = prioritized[:max_pages]

        # Batch fetch pages
        url_list = [cu.url for cu in urls_to_crawl]
        
        # Fetch in small batches
        crawled_htmls: dict[str, str] = {}
        BATCH = 10
        for i in range(0, len(url_list), BATCH):
            batch_urls = url_list[i:i + BATCH]
            responses = await http_crawler.fetch_many(batch_urls)
            for url, resp in responses.items():
                if resp and resp.status_code < 400:
                    crawled_htmls[url] = resp.text
                    pages_crawled += 1

        logger.info(f"[{company_name}] Crawled {pages_crawled} pages via HTTP")

        # === STAGE 3: Extract ALL contacts from crawled pages ===
        playwright_needed = []
        
        for crawl_url in urls_to_crawl:
            url = crawl_url.url
            html = crawled_htmls.get(url)
            if not html:
                continue

            page_type = classify_page(url, html)
            _extract_contacts_from_html(html, url, page_type.value,
                                        all_emails_raw, all_phones_raw, all_social_raw)

            # If high-priority page found no contacts, mark for Playwright fallback
            has_any = (any(e.source_url == url for e in all_emails_raw) or
                       any(p.source_url == url for p in all_phones_raw) or
                       any(s.source_url == url for s in all_social_raw))
            if not has_any and crawl_url.priority >= 85:
                playwright_needed.append(url)

        logger.info(f"[{company_name}] HTTP extraction: {len(all_emails_raw)} emails, {len(all_phones_raw)} phones, {len(all_social_raw)} social")

        # === STAGE 4: Playwright fallback for empty high-priority pages ===
        if settings.enable_playwright_fallback and playwright_needed:
            logger.info(f"[{company_name}] Running Playwright fallback on {len(playwright_needed)} pages")
            result.playwright_used = True

            for url in playwright_needed[:5]:
                try:
                    html = await render_page(url, wait_ms=3000)
                    if html:
                        page_type = classify_page(url, html)
                        _extract_contacts_from_html(html, url, page_type.value,
                                                    all_emails_raw, all_phones_raw, all_social_raw)
                except Exception as e:
                    logger.debug(f"[{company_name}] Playwright fallback failed for {url}: {e}")

        # === STAGE 5: Context extraction for emails ===
        logger.info(f"[{company_name}] Enriching {len(all_emails_raw)} emails with context")

        for email_obj in all_emails_raw:
            if not email_obj.person_name or not email_obj.job_title:
                html = crawled_htmls.get(email_obj.source_url, "")
                if html:
                    ctx = extract_person_context(html, email_obj.email)
                    if ctx.get("person_name") and not email_obj.person_name:
                        email_obj.person_name = ctx["person_name"]
                    if ctx.get("job_title") and not email_obj.job_title:
                        email_obj.job_title = ctx["job_title"]
                    if ctx.get("role") and not email_obj.role:
                        email_obj.role = ctx["role"]

            if email_obj.job_title and not email_obj.role:
                role_info = classify_person_role(
                    email_obj.person_name, email_obj.job_title, email_obj.email
                )
                email_obj.role = role_info.get("role", email_obj.job_title)

        # === STAGE 6: Email validation ===
        logger.info(f"[{company_name}] Validating {len(all_emails_raw)} emails")

        validated_emails = []
        for email_obj in all_emails_raw:
            validation = validate_email_comprehensive(
                email_obj.email, result.root_domain
            )
            email_obj.syntax_valid = validation["syntax_valid"]
            email_obj.mx_valid = validation["mx_valid"]
            email_obj.domain_match = validation["domain_match"]
            email_obj.is_disposable = validation["is_disposable_email"]

            if validation["overall_valid"]:
                validated_emails.append(email_obj)
            else:
                result.emails_rejected += 1
                logger.debug(
                    f"[{company_name}] Rejected {email_obj.email}: "
                    f"syntax={validation['syntax_valid']}, "
                    f"disposable={validation['is_disposable_email']}, "
                    f"noreply={validation['is_noreply_email']}"
                )

        # === STAGE 7: Deduplication ===
        deduped_emails = deduplicate_emails(validated_emails)
        logger.info(f"[{company_name}] After dedup: {len(deduped_emails)} unique emails")

        # Dedup phones by normalized number
        seen_phones = set()
        deduped_phones = []
        for p in all_phones_raw:
            if p.phone not in seen_phones:
                seen_phones.add(p.phone)
                deduped_phones.append(p)

        # Dedup social by URL
        seen_social = set()
        deduped_social = []
        for s in all_social_raw:
            if s.url not in seen_social:
                seen_social.add(s.url)
                deduped_social.append(s)

        # === STAGE 8: Confidence scoring ===
        scored = score_all(deduped_emails)

        # === STAGE 9: Contact ranking ===
        ranked = rank_contacts(scored)
        result.emails = ranked
        result.emails_found = len(ranked)

        # Set phones and social links
        result.phones = deduped_phones
        result.phones_found = len(deduped_phones)
        result.social_links = deduped_social
        result.social_links_found = len(deduped_social)

        # Select best contact
        best = select_best_contact(ranked)
        result.best_contact_email = best["best_contact_email"]
        result.best_contact_name = best["best_contact_name"]
        result.best_contact_role = best["best_contact_role"]
        result.best_contact_score = best["best_contact_score"]

        # Set social media summary
        for s in deduped_social:
            if s.platform == "linkedin" and not result.linkedin_url:
                result.linkedin_url = s.url
            elif s.platform == "facebook" and not result.facebook_url:
                result.facebook_url = s.url
            elif s.platform == "instagram" and not result.instagram_url:
                result.instagram_url = s.url

        # Set primary phone (first non-fax)
        for p in deduped_phones:
            if not p.is_fax:
                result.primary_phone = p.phone
                break

        has_any = ranked or deduped_phones or deduped_social
        if has_any:
            result.status = CrawlStatus.COMPLETED
        else:
            result.status = CrawlStatus.NO_CONTACTS_FOUND

    except Exception as e:
        logger.error(f"[{company_name}] Pipeline error: {e}")
        result.status = CrawlStatus.FAILED
        result.error_type = type(e).__name__
        result.error_message = str(e)
        result.stage_failed = "pipeline_error"
        import traceback
        traceback.print_exc()

    finally:
        await http_crawler.close()
        result.completed_at = datetime.now()
        result.duration_seconds = time.time() - start_time
        result.pages_crawled = pages_crawled

    return result


def _extract_contacts_from_html(
    html: str,
    url: str,
    page_type: str,
    all_emails: list,
    all_phones: list,
    all_social: list,
):
    """
    Extract all contact types from a single HTML page using 3-tier hierarchy:
        1. Scrapling-style CSS selectors (via existing extractors)
        2. WebExtractor regex pass (quick secondary scan)
        3. (Playwright handled at caller level for JS-heavy pages)
    """
    # --- Tier 1: Scrapling-style extraction (existing extractors) ---
    raw_emails = extract_all_emails(html, url, page_type)
    for raw in raw_emails:
        email_obj = ExtractedEmail(
            email=raw["email"],
            source_url=raw.get("source_url", url),
            source_page_type=page_type,
            extraction_method=raw.get("extraction_method", ExtractionMethod.REGEX),
            nearby_text=raw.get("nearby_text", ""),
            html_context=raw.get("html_context", ""),
            person_name=raw.get("person_name", ""),
            job_title=raw.get("job_title", ""),
        )
        all_emails.append(email_obj)

    raw_phones = extract_all_phones(html, url, page_type)
    for raw in raw_phones:
        phone_obj = ExtractedPhone(
            phone=raw["phone"],
            raw_phone=raw.get("raw_phone", raw["phone"]),
            country_code=raw.get("country_code", ""),
            source_url=raw.get("source_url", url),
            source_page_type=page_type,
            extraction_method=raw.get("extraction_method", ExtractionMethod.REGEX),
            nearby_text=raw.get("nearby_text", ""),
            person_name=raw.get("person_name", ""),
            is_fax=raw.get("is_fax", False),
            is_mobile=raw.get("is_mobile", False),
        )
        all_phones.append(phone_obj)

    raw_social = extract_all_social(html, url, page_type)
    for raw in raw_social:
        social_obj = ExtractedSocial(
            platform=raw["platform"],
            url=raw["url"],
            raw_url=raw.get("raw_url", raw["url"]),
            profile_name=raw.get("profile_name", ""),
            source_url=raw.get("source_url", url),
            source_page_type=page_type,
            extraction_method=raw.get("extraction_method", ExtractionMethod.CSS_SELECTOR),
            is_company_page=raw.get("is_company_page", False),
            is_personal_profile=raw.get("is_personal_profile", False),
        )
        all_social.append(social_obj)

    # --- Tier 2: WebExtractor regex pass (catches anything Tier 1 missed) ---
    we_result = webextract_all(html, url)
    existing_emails = {e.email for e in all_emails}
    existing_phones = {p.phone for p in all_phones}
    existing_social = {s.url for s in all_social}

    for raw in we_result["emails"]:
        if raw["email"] not in existing_emails:
            all_emails.append(ExtractedEmail(
                email=raw["email"],
                source_url=raw.get("source_url", url),
                extraction_method=raw.get("extraction_method", ExtractionMethod.REGEX),
                nearby_text=raw.get("nearby_text", ""),
            ))
            existing_emails.add(raw["email"])

    for raw in we_result["phones"]:
        digits = re.sub(r'[^\d]', '', raw["phone"])
        if digits not in existing_phones:
            all_phones.append(ExtractedPhone(
                phone=digits,
                raw_phone=raw.get("raw_phone", raw["phone"]),
                source_url=raw.get("source_url", url),
                extraction_method=raw.get("extraction_method", ExtractionMethod.REGEX),
                nearby_text=raw.get("nearby_text", ""),
                is_fax=raw.get("is_fax", False),
                is_mobile=raw.get("is_mobile", False),
            ))
            existing_phones.add(digits)

    for raw in we_result["social"]:
        if raw["url"] not in existing_social:
            all_social.append(ExtractedSocial(
                platform=raw["platform"],
                url=raw["url"],
                raw_url=raw.get("raw_url", raw["url"]),
                profile_name=raw.get("profile_name", ""),
                source_url=raw.get("source_url", url),
                extraction_method=raw.get("extraction_method", ExtractionMethod.REGEX),
                is_company_page=raw.get("is_company_page", False),
                is_personal_profile=raw.get("is_personal_profile", False),
            ))
            existing_social.add(raw["url"])
