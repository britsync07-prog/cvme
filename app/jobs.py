"""Tenant-safe job search adapters and public company-contact research.

Only published APIs are called.  Providers are independently optional: one
provider failing or being rate-limited never prevents a partial search result.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from .config import settings
from .services import EMAIL_RE, _public_url, _search_web, _website_text


log = logging.getLogger("tailormail.jobs")
USER_AGENT = "TailorJobhunt Job Search/1.0 (+https://out.ascentraconsulting.co.uk/contact)"
JOB_BOARD_DOMAINS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "adzuna.com", "jooble.org",
    "remotive.com", "arbeitnow.com", "jobicy.com", "themuse.com", "himalayas.app",
    "ziprecruiter.com", "google.com", "greenhouse.io", "lever.co", "workable.com",
    "ashbyhq.com", "smartrecruiters.com", "teamtailor.com", "bamboohr.com",
    "icims.com", "jobvite.com",
}
NON_EMPLOYER_DOMAINS = {
    "wikipedia.org", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "youtube.com", "tiktok.com", "crunchbase.com", "pitchbook.com", "yelp.com",
    "dnb.com", "zoominfo.com", "glassdoor.co.uk",
}
COUNTRY_ALIASES = {
    "uk": "GB", "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "usa": "US", "us": "US", "united states": "US", "united states of america": "US",
    "canada": "CA", "australia": "AU", "germany": "DE", "deutschland": "DE",
    "france": "FR", "netherlands": "NL", "bangladesh": "BD", "india": "IN",
    "ireland": "IE", "spain": "ES", "italy": "IT", "portugal": "PT",
    "poland": "PL", "sweden": "SE", "norway": "NO", "denmark": "DK",
    "finland": "FI", "japan": "JP", "singapore": "SG", "new zealand": "NZ",
}
ROLE_CONTACTS = ("careers", "jobs", "recruitment", "recruiting", "hr", "talent", "hiring", "people")
GENERIC_CONTACTS = {"info", "hello", "contact"}
_cache: dict[str, tuple[float, list["NormalizedJob"]]] = {}
_last_call: dict[str, float] = {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_country(value: str | None) -> str | None:
    """Return ISO alpha-2 where possible; provider-specific parsing stays here."""
    if not value:
        return None
    raw = str(value).strip().casefold()
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    if raw in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[raw]
    for name, code in COUNTRY_ALIASES.items():
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", raw):
            return code
    return None


def clean_html(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))).strip()


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (ValueError, OSError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except ValueError:
        return None


def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


def job_hash(source: str, source_job_id: str | None, apply_url: str, title: str, company: str, city: str | None) -> str:
    stable = source_job_id or canonical_url(apply_url) or "|".join((title.casefold(), company.casefold(), (city or "").casefold()))
    return hashlib.sha256(f"{source}|{stable}".encode()).hexdigest()[:40]


def classify_category(title: str, description: str = "", explicit: str | None = None) -> str:
    text = f"{title} {description}".casefold()
    if explicit:
        return str(explicit)[:80]
    if re.search(r"software|developer|engineer|data|cyber|it |cloud|devops|product|ux|designer", text):
        return "IT"
    if re.search(r"driver|warehouse|labour|labor|construction|cleaner|chef|nurse|caregiver|retail|security guard|factory", text):
        return "Physical / frontline"
    return "Non-IT"


@dataclass(frozen=True)
class JobQuery:
    keywords: str
    country: str | None = None
    city: str | None = None
    remote_only: bool = False
    job_type: str | None = None
    category: str | None = None
    salary_min: int | None = None
    posted_within_days: int | None = None
    sources: list[str] | None = None
    page: int = 1
    page_size: int = 25

    def normalized_country(self) -> str | None:
        return normalize_country(self.country)


@dataclass
class NormalizedJob:
    id: str
    source: str
    source_job_id: str | None
    title: str
    company_name: str
    company_website: str | None
    location_text: str | None
    country_code: str | None
    city: str | None
    is_remote: bool
    job_type: str | None
    category: str | None
    salary_min: int | None
    salary_max: int | None
    salary_currency: str | None
    description: str
    apply_url: str
    posted_at: datetime | None
    fetched_at: datetime
    raw: dict[str, Any]
    sources: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        data["fetched_at"] = self.fetched_at.isoformat()
        return data


@dataclass
class ProviderStatus:
    name: str
    status: str
    message: str = ""
    count: int = 0

    def public(self) -> dict[str, Any]:
        return asdict(self)


class ProviderUnavailable(RuntimeError):
    pass


class JobProvider(ABC):
    name = "provider"
    enabled_setting = "enable_provider"
    min_interval_seconds = 0.5

    def enabled(self) -> bool:
        return bool(getattr(settings, self.enabled_setting, True))

    def configured(self) -> tuple[bool, str]:
        return True, ""

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
        remaining = self.min_interval_seconds - (time.monotonic() - _last_call.get(self.name, 0))
        if remaining > 0:
            await asyncio.sleep(remaining)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.request(method, url, **kwargs)
                _last_call[self.name] = time.monotonic()
                if response.status_code == 429:
                    retry_after = min(int(response.headers.get("retry-after", "1") or 1), 30)
                    if attempt == 2:
                        raise ProviderUnavailable("rate limited (429)")
                    await asyncio.sleep(retry_after)
                    continue
                response.raise_for_status()
                return response
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                await asyncio.sleep(0.4 * (2**attempt))
        raise ProviderUnavailable(str(last_error or "request failed"))

    @abstractmethod
    async def search(self, query: JobQuery) -> list[NormalizedJob]:
        raise NotImplementedError


def _matches_query(job: NormalizedJob, query: JobQuery) -> bool:
    if query.remote_only and not job.is_remote:
        return False
    country = query.normalized_country()
    # A remote listing without a country restriction remains useful in a country search.
    if country and job.country_code and job.country_code != country:
        return False
    if query.city and job.city and query.city.casefold() not in job.city.casefold():
        return False
    if query.job_type and job.job_type and query.job_type.casefold() not in job.job_type.casefold():
        return False
    if query.category:
        desired = query.category.casefold()
        classified = classify_category(job.title, job.description).casefold()
        if desired in {"it", "non-it", "physical / frontline"}:
            if classified != desired:
                return False
        elif job.category and desired not in job.category.casefold():
            return False
    if query.salary_min and (job.salary_max or job.salary_min or 0) < query.salary_min:
        return False
    if query.posted_within_days and job.posted_at and job.posted_at < utc_now() - timedelta(days=query.posted_within_days):
        return False
    return True


class HimalayasProvider(JobProvider):
    name = "himalayas"
    enabled_setting = "enable_himalayas"

    async def search(self, query: JobQuery) -> list[NormalizedJob]:
        params: dict[str, Any] = {"q": query.keywords, "page": max(query.page, 1), "sort": "recent"}
        if query.normalized_country(): params["country"] = query.normalized_country()
        if query.remote_only: params["worldwide"] = "true"
        if query.job_type: params["employment_type"] = query.job_type
        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(timeout=settings.job_provider_timeout_seconds, headers=headers, follow_redirects=True) as client:
            data = (await self._request(client, "GET", "https://himalayas.app/jobs/api/search", params=params)).json()
        items = data.get("jobs", data.get("data", [])) if isinstance(data, dict) else []
        result = []
        for item in items if isinstance(items, list) else []:
            company = item.get("companyName") or item.get("company", {}).get("name") or "Unknown company"
            location = item.get("location") or item.get("locationRestrictions") or "Remote"
            if isinstance(location, list): location = ", ".join(map(str, location))
            apply_url = item.get("applicationLink") or item.get("applyUrl") or item.get("url") or ""
            if not canonical_url(apply_url): continue
            title = item.get("title") or "Untitled job"
            result.append(NormalizedJob(
                id=job_hash(self.name, str(item.get("guid") or item.get("id") or ""), apply_url, title, company, None),
                source=self.name, source_job_id=str(item.get("guid") or item.get("id") or "") or None,
                title=title, company_name=company, company_website=item.get("companyWebsite") or item.get("company", {}).get("website"),
                location_text=str(location), country_code=query.normalized_country() or normalize_country(str(location)), city=None,
                is_remote=True, job_type=item.get("employmentType"), category=classify_category(title, item.get("description") or "", item.get("category")),
                salary_min=item.get("minSalary") or item.get("salaryMin"), salary_max=item.get("maxSalary") or item.get("salaryMax"), salary_currency=item.get("currency") or item.get("salaryCurrency"),
                description=clean_html(item.get("description") or item.get("excerpt")), apply_url=canonical_url(apply_url),
                posted_at=parse_datetime(item.get("pubDate") or item.get("publishedAt")), fetched_at=utc_now(), raw=item, sources=[self.name],
            ))
        return [job for job in result if _matches_query(job, query)]


class RemotiveProvider(JobProvider):
    name = "remotive"
    enabled_setting = "enable_remotive"
    min_interval_seconds = 30.0  # provider requests no more than two calls/minute

    async def search(self, query: JobQuery) -> list[NormalizedJob]:
        params = {"search": query.keywords} if query.keywords else {}
        async with httpx.AsyncClient(timeout=settings.job_provider_timeout_seconds, headers={"User-Agent": USER_AGENT}) as client:
            data = (await self._request(client, "GET", "https://remotive.com/api/remote-jobs", params=params)).json()
        result = []
        for item in data.get("jobs", []) if isinstance(data, dict) else []:
            title, company, apply_url = item.get("title") or "Untitled job", item.get("company_name") or "Unknown company", item.get("url") or ""
            if not canonical_url(apply_url): continue
            location = item.get("candidate_required_location") or "Remote"
            result.append(NormalizedJob(job_hash(self.name, str(item.get("id") or ""), apply_url, title, company, None), self.name, str(item.get("id") or "") or None, title, company, item.get("company_logo_url"), str(location), normalize_country(str(location)), None, True, item.get("job_type"), classify_category(title, item.get("description") or "", item.get("category")), item.get("salary_min"), item.get("salary_max"), item.get("salary_currency"), clean_html(item.get("description")), canonical_url(apply_url), parse_datetime(item.get("publication_date")), utc_now(), item, [self.name]))
        return [job for job in result if _matches_query(job, query)]


class ArbeitnowProvider(JobProvider):
    name = "arbeitnow"
    enabled_setting = "enable_arbeitnow"

    async def search(self, query: JobQuery) -> list[NormalizedJob]:
        async with httpx.AsyncClient(timeout=settings.job_provider_timeout_seconds, headers={"User-Agent": USER_AGENT}) as client:
            data = (await self._request(client, "GET", "https://www.arbeitnow.com/api/job-board-api", params={"page": max(query.page, 1)})).json()
        result = []
        for item in data.get("data", []) if isinstance(data, dict) else []:
            title, company, apply_url = item.get("title") or "Untitled job", item.get("company_name") or "Unknown company", item.get("url") or ""
            if not canonical_url(apply_url): continue
            location = item.get("location") or ""
            result.append(NormalizedJob(job_hash(self.name, item.get("slug"), apply_url, title, company, None), self.name, item.get("slug"), title, company, None, location, normalize_country(location), location.split(",")[0].strip() or None, bool(item.get("remote")), ", ".join(item.get("job_types") or []) or None, classify_category(title, item.get("description") or ""), None, None, None, clean_html(item.get("description")), canonical_url(apply_url), parse_datetime(item.get("created_at")), utc_now(), item, [self.name]))
        return [job for job in result if _matches_query(job, query) and query.keywords.casefold() in f"{job.title} {job.description}".casefold()]


class JobicyProvider(JobProvider):
    name = "jobicy"
    enabled_setting = "enable_jobicy"
    min_interval_seconds = 1.0

    async def search(self, query: JobQuery) -> list[NormalizedJob]:
        params: dict[str, Any] = {"count": min(max(query.page_size, 1), 100)}
        if query.country: params["geo"] = query.country
        if query.category: params["industry"] = query.category
        async with httpx.AsyncClient(timeout=settings.job_provider_timeout_seconds, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
            data = (await self._request(client, "GET", "https://jobicy.com/api/jobs", params=params)).json()
        result = []
        for item in data.get("jobs", []) if isinstance(data, dict) else []:
            title, company, apply_url = item.get("jobTitle") or "Untitled job", item.get("companyName") or "Unknown company", item.get("url") or ""
            if not canonical_url(apply_url): continue
            location = item.get("jobGeo") or item.get("jobLevel") or "Remote"
            salary = item.get("annualSalaryMin") or item.get("salaryMin")
            result.append(NormalizedJob(job_hash(self.name, str(item.get("id") or ""), apply_url, title, company, None), self.name, str(item.get("id") or "") or None, title, company, item.get("companyUrl"), str(location), normalize_country(str(location)), None, True, item.get("jobType"), classify_category(title, item.get("jobDescription") or "", (item.get("jobIndustry") or [None])[0]), salary, item.get("annualSalaryMax") or item.get("salaryMax"), item.get("salaryCurrency"), clean_html(item.get("jobDescription")), canonical_url(apply_url), parse_datetime(item.get("pubDate")), utc_now(), item, [self.name]))
        return [job for job in result if _matches_query(job, query) and query.keywords.casefold() in f"{job.title} {job.description}".casefold()]


class MuseProvider(JobProvider):
    name = "the_muse"
    enabled_setting = "enable_the_muse"

    async def search(self, query: JobQuery) -> list[NormalizedJob]:
        params: dict[str, Any] = {"page": max(query.page, 1)}
        if query.keywords: params["category"] = query.keywords
        if query.country or query.city: params["location"] = query.city or query.country
        if settings.the_muse_api_key: params["api_key"] = settings.the_muse_api_key
        async with httpx.AsyncClient(timeout=settings.job_provider_timeout_seconds, headers={"User-Agent": USER_AGENT}) as client:
            data = (await self._request(client, "GET", "https://www.themuse.com/api/public/jobs", params=params)).json()
        result = []
        for item in data.get("results", []) if isinstance(data, dict) else []:
            company_data = item.get("company") or {}
            company = company_data.get("name") or "Unknown company"
            title, apply_url = item.get("name") or "Untitled job", item.get("refs", {}).get("landing_page") or ""
            if not canonical_url(apply_url): continue
            locations = ", ".join(part.get("name", "") for part in item.get("locations", []) if isinstance(part, dict))
            categories = ", ".join(part.get("name", "") for part in item.get("categories", []) if isinstance(part, dict))
            result.append(NormalizedJob(job_hash(self.name, str(item.get("id") or ""), apply_url, title, company, None), self.name, str(item.get("id") or "") or None, title, company, company_data.get("refs", {}).get("landing_page"), locations, normalize_country(locations), locations.split(",")[0].strip() or None, "remote" in locations.casefold() or "flexible" in locations.casefold(), ", ".join(part.get("name", "") for part in item.get("levels", []) if isinstance(part, dict)) or None, classify_category(title, item.get("contents") or "", categories), None, None, None, clean_html(item.get("contents")), canonical_url(apply_url), parse_datetime(item.get("publication_date")), utc_now(), item, [self.name]))
        return [job for job in result if _matches_query(job, query)]


class AdzunaProvider(JobProvider):
    name = "adzuna"
    enabled_setting = "enable_adzuna"

    def configured(self) -> tuple[bool, str]:
        return (bool(settings.adzuna_app_id and settings.adzuna_app_key), "missing ADZUNA_APP_ID / ADZUNA_APP_KEY")

    async def search(self, query: JobQuery) -> list[NormalizedJob]:
        country = (query.normalized_country() or "GB").lower()
        params: dict[str, Any] = {"app_id": settings.adzuna_app_id, "app_key": settings.adzuna_app_key, "what": query.keywords, "results_per_page": min(query.page_size, 50), "content-type": "application/json"}
        if query.city: params["where"] = query.city
        if query.salary_min: params["salary_min"] = query.salary_min
        if query.posted_within_days: params["max_days_old"] = query.posted_within_days
        async with httpx.AsyncClient(timeout=settings.job_provider_timeout_seconds, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
            data = (await self._request(client, "GET", f"https://api.adzuna.com/v1/api/jobs/{country}/search/{max(query.page, 1)}", params=params)).json()
        result = []
        for item in data.get("results", []) if isinstance(data, dict) else []:
            title, company, apply_url = item.get("title") or "Untitled job", (item.get("company") or {}).get("display_name") or "Unknown company", item.get("redirect_url") or ""
            if not canonical_url(apply_url): continue
            location = (item.get("location") or {}).get("display_name") or ""
            result.append(NormalizedJob(job_hash(self.name, str(item.get("id") or ""), apply_url, title, company, None), self.name, str(item.get("id") or "") or None, title, company, None, location, country.upper(), location.split(",")[0].strip() or None, "remote" in f"{title} {item.get('description','')} {location}".casefold(), item.get("contract_type") or item.get("contract_time"), classify_category(title, item.get("description") or "", (item.get("category") or {}).get("label")), item.get("salary_min"), item.get("salary_max"), {"gb":"GBP","us":"USD","ca":"CAD","au":"AUD","de":"EUR","nl":"EUR","fr":"EUR"}.get(country), clean_html(item.get("description")), canonical_url(apply_url), parse_datetime(item.get("created")), utc_now(), item, [self.name]))
        return [job for job in result if _matches_query(job, query)]


class JoobleProvider(JobProvider):
    name = "jooble"
    enabled_setting = "enable_jooble"

    def configured(self) -> tuple[bool, str]:
        return bool(settings.jooble_api_key), "missing JOOBLE_API_KEY"

    async def search(self, query: JobQuery) -> list[NormalizedJob]:
        # Jooble's public API requires both a keyword and a free-text location.
        location = query.city or query.country or "United Kingdom"
        payload = {"keywords": query.keywords, "location": location, "page": max(query.page, 1), "ResultOnPage": min(query.page_size, 100)}
        if query.salary_min: payload["salary"] = query.salary_min
        async with httpx.AsyncClient(timeout=settings.job_provider_timeout_seconds, headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"}) as client:
            data = (await self._request(client, "POST", f"https://jooble.org/api/{settings.jooble_api_key}", json=payload)).json()
        result = []
        for item in data.get("jobs", []) if isinstance(data, dict) else []:
            title, company, apply_url = item.get("title") or "Untitled job", item.get("company") or "Unknown company", item.get("link") or ""
            if not canonical_url(apply_url): continue
            location_text = item.get("location") or ""
            result.append(NormalizedJob(job_hash(self.name, str(item.get("id") or ""), apply_url, title, company, None), self.name, str(item.get("id") or "") or None, title, company, None, location_text, query.normalized_country() or normalize_country(location_text), location_text.split(",")[0].strip() or None, "remote" in f"{title} {location_text}".casefold(), item.get("type"), classify_category(title, item.get("snippet") or ""), None, None, None, clean_html(item.get("snippet")), canonical_url(apply_url), parse_datetime(item.get("updated")), utc_now(), item, [self.name]))
        return [job for job in result if _matches_query(job, query)]


class JobSpyProvider(JobProvider):
    name = "jobspy"
    enabled_setting = "enable_jobspy"
    min_interval_seconds = 3.0

    async def search(self, query: JobQuery) -> list[NormalizedJob]:
        try:
            from jobspy import scrape_jobs
        except ImportError as exc:
            raise ProviderUnavailable("python-jobspy is not installed") from exc
        sites = ["linkedin", "indeed", "glassdoor", "google", "zip_recruiter"]
        proxies = [item.strip() for item in settings.jobspy_proxies.split(",") if item.strip()]
        kwargs = {"site_name": sites, "search_term": query.keywords, "location": query.city or query.country or "United Kingdom", "results_wanted": min(query.page_size, 100), "is_remote": query.remote_only, "hours_old": (query.posted_within_days or 0) * 24 or None, "proxies": proxies or None}
        def run():
            frame = scrape_jobs(**{key: value for key, value in kwargs.items() if value is not None})
            return frame.to_dict("records")
        try:
            rows = await asyncio.wait_for(asyncio.to_thread(run), timeout=settings.job_provider_timeout_seconds)
        except TimeoutError as exc:
            raise ProviderUnavailable("timed out") from exc
        result = []
        for item in rows:
            title, company, apply_url = str(item.get("title") or "Untitled job"), str(item.get("company") or "Unknown company"), item.get("job_url_direct") or item.get("job_url") or ""
            if not canonical_url(apply_url): continue
            location = str(item.get("location") or "")
            source = str(item.get("site") or self.name).casefold()
            result.append(NormalizedJob(job_hash(source, str(item.get("id") or ""), apply_url, title, company, None), source, str(item.get("id") or "") or None, title, company, item.get("company_url"), location, query.normalized_country() or normalize_country(location), location.split(",")[0].strip() or None, bool(item.get("is_remote")), str(item.get("job_type") or "") or None, classify_category(title, str(item.get("description") or "")), item.get("min_amount"), item.get("max_amount"), item.get("currency"), clean_html(item.get("description")), canonical_url(apply_url), parse_datetime(item.get("date_posted")), utc_now(), item, [source]))
        return [job for job in result if _matches_query(job, query)]


class RemoteJobsReferenceProvider(JobProvider):
    """The requested repo is code-only and deliberately has no endpoint to scrape."""
    name = "remotejobs_reference"
    enabled_setting = "enable_remotejobs_reference"

    async def search(self, query: JobQuery) -> list[NormalizedJob]:
        raise ProviderUnavailable("reference-only: the RemoteJobs repository publishes no stable jobs feed")


def providers() -> list[JobProvider]:
    return [HimalayasProvider(), RemotiveProvider(), ArbeitnowProvider(), JobicyProvider(), MuseProvider(), AdzunaProvider(), JoobleProvider(), JobSpyProvider(), RemoteJobsReferenceProvider()]


def _dedupe(items: list[NormalizedJob]) -> list[NormalizedJob]:
    grouped: dict[str, NormalizedJob] = {}
    fallback_keys: dict[str, str] = {}
    for job in items:
        url_key = canonical_url(job.apply_url)
        fallback_key = "|".join((job.title.casefold(), job.company_name.casefold(), (job.city or "").casefold()))
        # Direct employer URLs are most reliable.  A job-board listing with
        # the same title/company/location is a duplicate of that direct role;
        # two distinct direct employer links remain distinct vacancies.
        host = (urlparse(url_key or "").hostname or "").casefold()
        key = url_key or fallback_key
        if key not in grouped and fallback_key in fallback_keys:
            fallback_group_key = fallback_keys[fallback_key]
            fallback_host = (urlparse(grouped[fallback_group_key].apply_url).hostname or "").casefold()
            if not url_key or host in JOB_BOARD_DOMAINS or fallback_host in JOB_BOARD_DOMAINS:
                key = fallback_group_key
        existing = grouped.get(key)
        if not existing:
            grouped[key] = job
            if not url_key or host in JOB_BOARD_DOMAINS:
                fallback_keys.setdefault(fallback_key, key)
            continue
        existing.sources = sorted(set(existing.sources + job.sources + [job.source]))
        # Prefer a non-job-board direct employer URL, then fuller descriptions.
        current_host = (urlparse(existing.apply_url).hostname or "").casefold()
        new_host = (urlparse(job.apply_url).hostname or "").casefold()
        if (current_host in JOB_BOARD_DOMAINS and new_host not in JOB_BOARD_DOMAINS) or len(job.description) > len(existing.description):
            job.sources = sorted(set(existing.sources + job.sources + [existing.source]))
            grouped[key] = job
            if new_host not in JOB_BOARD_DOMAINS:
                fallback_keys[fallback_key] = key
    return list(grouped.values())


async def aggregate_search(query: JobQuery) -> tuple[list[NormalizedJob], list[ProviderStatus]]:
    selected = {name.casefold() for name in query.sources or []}
    active = [provider for provider in providers() if not selected or provider.name.casefold() in selected]
    statuses: list[ProviderStatus] = []
    tasks: list[tuple[JobProvider, str, bool, Any]] = []
    for provider in active:
        if not provider.enabled():
            statuses.append(ProviderStatus(provider.name, "skipped", "disabled by environment"))
            continue
        ok, reason = provider.configured()
        if not ok:
            statuses.append(ProviderStatus(provider.name, "skipped", reason))
            continue
        cache_key = f"{provider.name}:{query!r}"
        cached = _cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            statuses.append(ProviderStatus(provider.name, "ok", "cached", len(cached[1])))
            tasks.append((provider, cache_key, True, asyncio.sleep(0, result=cached[1])))
        else:
            tasks.append((provider, cache_key, False, provider.search(query)))
    outcomes = await asyncio.gather(*(task[3] for task in tasks), return_exceptions=True)
    all_jobs: list[NormalizedJob] = []
    for (provider, cache_key, cached, _), outcome in zip(tasks, outcomes):
        if isinstance(outcome, Exception):
            statuses.append(ProviderStatus(provider.name, "failed", str(outcome)[:240]))
            continue
        jobs = outcome
        if not cached:
            _cache[cache_key] = (time.monotonic() + max(settings.job_cache_ttl_seconds, 30), jobs)
            statuses.append(ProviderStatus(provider.name, "ok", "", len(jobs)))
        all_jobs.extend(jobs)
    merged = _dedupe(all_jobs)
    if query.salary_min:
        merged.sort(key=lambda job: (job.salary_max or job.salary_min or 0, job.posted_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    else:
        merged.sort(key=lambda job: job.posted_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    start = max(query.page - 1, 0) * query.page_size
    return merged[start:start + query.page_size], sorted(statuses, key=lambda item: item.name)


def is_job_board_domain(domain: str | None) -> bool:
    host = (domain or "").casefold().removeprefix("www.")
    return any(host == item or host.endswith("." + item) for item in JOB_BOARD_DOMAINS)


def _is_company_research_domain(domain: str | None) -> bool:
    host = (domain or "").casefold().removeprefix("www.")
    return bool(host) and not is_job_board_domain(host) and not any(host == item or host.endswith("." + item) for item in NON_EMPLOYER_DOMAINS)


def _company_tokens(company_name: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", company_name.casefold()) if len(token) > 2 and token not in {"the", "and", "for", "ltd", "limited", "inc", "llc", "plc", "group"}]


def _company_site_score(url: str, company_name: str) -> int:
    """Score only likely official-company results; never select a directory by accident."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if not _is_company_research_domain(host):
        return 0
    tokens = _company_tokens(company_name)
    if not tokens:
        return 0
    compact_host = re.sub(r"[^a-z0-9]", "", host)
    compact_name = "".join(tokens)
    if compact_name and compact_name in compact_host:
        return 100
    matches = sum(token in compact_host for token in tokens)
    # A search result is usable only when the host identifies every meaningful
    # part of the employer name. One token (for example "Cambridge") is not
    # enough and leads to unrelated sites.
    return 85 if matches == len(tokens) else 0


def _search_company_pages(company_name: str) -> list[str]:
    pages: list[str] = []
    seen: set[str] = set()
    for query in (f"{company_name} official company website", f"{company_name} careers"):
        for page in _search_web(query):
            canonical = canonical_url(page)
            if canonical and canonical not in seen:
                seen.add(canonical)
                pages.append(canonical)
    return pages


def _derived_company_sites(company_name: str) -> list[str]:
    """Conservative fallback for brands whose job board only exposes a name."""
    slug = "".join(_company_tokens(company_name))
    if len(slug) < 4:
        return []
    return [f"https://{slug}{suffix}" for suffix in (".com", ".co.uk", ".io", ".ai")]


def _page_mentions_company(text: str, company_name: str) -> bool:
    compact_text = re.sub(r"[^a-z0-9]", "", text.casefold())
    tokens = _company_tokens(company_name)
    return bool(tokens) and all(token in compact_text for token in tokens)


def company_domain(company_website: str | None, apply_url: str | None, company_name: str) -> tuple[str | None, str | None]:
    for value in (company_website, apply_url):
        parsed = urlparse(value or "")
        if parsed.hostname and _is_company_research_domain(parsed.hostname):
            return parsed.hostname.casefold().removeprefix("www."), value
    candidates = _search_company_pages(company_name)
    scored = sorted(((_company_site_score(candidate, company_name), candidate) for candidate in candidates), reverse=True)
    for score, candidate in scored:
        if not score:
            continue
        parsed = urlparse(candidate)
        if parsed.hostname:
            return parsed.hostname.casefold().removeprefix("www."), candidate
    # When a job board provides only a company name and search has no reliable
    # result, test the common company-domain forms. A domain is accepted only
    # when its own public page identifies the employer; this is not email
    # guessing and avoids sending to a similarly named site.
    for candidate in _derived_company_sites(company_name):
        try:
            source_url, text = _website_text(candidate)
        except (ValueError, httpx.HTTPError):
            continue
        parsed = urlparse(source_url)
        if parsed.hostname and _is_company_research_domain(parsed.hostname) and _page_mentions_company(text, company_name):
            return parsed.hostname.casefold().removeprefix("www."), source_url
    return None, None


def _robots_allows(url: str) -> bool:
    parsed = urlparse(url)
    try:
        robots_url = _public_url(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        response = httpx.get(robots_url, timeout=min(settings.job_provider_timeout_seconds, 10), headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        if response.status_code == 404:
            return True
        if response.status_code >= 400:
            return False
        rules: list[tuple[list[str], bool]] = []
        applies = False
        for line in response.text.splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip().casefold(), value.strip()
            if key == "user-agent":
                applies = value in {"*", USER_AGENT.casefold()}
            elif applies and key in {"allow", "disallow"} and value:
                rules.append(([value], key == "allow"))
        path = parsed.path or "/"
        matching = [rule for rule in rules if path.startswith(rule[0][0])]
        return not matching or max(matching, key=lambda item: len(item[0][0]))[1]
    except (httpx.HTTPError, ValueError):
        # Public fallback pages are only fetched when robots cannot be obtained;
        # failures are not interpreted as permission to bypass an explicit rule.
        return False


def verify_mx(email: str) -> bool:
    """Syntax + DNS MX validation only; never SMTP probing."""
    _, address = parseaddr(email)
    if address.casefold() != email.casefold() or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        return False
    try:
        import dns.resolver
        return bool(dns.resolver.resolve(address.rsplit("@", 1)[1], "MX", lifetime=4))
    except Exception:
        return False


def rank_contact(email: str, domain: str) -> tuple[int, str]:
    local, found_domain = email.casefold().split("@", 1)
    if found_domain != domain.casefold():
        return 0, "personal"
    if local in ROLE_CONTACTS:
        return 100 - ROLE_CONTACTS.index(local), "role"
    if local in GENERIC_CONTACTS:
        return 55, "generic"
    # Published regional/team aliases such as careers-uk@ or
    # recruiting.emea@ are role mailboxes, not individual addresses.
    normalized = re.sub(r"[^a-z]", "", local)
    if any(term in normalized for term in ("career", "recruit", "talent", "hiring", "job", "humanresources")):
        return 80, "role"
    return 0, "personal"


def find_public_company_contacts(
    company_name: str,
    company_website: str | None,
    apply_url: str | None,
    job_description: str | None = None,
) -> list[dict[str, Any]]:
    """Respect robots and return only public generic/role addresses with sources."""
    results: dict[str, dict[str, Any]] = {}

    def add_contacts(text: str, domain: str, source_url: str) -> None:
        for email in {match.casefold() for match in EMAIL_RE.findall(text.replace("\\u003e", ">"))}:
            confidence, kind = rank_contact(email, domain)
            if not confidence:
                continue
            record = {
                "email": email, "domain": domain, "source_url": source_url,
                "kind": kind, "confidence": confidence, "verified_mx": verify_mx(email),
            }
            previous = results.get(email)
            if not previous or (record["confidence"], record["verified_mx"]) > (previous["confidence"], previous["verified_mx"]):
                results[email] = record

    # Some job boards copy a public recruiting mailbox into the listing. It is
    # already job-specific evidence, so accept only public role/generic aliases
    # with a real MX domain; never a person's mailbox.
    if job_description and apply_url:
        for email in {match.casefold() for match in EMAIL_RE.findall(job_description.replace("\\u003e", ">"))}:
            email_domain = email.rsplit("@", 1)[1]
            add_contacts(email, email_domain, apply_url)
    if results:
        return sorted(results.values(), key=lambda item: (item["confidence"], item["verified_mx"]), reverse=True)

    domain, site = company_domain(company_website, apply_url, company_name)
    if not domain or not site:
        return sorted(results.values(), key=lambda item: (item["confidence"], item["verified_mx"]), reverse=True)
    parsed = urlparse(site)
    root = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [root, *[urljoin(root + "/", path) for path in ("contact", "careers", "jobs", "about", "team", "join-us", "work-with-us", "contact-us")]]
    def scan_pages(pages: list[str]) -> None:
        for page in pages:
            if not _robots_allows(page):
                continue
            try:
                source_url, text = _website_text(page)
            except (ValueError, httpx.HTTPError):
                continue
            add_contacts(text, domain, source_url)

    scan_pages(list(dict.fromkeys(candidates)))
    if not results:
        # Search-result pages are a fallback only after the official domain has
        # been established, and only when they stay on that same public domain.
        fallback_pages = []
        for page in _search_company_pages(company_name):
            host = (urlparse(page).hostname or "").casefold().removeprefix("www.")
            if (host == domain or host.endswith("." + domain)) and page not in candidates:
                fallback_pages.append(page)
        scan_pages(fallback_pages)
    return sorted(results.values(), key=lambda item: (item["confidence"], item["verified_mx"]), reverse=True)
