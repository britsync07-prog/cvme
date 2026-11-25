import base64
import html
import json
import ipaddress
import re
import smtplib
import socket
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
import yaml

from .config import settings
from .security import decrypt_secret, encrypt_secret


LLM_TIMEOUT_SECONDS = 300
LLM_REQUEST_ATTEMPTS = 5
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def _llm_url(base_url: str, endpoint: str) -> str:
    """Accept provider roots with or without a trailing /v1."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return f"{root}/v1/{endpoint.lstrip('/')}"


def _llm_request(request, *args, **kwargs) -> httpx.Response:
    """Retry transport failures from slow or temporarily unavailable providers."""
    for attempt in range(LLM_REQUEST_ATTEMPTS):
        try:
            return request(*args, timeout=LLM_TIMEOUT_SECONDS, **kwargs)
        except httpx.RequestError:
            if attempt == LLM_REQUEST_ATTEMPTS - 1:
                raise
    raise RuntimeError("Unreachable")


def _llm_content(response: httpx.Response) -> str:
    """Read standard OpenAI JSON or OpenAI-compatible SSE responses."""
    headers = getattr(response, "headers", {})
    if "text/event-stream" not in headers.get("content-type", "").lower():
        return response.json()["choices"][0]["message"]["content"]

    parts: list[str] = []
    for line in response.text.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        event = json.loads(data)
        choices = event.get("choices") or []
        if choices:
            content = choices[0].get("delta", {}).get("content")
            if isinstance(content, str):
                parts.append(content)
    if not parts:
        raise ValueError("LLM stream did not contain assistant content.")
    return "".join(parts)


def _public_url(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Website URL contains invalid control characters.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Website URL must start with http:// or https://")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as exc:
        raise ValueError("Website host cannot be resolved.") from exc
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Website must resolve to a public IP address.")
    return value


def _website_text(website: str) -> tuple[str, str]:
    current = _public_url(website)
    response = None
    for _ in range(4):
        response = httpx.get(
            current, timeout=15, follow_redirects=False,
            headers={"User-Agent": "TailorJobhunt research bot/1.0"},
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        location = response.headers.get("location")
        if not location:
            break
        current = _public_url(urljoin(current, location))
    assert response is not None
    response.raise_for_status()
    text = re.sub(r"<[^>]+>", " ", response.text)
    return str(response.url), re.sub(r"\s+", " ", text).strip()


def _search_web(query: str) -> list[str]:
    for search_url, pattern in (
        ("https://www.bing.com/search", r'<li class="b_algo"[^>]*>.*?<h2[^>]*>.*?<a[^>]+href="([^"]+)"'),
        ("https://html.duckduckgo.com/html/", r'class="result__a"[^>]+href="([^"]+)"'),
        ("https://lite.duckduckgo.com/lite/", r'href="([^"]+)" class=[\'\"]result-link'),
    ):
        try:
            response = httpx.get(
                search_url, params={"q": query}, timeout=20,
                headers={"User-Agent": "Mozilla/5.0 (compatible; TailorJobhunt/1.0)"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            continue
        results: list[str] = []
        for link in re.findall(pattern, response.text, flags=re.S):
            parsed = urlparse(unquote(link.replace("&amp;", "&")))
            destination = parse_qs(parsed.query).get("uddg", [link])[0]
            if parsed.hostname and parsed.hostname.endswith("bing.com"):
                encoded = parse_qs(parsed.query).get("u", [""])[0]
                if encoded.startswith("a1"):
                    try:
                        destination = base64.b64decode(encoded[2:] + "===").decode("utf-8")
                    except (UnicodeDecodeError, ValueError):
                        continue
            if destination.startswith(("http://", "https://")) and not any(
                ord(character) < 32 or ord(character) == 127 for character in destination
            ):
                results.append(destination)
        if results:
            return results
    return []


def research_company(
    name: str,
    website: str | None,
    target_role: str | None = None,
    job_description: str | None = None,
) -> dict:
    """Collect public facts and job context without hallucination."""
    sources: list[dict[str, str]] = []

    if website:
        try:
            website_url, text = _website_text(website)
            sources.append({"url": website_url, "content": text[:12000]})
            website_host = urlparse(website_url).hostname
            # Collect evidence about both the business and what it hires for.
            seen_sources = {website_url}
            parsed_website = urlparse(website_url)
            for suffix in ("/contact", "/contact-us", "/careers"):
                if len(sources) >= 4:
                    break
                try:
                    contact_url, contact_text = _website_text(f"{parsed_website.scheme}://{parsed_website.netloc}{suffix}")
                except (httpx.HTTPError, ValueError, KeyError):
                    continue
                if contact_url not in seen_sources:
                    seen_sources.add(contact_url)
                    sources.append({"url": contact_url, "content": contact_text[:12000]})
            research_queries = [
                f"{name} company services technology",
                f"{name} careers jobs hiring {target_role or ''}",
                f"{name} company information",
            ]
            for query in research_queries:
                for result_url in _search_web(query):
                    if len(sources) >= 4 or urlparse(result_url).hostname == website_host:
                        continue
                    try:
                        source_url, source_text = _website_text(result_url)
                    except (httpx.HTTPError, ValueError):
                        continue
                    if source_url not in seen_sources:
                        seen_sources.add(source_url)
                        sources.append({"url": source_url, "content": source_text[:12000]})
        except ValueError as exc:
            if "public IP" in str(exc) or "http" in str(exc):
                raise
            if job_description or target_role:
                sources.append({"url": website, "content": f"DNS note: {exc}. Using provided job specifications."})
            else:
                raise
        except httpx.HTTPError as exc:
            if job_description or target_role:
                sources.append({"url": website, "content": f"Web fetch notice: {exc}. Using provided job specifications."})
            else:
                raise ValueError(f"Failed to fetch company website: {exc}") from exc

    elif job_description or target_role:
        sources.append({"url": "user-input", "content": job_description or f"Target Role: {target_role}"})
    else:
        raise ValueError("Provide a company website or target role/job description.")

    if job_description and not any(s.get("url") == "user-input" for s in sources):
        sources.append({"url": "user-input", "content": job_description})

    return {
        "company": name,
        "target_role": target_role or "",
        "researched_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
    }


def candidates_from_zip(contents: bytes) -> list[tuple[str, str, str]]:
    """Extract RenderCV YAML records without writing untrusted archive members to disk."""
    if len(contents) > 20 * 1024 * 1024:
        raise ValueError("ZIP upload exceeds 20 MB.")
    try:
        archive = zipfile.ZipFile(BytesIO(contents))
    except zipfile.BadZipFile as exc:
        raise ValueError("Upload must be a ZIP file containing RenderCV YAML files.") from exc
    entries = [entry for entry in archive.infolist() if not entry.is_dir()]
    if not 1 <= len(entries) <= 500:
        raise ValueError("ZIP must contain between 1 and 500 CV files.")
    if sum(entry.file_size for entry in entries) > 20 * 1024 * 1024:
        raise ValueError("Uncompressed ZIP content exceeds 20 MB.")
    result = []
    for entry in entries:
        if not entry.filename.lower().endswith((".yaml", ".yml")):
            raise ValueError("ZIP may contain only .yaml or .yml CV files.")
        raw = archive.read(entry).decode("utf-8")
        data = yaml.safe_load(raw)
        cv = data.get("cv", {}) if isinstance(data, dict) else {}
        name, email = cv.get("name"), cv.get("email")
        if not isinstance(name, str) or not name.strip() or not isinstance(email, str) or "@" not in email or not isinstance(cv.get("sections", {}), dict):
            raise ValueError(f"{entry.filename} needs cv.name and cv.email.")
        result.append((name.strip(), email.strip(), raw))
    return result


def cv_outreach_readiness_error(candidate_yaml: str) -> str | None:
    """Return a concrete reason when a CV is too thin for unattended outreach."""
    try:
        document = yaml.safe_load(candidate_yaml) or {}
    except yaml.YAMLError:
        return "Candidate CV is not valid RenderCV YAML."
    cv = document.get("cv", {}) if isinstance(document, dict) else {}
    sections = cv.get("sections", {}) if isinstance(cv, dict) else {}
    if not isinstance(cv, dict) or not cv.get("name") or not cv.get("email"):
        return "Candidate CV needs a name and email before outreach."
    if not isinstance(sections, dict):
        return "Candidate CV needs populated RenderCV sections before outreach."
    populated = [name for name, entries in sections.items() if entries]
    if len(populated) < 2:
        return (
            "Candidate CV is too thin for automatic sending. Add at least one more populated section "
            "such as skills, education, or projects."
        )
    return None


def _clean_json_response(content: str) -> dict:
    """Safely extracts JSON even if enclosed in markdown code fences."""
    cleaned = content.strip()
    if cleaned.startswith("```"):
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if match:
            cleaned = match.group(1).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("AI response JSON must be an object.")
    return parsed


def _grounded_outreach_email(candidate_yaml: str, company_name: str, target_role: str | None) -> tuple[str, str]:
    """Build a truthful email when discovery has no verified job advert."""
    source = yaml.safe_load(candidate_yaml) or {}
    cv = source.get("cv", {}) if isinstance(source, dict) else {}
    sections = cv.get("sections", {}) if isinstance(cv, dict) else {}
    experience = sections.get("experience", []) if isinstance(sections, dict) else []
    latest = experience[0] if isinstance(experience, list) and experience and isinstance(experience[0], dict) else {}
    name = cv.get("name", "") if isinstance(cv, dict) else ""
    role = target_role or "an opportunity"
    background = ""
    if latest.get("position") and latest.get("company"):
        background = f" My current background includes work as {latest['position']} at {latest['company']}."
    body = (
        f"Dear Hiring Team,\n\n"
        f"I am writing to express my interest in {role} at {company_name}.{background} "
        f"After reviewing your public website, I would welcome the opportunity to discuss how my experience could contribute to your team. "
        f"I have attached my tailored CV for your consideration.\n\n"
        f"Kind regards,\n{name}".rstrip()
    )
    return f"Application to {company_name} — {role} | {name}".rstrip(" |"), body


def tailor(
    candidate_yaml: str,
    research: dict,
    company_name: str,
    target_role: str | None = None,
    job_description: str | None = None,
    ai_config=None,
) -> dict:
    base_url = settings.base_url
    api_key = settings.api_key
    llm_model = settings.llm_model
    if ai_config is not None:
        base_url = ai_config.base_url or base_url
        api_key = decrypt_secret(ai_config.encrypted_api_key)
        llm_model = ai_config.model_name or llm_model
    if not api_key:
        raise RuntimeError("API_KEY is not configured.")
    try:
        parsed_source = yaml.safe_load(candidate_yaml)
    except yaml.YAMLError as exc:
        raise ValueError(f"Candidate RenderCV YAML is invalid: {exc}") from exc

    original_name = parsed_source.get("cv", {}).get("name", "")

    role_str = f" for the position of {target_role}" if target_role else "; infer the most relevant role from the company evidence"
    prompt = f"""You are a professional CV tailoring and job application assistant.
Your goal is to tailor the candidate's RenderCV YAML and draft an application email specifically for {company_name}{role_str}.

You MUST return a JSON object with exactly three string keys:
1. "cv_yaml": The customized RenderCV YAML document.
2. "email_subject": A concise, engaging, professional email subject line.
3. "email_body": A high-converting, professional cover letter / email body.

STRICT RULES:
1. Preserve all factual candidate details. Never add fake employers, degrees, dates, metrics, skills, certifications, claims, or referrals not present in the original YAML.
2. You may reorder sections, emphasize matching skills, and refine wording to match the company's domain and tech stack.
3. Keep valid RenderCV YAML conforming to RenderCV specifications.
4. The email body must be concise, professional, mention the company name, cite one evidenced company/job fact, include no aggressive pressure, and state that the tailored CV is attached in PDF format.
5. Use the research to identify the company's services, hiring signals, priorities, terminology, and likely skills; reflect those in the headline, profile emphasis, skills ordering, role, and email.
6. Return a genuinely tailored CV, not a byte-for-byte copy of the original. Never invent a requirement when the research does not support it.
7. Preserve every original RenderCV section and every factual list entry; rewrite or reorder wording only, without omitting entries or changing section names.

Company: {company_name}
{f'Target Role: {target_role}' if target_role else ''}
{f'Job Description / Notes: {job_description}' if job_description else ''}
Research Insights:
{json.dumps(research)}

Original RenderCV YAML:
{candidate_yaml}"""

    url = _llm_url(base_url, "chat/completions")
    headers = {"Authorization": f"Bearer {api_key}"}

    # First attempt with json_object format; fallback to standard if model does not support it
    payload = {
        "model": llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.15,
        "response_format": {"type": "json_object"},
    }

    try:
        response = _llm_request(httpx.post, url, headers=headers, json=payload)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {400, 422}:
            # Retry without response_format
            payload.pop("response_format", None)
            response = _llm_request(httpx.post, url, headers=headers, json=payload)
            response.raise_for_status()
        else:
            raise

    result = None
    last_error = None
    for attempt in range(LLM_REQUEST_ATTEMPTS):
        try:
            result = _clean_json_response(_llm_content(response))
            break
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt == LLM_REQUEST_ATTEMPTS - 1:
                raise ValueError("AI returned invalid JSON after five attempts.") from exc
            response = _llm_request(httpx.post, url, headers=headers, json=payload)
            response.raise_for_status()
    if result is None:
        raise ValueError("AI returned no usable tailoring result.") from last_error

    for key in ("cv_yaml", "email_subject", "email_body"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise ValueError(f"LLM response missing {key}")
    if company_name.casefold() not in result["email_body"].casefold():
        raise ValueError("AI email must mention the researched company by name.")

    tailored_data = yaml.safe_load(result["cv_yaml"])
    if not isinstance(tailored_data, dict):
        raise ValueError("LLM response cv_yaml must be a YAML object.")
    source_cv = parsed_source.get("cv", {})
    tailored_cv = tailored_data.get("cv", {})
    source_sections = source_cv.get("sections", {}) if isinstance(source_cv, dict) else {}
    tailored_sections = tailored_cv.get("sections", {}) if isinstance(tailored_cv, dict) else {}
    factual_fields = ("name", "email", "phone", "location", "website")
    source_lists_match = all(
        isinstance(tailored_sections.get(name), list)
        and len(tailored_sections[name]) == len(entries)
        for name, entries in source_sections.items()
        if isinstance(entries, list)
    )
    if (
        not isinstance(tailored_cv, dict)
        or any(tailored_cv.get(field) != source_cv.get(field) for field in factual_fields if field in source_cv)
        or set(tailored_sections) != set(source_sections)
        or not source_lists_match
    ):
        tailored_data = parsed_source
    if original_name and tailored_data.get("cv", {}).get("name") != original_name:
        tailored_data.setdefault("cv", {})["name"] = original_name
    if yaml.safe_dump(tailored_data, sort_keys=False) == yaml.safe_dump(parsed_source, sort_keys=False):
        raise ValueError("AI returned an unchanged CV; company-specific tailoring is required.")
    # RenderCV design is top-level. Keep the source template when a model nests or omits it.
    if "design" in parsed_source:
        tailored_data["design"] = parsed_source["design"]
        if isinstance(tailored_data.get("cv"), dict):
            tailored_data["cv"].pop("design", None)
    result["cv_yaml"] = yaml.dump(tailored_data, sort_keys=False)

    return result


def render_pdf(cv_yaml: str, campaign_id: int) -> Path:
    output = settings.artifact_dir / str(campaign_id)
    output.mkdir(parents=True, exist_ok=True)
    input_path = output / "tailored_cv.yaml"
    input_path.write_text(cv_yaml, encoding="utf-8")
    pdf_path = output / "tailored_cv.pdf"

    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "rendercv",
            "render",
            str(input_path),
            "--pdf-path",
            "tailored_cv.pdf",
            "-nomd",
            "-nohtml",
            "-nopng",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if run.returncode != 0 or not pdf_path.exists():
        err_msg = run.stderr[-1500:] if run.stderr else "RenderCV exited with non-zero status"
        raise RuntimeError(f"RenderCV failed: {err_msg}")
    return pdf_path


def send_email(recipient: str, subject: str, body: str, attachment: Path, email_account=None) -> str | None:
    sender_host = settings.smtp_host
    sender_port = settings.smtp_port
    sender_username = settings.smtp_username
    sender_password = settings.smtp_password
    sender_from = settings.smtp_from
    sender_tls = settings.smtp_use_tls
    if email_account is not None:
        if email_account.provider == "gmail":
            credentials = json.loads(decrypt_secret(email_account.encrypted_credentials))
            access_token = credentials.get("access_token", "")
            if credentials.get("refresh_token") and credentials.get("expires_at", 0) <= int(datetime.now(timezone.utc).timestamp()) + 30:
                response = httpx.post("https://oauth2.googleapis.com/token", data={"client_id": settings.google_client_id, "client_secret": settings.google_client_secret, "refresh_token": credentials["refresh_token"], "grant_type": "refresh_token"}, timeout=30)
                response.raise_for_status()
                token = response.json()
                access_token = token["access_token"]
                credentials["access_token"] = access_token
                credentials["expires_at"] = int(datetime.now(timezone.utc).timestamp()) + int(token.get("expires_in", 3600))
                email_account.encrypted_credentials = encrypt_secret(json.dumps(credentials))
            if not access_token:
                raise RuntimeError("Gmail authorization has expired. Reconnect Gmail.")
            message = EmailMessage()
            message["From"] = email_account.email
            message["To"] = recipient
            message["Subject"] = subject
            message.set_content(body)
            message.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment.name)
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
            response = httpx.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", headers={"Authorization": f"Bearer {access_token}"}, json={"raw": raw}, timeout=30)
            response.raise_for_status()
            return response.json().get("id")
        if email_account.provider != "smtp":
            raise RuntimeError("This email account provider is not enabled yet.")
        credentials = json.loads(decrypt_secret(email_account.encrypted_credentials))
        sender_host = credentials["host"]
        sender_port = int(credentials.get("port", 587))
        sender_username = credentials.get("username", "")
        sender_password = credentials.get("password", "")
        sender_from = email_account.email
        sender_tls = bool(credentials.get("use_tls", True))
    if settings.resend_api_key:
        if email_account is not None:
            raise RuntimeError("Resend is a platform sender; disconnect the workspace sender first.")
        if not settings.resend_from:
            raise RuntimeError("RESEND_FROM is required when using Resend.")
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.resend_from,
                "to": [recipient],
                "subject": subject,
                "text": body,
                "attachments": [{"filename": attachment.name, "content": base64.b64encode(attachment.read_bytes()).decode("ascii")}],
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("id") if isinstance(payload, dict) and isinstance(payload.get("id"), str) else None
    if not all([sender_host, sender_from]):
        raise RuntimeError("SMTP is not configured. Email was not sent.")
    message = EmailMessage()
    message["From"] = sender_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    message.add_attachment(
        attachment.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=attachment.name,
    )
    with smtplib.SMTP(sender_host, sender_port, timeout=30) as client:
        if sender_tls:
            client.starttls()
        if sender_username:
            client.login(sender_username, sender_password)
        client.send_message(message)
    return None


def test_llm_connection(base_url: str, api_key: str, model: str) -> dict:
    url = _llm_url(base_url, "models")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = _llm_request(httpx.get, url, headers=headers)
        response.raise_for_status()
        models = [m.get("id") for m in response.json().get("data", []) if isinstance(m, dict)]
        return {"ok": True, "message": f"Connected successfully. Models available: {', '.join(models[:5]) or model}"}
    except Exception as exc:
        return {"ok": False, "message": f"Connection failed to {url}: {exc}"}


def send_system_email(recipient: str, subject: str, body: str) -> None:
    """Send a platform transactional message, never through a tenant sender."""
    if settings.resend_api_key and settings.resend_from:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.resend_from,
                "to": [recipient],
                "subject": subject,
                "text": body,
                "html": f"<main style='font-family:Arial,sans-serif;line-height:1.5'><h2>TailorJobhunt</h2><p>{html.escape(body).replace(chr(10), '<br>')}</p></main>",
            },
            timeout=30,
        )
        response.raise_for_status()
        return
    if not settings.smtp_host or not settings.smtp_from:
        return
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(f"<main style='font-family:Arial,sans-serif;line-height:1.5'><h2>TailorJobhunt</h2><p>{html.escape(body).replace(chr(10), '<br>')}</p></main>", subtype="html")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as client:
        if settings.smtp_use_tls:
            client.starttls()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


def test_smtp_connection(
    host: str, port: int, username: str, password: str, use_tls: bool
) -> dict:
    if not host:
        return {"ok": False, "message": "SMTP host is required"}
    try:
        with smtplib.SMTP(host, port, timeout=10) as client:
            client.ehlo()
            if use_tls:
                client.starttls()
                client.ehlo()
            if username and password:
                client.login(username, password)
        return {"ok": True, "message": f"Successfully connected to SMTP server at {host}:{port}"}
    except Exception as exc:
        return {"ok": False, "message": f"SMTP connection failed: {exc}"}
