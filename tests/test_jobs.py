import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.jobs as jobs
import app.main as main
from app.db import Base, SessionLocal, engine
from app.models import Campaign, Candidate, CompanyContact, Job, Suppression
from app.tenancy import ensure_default_workspace


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    monkeypatch.setattr(main.settings, "environment", "development")
    monkeypatch.setattr(main.settings, "admin_username", "")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def normalized(source="fixture", apply_url="https://employer.example/apply", title="Warehouse Operative", company="Acme", city="London"):
    return jobs.NormalizedJob(
        id=jobs.job_hash(source, "1", apply_url, title, company, city), source=source, source_job_id="1",
        title=title, company_name=company, company_website="https://employer.example", location_text=f"{city}, United Kingdom",
        country_code="GB", city=city, is_remote=False, job_type="Full Time", category="Physical / frontline",
        salary_min=25000, salary_max=30000, salary_currency="GBP", description="Public job description", apply_url=apply_url,
        posted_at=jobs.utc_now(), fetched_at=jobs.utc_now(), raw={"id": 1}, sources=[source],
    )


def test_country_normalization():
    assert jobs.normalize_country("United Kingdom") == "GB"
    assert jobs.normalize_country("Berlin, Germany") == "DE"
    assert jobs.normalize_country("bd") == "BD"


def test_deduplicate_prefers_employer_apply_link():
    board = normalized("remotive", "https://remotive.com/remote-jobs/acme")
    direct = normalized("adzuna", "https://employer.example/careers/warehouse")
    merged = jobs._dedupe([board, direct])
    assert len(merged) == 1
    assert merged[0].apply_url == "https://employer.example/careers/warehouse"
    assert set(merged[0].sources) == {"remotive", "adzuna"}


def test_partial_provider_failure_is_returned(monkeypatch):
    class Good(jobs.JobProvider):
        name = "good"
        enabled_setting = "enable_himalayas"
        async def search(self, query): return [normalized("good")]
    class Bad(jobs.JobProvider):
        name = "bad"
        enabled_setting = "enable_himalayas"
        async def search(self, query): raise jobs.ProviderUnavailable("rate limited (429)")
    monkeypatch.setattr(jobs, "providers", lambda: [Good(), Bad()])
    results, statuses = asyncio.run(jobs.aggregate_search(jobs.JobQuery(keywords="warehouse")))
    assert len(results) == 1
    assert {item.name: item.status for item in statuses} == {"good": "ok", "bad": "failed"}


def test_persist_jobs_bounds_provider_fields_and_normalizes_non_finite_json():
    listing = normalized()
    listing.location_text = "L" * 700
    listing.source_job_id = "I" * 400
    listing.company_website = "https://employer.example/" + ("p" * 1_100)
    listing.raw = {"score": float("nan"), "nested": [float("inf"), {"value": float("-inf")}]} 
    statuses = [jobs.ProviderStatus("fixture", "ok", count=1)]

    with SessionLocal() as db:
        workspace, user = ensure_default_workspace(db)
        persisted = main._persist_jobs(
            db,
            workspace,
            user,
            jobs.JobQuery(keywords="warehouse"),
            [listing],
            statuses,
        )
        db.commit()
        stored = db.get(Job, persisted[0].id)

    assert len(stored.location_text) == 500
    assert len(stored.source_job_id) == 255
    assert len(stored.company_website) == 1_000
    assert stored.raw == {"score": None, "nested": [None, {"value": None}]}
    assert all(status.name != "storage" for status in statuses)


def test_persist_jobs_skips_one_malformed_listing_without_aborting_page():
    valid = normalized(source="valid")
    malformed = normalized(source="broken", apply_url="https://employer.example/broken")
    malformed.id = ""
    statuses = [jobs.ProviderStatus("fixture", "ok", count=2)]

    with SessionLocal() as db:
        workspace, user = ensure_default_workspace(db)
        persisted = main._persist_jobs(
            db,
            workspace,
            user,
            jobs.JobQuery(keywords="warehouse"),
            [valid, malformed],
            statuses,
        )
        db.commit()
        total = db.query(Job).count()

    assert len(persisted) == 1
    assert total == 1
    assert statuses[-1].public() == {
        "name": "storage",
        "status": "partial",
        "message": "Skipped 1 malformed listing(s).",
        "count": 1,
    }


def test_contact_ranking_and_public_domain_only():
    assert jobs.rank_contact("careers@acme.example", "acme.example")[1] == "role"
    assert jobs.rank_contact("recruiting-emea@acme.example", "acme.example")[1] == "role"
    assert jobs.rank_contact("person@acme.example", "acme.example")[0] == 0
    assert jobs.is_job_board_domain("www.linkedin.com") is True
    assert jobs.is_job_board_domain("careers.acme.example") is False


def test_company_domain_uses_only_name_matched_official_search_results(monkeypatch):
    monkeypatch.setattr(jobs, "_search_web", lambda query: [
        "https://directory.example/acme",
        "https://acme.example/careers",
    ])
    domain, site = jobs.company_domain(None, "https://linkedin.com/jobs/123", "Acme")
    assert domain == "acme.example"
    assert site == "https://acme.example/careers"


def test_company_domain_verifies_a_derived_company_site(monkeypatch):
    monkeypatch.setattr(jobs, "_search_web", lambda query: [])
    monkeypatch.setattr(jobs, "_website_text", lambda url: (url, "Welcome to Acme. Acme builds software."))
    domain, site = jobs.company_domain(None, "https://linkedin.com/jobs/123", "Acme")
    assert domain == "acme.com"
    assert site == "https://acme.com"


def test_contact_research_uses_public_role_address_from_job_listing(monkeypatch):
    monkeypatch.setattr(jobs, "company_domain", lambda *args: (None, None))
    monkeypatch.setattr(jobs, "verify_mx", lambda email: email == "recruiting-emea@acme.example")
    found = jobs.find_public_company_contacts(
        "Acme", None, "https://jobs.example/acme", "Apply at recruiting-emea@acme.example. Ask jane@acme.example for help.",
    )
    assert [item["email"] for item in found] == ["recruiting-emea@acme.example"]
    assert found[0]["source_url"] == "https://jobs.example/acme"


def test_contact_research_stores_only_ranked_public_addresses(monkeypatch):
    monkeypatch.setattr(jobs, "company_domain", lambda *args: ("acme.example", "https://acme.example"))
    monkeypatch.setattr(jobs, "_robots_allows", lambda url: True)
    monkeypatch.setattr(jobs, "_website_text", lambda url: (url, "careers@acme.example person@acme.example hello@acme.example"))
    monkeypatch.setattr(jobs, "verify_mx", lambda email: email.startswith("careers"))
    found = jobs.find_public_company_contacts("Acme", "https://acme.example", "https://board.example/job")
    assert [item["email"] for item in found] == ["careers@acme.example", "hello@acme.example"]
    assert found[0]["verified_mx"] is True


def test_job_endpoints_persist_and_prepare_review_campaign(monkeypatch):
    async def search(_query):
        return [normalized()], [jobs.ProviderStatus("fixture", "ok", count=1)]
    monkeypatch.setattr(main, "aggregate_search", search)
    monkeypatch.setattr(main, "find_public_company_contacts", lambda *args: [])
    with SessionLocal() as db:
        workspace, user = ensure_default_workspace(db)
        candidate = Candidate(workspace_id=workspace.id, user_id=user.id, name="Person", email="person@example.com", cv_yaml="cv:\n  name: Person\n  email: person@example.com\n  sections: {}\n")
        db.add(candidate)
        db.commit()
        candidate_id = candidate.id
    monkeypatch.setattr(main, "enqueue_job", lambda *args: 55)
    with TestClient(main.app, base_url="http://localhost") as client:
        search_result = client.get("/api/jobs/search", params={"keywords": "warehouse"})
        assert search_result.status_code == 200
        job_id = search_result.json()["jobs"][0]["id"]
        found = client.post(f"/api/jobs/{job_id}/find-contact")
        # No network was requested: this behavior is tested above. It may have no contact here.
        assert found.status_code == 200
        prepared = client.post(f"/api/jobs/{job_id}/prepare-application", data={"candidate_id": candidate_id, "recipient_email": "careers@acme.example"})
        assert prepared.status_code == 200
        campaign_id = prepared.json()["campaign_id"]
        suppressed = client.post(f"/api/jobs/{job_id}/suppress", data={"email": "careers@acme.example"})
        assert suppressed.status_code == 200
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign.job_id == job_id
        assert campaign.status == "queued"
        assert db.query(Suppression).filter_by(email="careers@acme.example").one().reason == "job_contact_opt_out"


def test_send_blocks_company_domain_during_cooldown(monkeypatch):
    with SessionLocal() as db:
        workspace, user = ensure_default_workspace(db)
        candidate = Candidate(workspace_id=workspace.id, user_id=user.id, name="Person", email="person@example.com", cv_yaml="cv:\n  name: Person\n  email: person@example.com\n  sections:\n    skills:\n      - label: Skills\n        details: Python\n    experience:\n      - company: Example\n        position: Engineer\n")
        db.add(candidate)
        db.flush()
        db.add(Campaign(workspace_id=workspace.id, user_id=user.id, candidate_id=candidate.id, company_name="Previous", recipient_email="careers@acme.example", status="sent", sent_at=jobs.utc_now() - timedelta(days=1)))
        db.commit()
    monkeypatch.setattr(main.settings, "contact_cooldown_days", 30)
    with SessionLocal() as db:
        workspace, user = ensure_default_workspace(db)
        candidate = db.query(Candidate).first()
        campaign = Campaign(workspace_id=workspace.id, user_id=user.id, candidate_id=candidate.id, company_name="Acme", recipient_email="jobs@acme.example", status="ready", research={"sources": [{"url": "https://acme.example", "content": "jobs@acme.example"}]}, tailored_yaml="cv: {}", email_subject="Acme", email_body="Acme", artifact_path="missing.pdf")
        db.add(campaign)
        db.commit()
        campaign_id = campaign.id
    with TestClient(main.app, base_url="http://localhost") as client:
        response = client.post(f"/campaigns/{campaign_id}/send", data={"approved": "true"})
    assert response.status_code == 400
    assert "cooldown" in response.json()["detail"]
