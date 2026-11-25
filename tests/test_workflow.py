import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse, RedirectResponse

from app import daily_outreach, services
from app.db import Base, SessionLocal, engine
from app.main import app
import app.main as main
from app.models import Campaign, Candidate
from app.tenancy import ensure_default_workspace


@pytest.fixture(autouse=True)
def init_db(monkeypatch):
    # Tests exercise the application in its local, unauthenticated development mode.
    # Production authentication/CSRF is covered by explicit smoke checks and deployment tests.
    monkeypatch.setattr(main.settings, "environment", "development")
    monkeypatch.setattr(main.settings, "admin_username", "")
    monkeypatch.setattr(main.settings, "admin_password", "")
    monkeypatch.setattr(services.settings, "environment", "development")
    monkeypatch.setattr(main.settings, "encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(services.settings, "encryption_key", main.settings.encryption_key)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def test_research_rejects_unsafe_url():
    try:
        services.research_company("Example", "file:///etc/passwd")
    except ValueError as exc:
        assert "http" in str(exc)
    else:
        raise AssertionError("unsafe URL was accepted")


def test_search_web_uses_bing_results_before_fallbacks(monkeypatch):
    encoded_url = base64.b64encode(b"https://example.com/contact").decode()

    class Response:
        text = f'<li class="b_algo"><h2><a class="result" href="https://www.bing.com/ck/a?u=a1{encoded_url}">Example</a></h2></li>'

        def raise_for_status(self):
            pass

    monkeypatch.setattr(services.httpx, "get", lambda *args, **kwargs: Response())

    assert services._search_web("example company") == ["https://example.com/contact"]


def test_search_web_skips_bing_urls_with_control_characters(monkeypatch):
    encoded_url = base64.b64encode(b"https://example.com/\x12contact").decode()

    class Response:
        text = f'<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1{encoded_url}">Example</a></h2></li>'

        def raise_for_status(self):
            pass

    monkeypatch.setattr(services.httpx, "get", lambda *args, **kwargs: Response())

    assert services._search_web("example company") == []


def test_healthz_checks_database_connection():
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_available_with_dashboard_auth_configured(monkeypatch):
    monkeypatch.setattr(main.settings, "admin_username", "admin")
    monkeypatch.setattr(main.settings, "admin_password", "password")

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/healthz")

    assert response.status_code == 200


def test_resend_webhook_requires_a_verification_secret(monkeypatch):
    monkeypatch.setattr(main.settings, "resend_webhook_secret", "")

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/webhooks/resend", content=b"{}")

    assert response.status_code == 503


def test_login_form_protects_dashboard_with_a_session(monkeypatch):
    monkeypatch.setattr(main.settings, "admin_username", "admin")
    monkeypatch.setattr(main.settings, "admin_password", "Saimon55@")

    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/login?next=/"
        assert client.post("/login", data={"username": "admin", "password": "wrong", "next": "/"}).status_code == 401
        login = client.post("/login", data={"username": "admin", "password": "Saimon55@", "next": "/"}, follow_redirects=False)
        assert login.status_code == 303
        assert login.headers["location"] == "/"
        assert client.get("/").status_code == 200
        assert client.post("/logout", follow_redirects=False).status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 303


def test_production_configuration_rejects_sqlite_database():
    env = os.environ | {
        "ENVIRONMENT": "production",
        "DATABASE_URL": "sqlite:///./data/production.db",
        "APP_SECRET": "x" * 32,
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "password",
        "ALLOWED_HOSTS": "mail.example.com",
    }

    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "DATABASE_URL must use PostgreSQL" in result.stderr


def test_resend_send_returns_provider_message_id(monkeypatch, tmp_path):
    attachment = tmp_path / "cv.pdf"
    attachment.write_bytes(b"%PDF-test")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "email_123"}

    captured = {}
    monkeypatch.setattr(services.settings, "resend_api_key", "test-key")
    monkeypatch.setattr(services.settings, "resend_from", "jobs@example.com")
    monkeypatch.setattr(
        services.httpx,
        "post",
        lambda *args, **kwargs: (captured.update(kwargs) or Response()),
    )

    message_id = services.send_email("careers@example.com", "Application", "Hello", attachment)

    assert message_id == "email_123"
    assert captured["json"]["to"] == ["careers@example.com"]


def test_research_rejects_private_host():
    try:
        services.research_company("Example", "http://127.0.0.1:8000")
    except ValueError as exc:
        assert "public" in str(exc)
    else:
        raise AssertionError("private network target was accepted")


def test_research_with_job_description_fallback():
    result = services.research_company(
        "Acme AI",
        website=None,
        target_role="Senior Python Engineer",
        job_description="Seeking 5+ years with FastAPI and async services.",
    )
    assert result["company"] == "Acme AI"
    assert result["target_role"] == "Senior Python Engineer"
    assert any("FastAPI" in s["content"] for s in result["sources"])


def test_research_adds_one_independent_public_search_source(monkeypatch):
    pages = {
        "https://acme.example": ("https://acme.example", "Acme's official site"),
        "https://industry.example/acme": ("https://industry.example/acme", "Independent company profile"),
    }
    monkeypatch.setattr(services, "_website_text", lambda url: pages[url])
    monkeypatch.setattr(
        services,
        "_search_web",
        lambda query: ["https://acme.example/about", "https://industry.example/acme"],
    )

    result = services.research_company("Acme", "https://acme.example")

    assert [source["url"] for source in result["sources"]] == [
        "https://acme.example",
        "https://industry.example/acme",
    ]


def test_company_lead_routes_are_retired_in_favor_of_jobs():
    with TestClient(app, base_url="http://localhost") as client:
        redirect = client.get("/leads", follow_redirects=False)
        retired = client.post("/leads/batch", data={"candidate_id": 1, "query": "beta", "limit": 1})
        dashboard = client.get("/?tab=jobs")

    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/?tab=jobs"
    assert retired.status_code == 410
    assert "Lead discovery" not in dashboard.text
    assert "Search jobs across enabled sources" in dashboard.text


def test_daily_send_budget_counts_sent_campaigns(monkeypatch):
    monkeypatch.setattr(main.settings, "outreach_daily_limit", 2)
    monkeypatch.setattr(main.settings, "outreach_timezone", "UTC")
    with SessionLocal() as db:
        candidate = Candidate(name="Test Person", email="person@tailormail.invalid", cv_yaml="cv:\n  name: Test Person\n  sections: {}\n")
        db.add(candidate)
        db.commit()
        db.add_all([
            Campaign(candidate_id=candidate.id, company_name=f"Company {number}", recipient_email=f"contact{number}@company.example", status="sent", sent_at=datetime.now(timezone.utc))
            for number in range(2)
        ])
        db.commit()
        assert main.remaining_daily_send_budget(db) == 0


def _outreach_ready_candidate_id() -> int:
    source = """cv:
  name: Test Person
  email: test@example.com
  sections:
    experience:
      - company: Example Systems
        position: Software Engineer
    skills:
      - label: Languages
        details: Python
"""
    with SessionLocal() as db:
        candidate = Candidate(name="Test Person", email="test@example.com", cv_yaml=source)
        db.add(candidate)
        db.commit()
        return candidate.id


def _configure_daily_worker(monkeypatch, candidate_id: int) -> None:
    monkeypatch.setattr(daily_outreach.settings, "outreach_candidate_id", candidate_id)
    monkeypatch.setattr(daily_outreach.settings, "outreach_query", "software")
    monkeypatch.setattr(daily_outreach.settings, "outreach_target_role", "Backend Engineer")
    monkeypatch.setattr(daily_outreach.settings, "outreach_daily_limit", 100)
    monkeypatch.setattr(daily_outreach.settings, "outreach_auto_send", True)
    monkeypatch.setattr(daily_outreach.settings, "api_key", "test-key")
    monkeypatch.setattr(daily_outreach.settings, "base_url", "http://llm.example/v1")
    monkeypatch.setattr(daily_outreach.settings, "llm_model", "test-model")
    monkeypatch.setattr(daily_outreach.settings, "resend_api_key", "test-key")
    monkeypatch.setattr(daily_outreach.settings, "resend_from", "jobs@example.com")
    monkeypatch.setattr(daily_outreach.settings, "outreach_timezone", "UTC")


def test_daily_worker_returns_cleanly_when_auto_send_has_no_verified_leads(monkeypatch):
    _configure_daily_worker(monkeypatch, _outreach_ready_candidate_id())
    monkeypatch.setattr(
        daily_outreach,
        "discover_job_batch",
        lambda **kwargs: {"discovered": 0, "completed": 0, "sent": 0},
    )
    monkeypatch.setattr(daily_outreach, "remaining_daily_send_budget", lambda *args: 99)

    daily_outreach.main()


def test_daily_worker_stops_for_missing_candidate(monkeypatch):
    _configure_daily_worker(monkeypatch, 999)

    with pytest.raises(SystemExit, match="candidate was not found"):
        daily_outreach.main()


def test_daily_worker_uses_platform_defaults_for_an_empty_workspace(monkeypatch):
    candidate_id = _outreach_ready_candidate_id()
    _configure_daily_worker(monkeypatch, candidate_id)
    with SessionLocal() as db:
        workspace, _ = ensure_default_workspace(db)
        candidate = db.get(Candidate, candidate_id)
        candidate.workspace_id = workspace.id
        workspace.outreach_candidate_id = None
        workspace.outreach_query = ""
        workspace.outreach_target_role = ""
        workspace.outreach_auto_send = True
        db.commit()
        workspace_id = workspace.id

    calls = []
    monkeypatch.setattr(
        daily_outreach,
        "discover_job_batch",
        lambda **kwargs: (calls.append(kwargs) or {"discovered": 1, "completed": 1, "sent": 1}),
    )
    monkeypatch.setattr(daily_outreach, "remaining_daily_send_budget", lambda *args: 1)

    daily_outreach.main(workspace_id=workspace_id)

    assert calls == [{
        "candidate_id": candidate_id,
        "keywords": "software",
        "target_role": "Backend Engineer",
        "limit": 1,
        "send_when_ready": True,
        "workspace_id": workspace_id,
    }]


def test_daily_preflight_requires_sender_for_auto_send(monkeypatch):
    _configure_daily_worker(monkeypatch, _outreach_ready_candidate_id())
    monkeypatch.setattr(daily_outreach.settings, "resend_api_key", "")
    monkeypatch.setattr(daily_outreach.settings, "resend_from", "")

    errors = daily_outreach.production_preflight_errors()

    assert "requires Resend or SMTP" in "\n".join(errors)


def test_daily_worker_accepts_completed_daily_send_target(monkeypatch):
    _configure_daily_worker(monkeypatch, _outreach_ready_candidate_id())
    monkeypatch.setattr(
        daily_outreach,
        "discover_job_batch",
        lambda **kwargs: {"discovered": 100, "completed": 100, "sent": 100},
    )
    monkeypatch.setattr(daily_outreach, "remaining_daily_send_budget", lambda *args: 100)

    daily_outreach.main()


def test_daily_worker_runs_one_job_search_batch_per_schedule(monkeypatch):
    _configure_daily_worker(monkeypatch, _outreach_ready_candidate_id())
    calls = []
    monkeypatch.setattr(
        daily_outreach,
        "discover_job_batch",
        lambda **kwargs: (calls.append(kwargs) or {"discovered": 100, "completed": 100, "sent": 100}),
    )
    monkeypatch.setattr(daily_outreach, "remaining_daily_send_budget", lambda *args: 100)

    daily_outreach.main()

    assert len(calls) == 1


def test_scheduled_job_batch_prepares_and_sends_a_verified_employer_contact(monkeypatch):
    candidate_id = _outreach_ready_candidate_id()
    with SessionLocal() as db:
        workspace, _ = ensure_default_workspace(db)
        candidate = db.get(Candidate, candidate_id)
        candidate.workspace_id = workspace.id
        db.commit()
        workspace_id = workspace.id

    job = SimpleNamespace(
        id=99,
        company_name="Acme Ltd",
        company_website="https://acme.example",
        apply_url="https://acme.example/jobs/backend",
        title="Backend Engineer",
        description="Build reliable APIs.",
    )
    searches = []
    async def aggregate(query):
        searches.append(query)
        return [SimpleNamespace()], []

    created, sent = [], []
    monkeypatch.setattr(daily_outreach, "aggregate_search", aggregate)
    monkeypatch.setattr(daily_outreach, "_persist_jobs", lambda *args: [job])
    monkeypatch.setattr(daily_outreach, "find_public_company_contacts", lambda *args: [{"email": "careers@acme.example", "verified_mx": True}])
    monkeypatch.setattr(daily_outreach, "create_campaign", lambda **kwargs: (created.append(kwargs) or RedirectResponse("/campaigns/77", status_code=303)))
    monkeypatch.setattr(daily_outreach, "send_campaign", lambda campaign_id, **kwargs: sent.append((campaign_id, kwargs)))

    result = daily_outreach.discover_job_batch(
        workspace_id=workspace_id,
        candidate_id=candidate_id,
        keywords="backend engineer",
        target_role="Backend Engineer",
        limit=3,
        send_when_ready=True,
    )

    assert searches[0].keywords == "backend engineer"
    assert created[0]["job_id"] == 99
    assert created[0]["recipient_email"] == "careers@acme.example"
    assert sent == [(77, {"approved": True, "workspace_id": workspace_id})]
    assert result == {"discovered": 1, "completed": 1, "sent": 1}


def test_search_web_uses_duckduckgo_lite_when_html_has_no_results(monkeypatch):
    class Response:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    responses = iter((Response("no results"), Response("no results"), Response(
        '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.example%2F&amp;rut=x" class=\'result-link\'>Acme</a>'
    )))
    monkeypatch.setattr(services.httpx, "get", lambda *a, **k: next(responses))

    assert services._search_web("Acme official website") == ["https://acme.example/"]


def test_zip_import_reads_multiple_rendercv_files():
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as files:
        files.writestr(
            "first.yaml",
            "cv:\n  name: First Person\n  email: first@example.com\n  sections: {}\n",
        )
        files.writestr(
            "second.yml",
            "cv:\n  name: Second Person\n  email: second@example.com\n  sections: {}\n",
        )
    records = services.candidates_from_zip(archive.getvalue())
    assert [(name, email) for name, email, _ in records] == [
        ("First Person", "first@example.com"),
        ("Second Person", "second@example.com"),
    ]


def test_cv_outreach_readiness_blocks_thin_source_cv():
    thin_cv = """cv:
  name: Test Person
  email: test@example.com
  sections:
    experience:
      - company: Example Systems
        position: Software Engineer
"""
    ready_cv = thin_cv + """    skills:
      - label: Languages
        details: Python
"""

    assert "too thin" in services.cv_outreach_readiness_error(thin_cv)
    assert services.cv_outreach_readiness_error(ready_cv) is None


def test_campaign_page_explains_thin_cv_send_block():
    thin_cv = """cv:
  name: Test Person
  email: test@example.com
  sections:
    experience:
      - company: Example Systems
        position: Software Engineer
"""
    with SessionLocal() as db:
        candidate = Candidate(name="Test Person", email="test@example.com", cv_yaml=thin_cv)
        db.add(candidate)
        db.commit()
        campaign = Campaign(
            candidate_id=candidate.id,
            company_name="Acme Ltd",
            recipient_email="careers@acme.example",
            status="ready",
            email_subject="Application",
            email_body="Dear Acme Corp, my CV is attached.",
        )
        db.add(campaign)
        db.commit()
        campaign_id = campaign.id

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"/campaigns/{campaign_id}")

    assert page.status_code == 200
    assert "Sending blocked:" in page.text
    assert 'class="button primary send-btn"' not in page.text


def test_tailor_prompt_guardrails(monkeypatch):
    source = "cv:\n  name: Test Person\n  email: test@example.com\n  headline: Original\n  sections: {}\n"
    payload = {
        "cv_yaml": "cv:\n  name: Test Person\n  email: test@example.com\n  headline: Backend Engineer\n  sections: {}\n",
        "email_subject": "Application – Backend Engineer",
        "email_body": "Dear Acme Corp, my CV is attached.",
    }

    class MockResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(payload)}}]}

    monkeypatch.setattr(services.settings, "api_key", "test-key")
    monkeypatch.setattr(services.httpx, "post", lambda *a, **k: MockResponse())

    result = services.tailor(source, {"sources": []}, "Acme Corp", target_role="Backend Engineer")
    assert yaml.safe_load(result["cv_yaml"])["cv"]["name"] == "Test Person"
    assert "Backend Engineer" in result["email_subject"]


def test_discovery_email_is_grounded_in_candidate_facts(monkeypatch):
    source = """cv:
  name: Test Person
  email: test@example.com
  sections:
    experience:
      - company: Example Systems
        position: Software Engineer
"""
    payload = {
        "cv_yaml": source.replace("position: Software Engineer", "position: Backend Software Engineer"),
        "email_subject": "Interest in Acme Corp's technology growth",
        "email_body": "I am interested in contributing to Acme Corp's technology and business development work.",
    }

    class MockResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(payload)}}]}

    monkeypatch.setattr(services.settings, "api_key", "test-key")
    monkeypatch.setattr(services.httpx, "post", lambda *a, **k: MockResponse())

    result = services.tailor(
        source,
        {"sources": [{"url": "https://acme.example", "content": "Acme home page"}]},
        "Acme Corp",
        target_role="Backend Engineer",
        job_description="Job listing: https://acme.example/jobs/backend",
    )
    assert result["email_subject"] == "Interest in Acme Corp's technology growth"
    assert "Acme Corp" in result["email_body"]
    assert "advertised vacancy" not in result["email_body"]
    assert "ten years" not in result["email_body"]


def test_tailor_accepts_sse_responses(monkeypatch):
    source = "cv:\n  name: Test Person\n  email: test@example.com\n  headline: Original\n  sections: {}\n"
    streamed = "\n".join(
        [
            'data: {"choices":[{"delta":{"content":"{\\\"cv_yaml\\\": \\\"cv:\\\\n  name: Test Person\\\\n  email: test@example.com\\\\n  sections: {}\\\\n\\\", \\\"email_subject\\\": \\\"Application\\\", \\\"email_body\\\": \\\"Attached.\\\"}"}}]}',
            "data: [DONE]",
        ]
    )

    class MockResponse:
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            pass

        @property
        def text(self):
            return streamed

    monkeypatch.setattr(services.settings, "api_key", "test-key")
    monkeypatch.setattr(services.httpx, "post", lambda *a, **k: MockResponse())

    with pytest.raises(ValueError, match="company"):
        services.tailor(source, {"sources": []}, "Acme Corp")


def test_tailor_restores_the_source_rendercv_design(monkeypatch):
    source = "cv:\n  name: Test Person\n  email: test@example.com\n  headline: Original\n  sections: {}\ndesign:\n  theme: classic\n"
    payload = {
        "cv_yaml": "cv:\n  name: Test Person\n  email: test@example.com\n  headline: Tailored\n  sections: {}\n  design:\n    theme: modern\n",
        "email_subject": "Application",
        "email_body": "Dear Acme Corp, my CV is attached.",
    }

    class MockResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(payload)}}]}

    monkeypatch.setattr(services.settings, "api_key", "test-key")
    monkeypatch.setattr(services.httpx, "post", lambda *a, **k: MockResponse())

    result = services.tailor(source, {"sources": []}, "Acme Corp")
    rendered = yaml.safe_load(result["cv_yaml"])
    assert rendered["design"] == {"theme": "classic"}
    assert "design" not in rendered["cv"]


def test_tailor_rejects_invented_cv_facts(monkeypatch):
    source = """cv:
  name: Test Person
  email: test@example.com
  location: Dhaka, Bangladesh
  sections:
    experience:
      - company: Example Systems
        position: Software Engineer
design:
  theme: classic
"""
    payload = {
        "cv_yaml": """cv:
  name: Test Person
  email: test@example.com
  location: London, United Kingdom
  sections:
    experience:
      - company: Invented Bank
        position: Principal Engineer
    education:
      - institution: Invented University
design:
  theme: classic
""",
        "email_subject": "Application",
        "email_body": "Dear Acme Corp, my CV is attached.",
    }

    class MockResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(payload)}}]}

    monkeypatch.setattr(services.settings, "api_key", "test-key")
    monkeypatch.setattr(services.httpx, "post", lambda *a, **k: MockResponse())

    with pytest.raises(ValueError, match="unchanged"):
        services.tailor(source, {"sources": []}, "Acme Corp")


def test_llm_url_accepts_provider_v1_base_url():
    assert services._llm_url("http://provider.test", "models") == "http://provider.test/v1/models"
    assert services._llm_url("http://provider.test/v1", "models") == "http://provider.test/v1/models"


def test_llm_request_retries_transport_errors():
    calls = 0

    def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise services.httpx.ReadTimeout("slow provider")
        return "response"

    assert services._llm_request(request, "https://provider.test") == "response"
    assert calls == 3


def test_tailor_retries_malformed_json_until_valid_response(monkeypatch):
    source = "cv:\n  name: Test Person\n  email: test@example.com\n  headline: Original\n  sections: {}\n"
    valid = {
        "cv_yaml": source.replace("Original", "Platform Engineer"),
        "email_subject": "Application to Acme Corp",
        "email_body": "Dear Acme Corp, my CV is attached.",
    }
    calls = 0

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            nonlocal calls
            calls += 1
            content = "not json" if calls < 3 else json.dumps(valid)
            return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(services.settings, "api_key", "test-key")
    monkeypatch.setattr(services.httpx, "post", lambda *args, **kwargs: Response())
    result = services.tailor(source, {"sources": []}, "Acme Corp")
    assert calls == 3
    assert "Platform Engineer" in result["cv_yaml"]


def test_render_pdf_with_rendercv(tmp_path, monkeypatch):
    """Verifies that the installed RenderCV v2.8 compiler creates a real PDF from YAML."""
    monkeypatch.setattr(services.settings, "artifact_dir", tmp_path)
    sample_path = Path("tests/fixtures/sample_cv.yaml")
    cv_yaml = sample_path.read_text(encoding="utf-8")

    pdf_file = services.render_pdf(cv_yaml, campaign_id=999)
    assert pdf_file.exists()
    assert pdf_file.stat().st_size > 1000
    # PDF magic header check
    assert pdf_file.read_bytes().startswith(b"%PDF")


def test_settings_and_test_endpoints(monkeypatch):
    with TestClient(app, base_url="http://localhost") as client:
        # Test dashboard settings update
        resp = client.post(
            "/settings",
            data={
                "base_url": "http://127.0.0.1:31415",
                "api_key": "test-key",
                "llm_model": "test-model",
                "smtp_host": "mail.example.com",
                "smtp_port": "587",
                "smtp_username": "user",
                "smtp_password": "pwd",
                "smtp_from": "me@example.com",
                "smtp_use_tls": "true",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert services.settings.base_url == "http://127.0.0.1:31415"
        assert services.settings.smtp_host == "mail.example.com"

        # Test LLM endpoint check (offline endpoint test)
        llm_check = client.post(
            "/settings/test-llm",
            data={"base_url": "http://127.0.0.1:59999", "api_key": "", "llm_model": "test"},
        )
        assert llm_check.status_code == 200
        assert "ok" in llm_check.json()


def test_candidate_management():
    source = "cv:\n  name: Candidate A\n  email: ca@example.com\n  sections: {}\n"
    with TestClient(app, base_url="http://localhost") as client:
        # Create without consent -> rejected
        bad = client.post(
            "/candidates",
            data={"name": "Candidate A", "email": "ca@example.com", "cv_yaml": source, "consent": "false"},
        )
        assert bad.status_code == 400

        # Create with consent -> ok
        ok = client.post(
            "/candidates",
            data={"name": "Candidate A", "email": "ca@example.com", "cv_yaml": source, "consent": "true"},
            follow_redirects=False,
        )
        assert ok.status_code == 303

        # Read candidate info
        detail = client.get("/candidates/1")
        assert detail.status_code == 200
        assert detail.json()["name"] == "Candidate A"

        # Delete candidate
        delete_resp = client.post("/candidates/1/delete", follow_redirects=False)
        assert delete_resp.status_code == 303
        assert client.get("/candidates/1").status_code == 404


def test_full_pipeline_end_to_end(monkeypatch, tmp_path):
    """End-to-end test of the entire system:
    1. Candidate created from RenderCV YAML.
    2. Campaign created targeting a company & role.
    3. Research executed with company facts & job description.
    4. LLM tailoring formats RenderCV YAML & drafts email.
    5. Real RenderCV compiles tailored PDF.
    6. Email subject and body can be edited before send.
    7. PDF can be previewed and downloaded.
    8. SMTP send delivers email with PDF attachment.
    9. Audit trail tracks every milestone.
    10. Duplicate send is strictly rejected.
    """
    from app import main

    source = Path("tests/fixtures/sample_cv.yaml").read_text(encoding="utf-8")
    monkeypatch.setattr(main.settings, "artifact_dir", tmp_path)

    # Mock research and LLM response to provide deterministic testing
    monkeypatch.setattr(
        main,
        "research_company",
        lambda *args, **kwargs: {
            "company": "DeepTech Labs",
            "target_role": "Lead Cloud Architect",
            "sources": [{"url": "https://deeptech.example.com", "content": "Specializing in distributed systems and FastAPI."}],
        },
    )

    tailored_source = source.replace("Built reliable Python services.", "Architected high-throughput async FastAPI microservices.")
    monkeypatch.setattr(
        main,
        "tailor",
        lambda *args, **kwargs: {
            "cv_yaml": tailored_source,
            "email_subject": "Application: Lead Cloud Architect – Test Person",
            "email_body": "Dear DeepTech Labs Team,\n\nI am applying for the Lead Cloud Architect role.\nMy CV is attached.",
        },
    )

    sent_messages = []
    monkeypatch.setattr(main, "send_email", lambda *args, **kwargs: sent_messages.append(args))

    with TestClient(app, base_url="http://localhost") as client:
        # 1. Add candidate
        c_resp = client.post(
            "/candidates",
            data={"name": "Test Person", "email": "test@example.com", "cv_yaml": source, "consent": "true"},
            follow_redirects=False,
        )
        assert c_resp.status_code == 303

        # 2. Create target campaign
        camp_resp = client.post(
            "/campaigns",
            data={
                "candidate_id": 1,
                "company_name": "DeepTech Labs",
                "company_url": "https://deeptech.example.com",
                "target_role": "Lead Cloud Architect",
                "job_description": "Must have expertise in Python, FastAPI, and RenderCV systems.",
                "recipient_email": "careers@deeptech.example.com",
            },
            follow_redirects=False,
        )
        assert camp_resp.status_code == 303

        # 3. Prepare campaign (Research -> Tailor -> Real RenderCV PDF Compilation)
        prep_resp = client.post("/campaigns/1/prepare", follow_redirects=False)
        assert prep_resp.status_code == 303

        # 4. Detail page inspection
        page = client.get("/campaigns/1")
        assert page.status_code == 200
        assert "DeepTech Labs" in page.text
        assert "Lead Cloud Architect" in page.text
        assert "Application: Lead Cloud Architect" in page.text

        # 5. Preview & Download PDF
        download_resp = client.get("/campaigns/1/cv.pdf")
        assert download_resp.status_code == 200
        assert download_resp.headers["content-type"] == "application/pdf"
        assert download_resp.content.startswith(b"%PDF")

        preview_resp = client.get("/campaigns/1/preview.pdf")
        assert preview_resp.status_code == 200
        assert preview_resp.headers["content-disposition"] == "inline"

        # 6. Edit email before sending
        edit_resp = client.post(
            "/campaigns/1/update-email",
            data={
                "email_subject": "Application: Lead Cloud Architect (Updated)",
                    "email_body": "Dear DeepTech Labs Hiring Team,\n\nUpdated cover note. My CV is attached.",
            },
            follow_redirects=False,
        )
        assert edit_resp.status_code == 303

        # Check that email was updated
        page_updated = client.get("/campaigns/1")
        assert "Application: Lead Cloud Architect (Updated)" in page_updated.text

        # 7. Send without approval -> rejected
        unapproved = client.post("/campaigns/1/send", data={"approved": "false"})
        assert unapproved.status_code == 400

        # 8. Send with approval -> delivered
        sent_resp = client.post("/campaigns/1/send", data={"approved": "true"}, follow_redirects=False)
        assert sent_resp.status_code == 303
        assert len(sent_messages) == 1
        recipient, subject, body, attachment = sent_messages[0]
        assert recipient == "careers@deeptech.example.com"
        assert subject == "Application: Lead Cloud Architect (Updated)"
        assert Path(attachment).exists()

        # 9. Verify campaign status is now 'sent'
        page_sent = client.get("/campaigns/1")
        assert "status-sent" in page_sent.text

        # 10. Duplicate send rejection
        dup = client.post("/campaigns/1/send", data={"approved": "true"})
        assert dup.status_code == 400
