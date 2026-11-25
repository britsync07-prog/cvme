from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/tailormail.db"
    app_secret: str = "development-only-change-me"
    encryption_key: str = ""
    environment: str = "development"
    admin_username: str = ""
    admin_password: str = ""
    allowed_hosts: str = "localhost,127.0.0.1"
    artifact_dir: Path = Path("artifacts")
    base_url: str = "http://127.0.0.1:31415"
    api_key: str = ""
    llm_model: str = "local-model"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    resend_api_key: str = ""
    resend_from: str = ""
    resend_webhook_secret: str = ""
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    jooble_api_key: str = ""
    the_muse_api_key: str = ""
    jobspy_proxies: str = ""
    enable_jobspy: bool = True
    enable_himalayas: bool = True
    enable_adzuna: bool = True
    enable_jooble: bool = True
    enable_remotive: bool = True
    enable_arbeitnow: bool = True
    enable_jobicy: bool = True
    # The former public endpoint now returns 404; leave off until a valid
    # registered API integration is configured.
    enable_the_muse: bool = False
    enable_remotejobs_reference: bool = False
    job_cache_ttl_seconds: int = 900
    job_provider_timeout_seconds: int = 30
    contact_cooldown_days: int = 30
    scrapollo_binary: str = "scrapollo"
    scrapollo_cookie_file: str = ""
    scrapollo_input_file: str = ""
    scrapollo_output_dir: Path = Path("data/scrapollo-results")
    outreach_daily_limit: int = 100
    outreach_policy: str = "review_required"
    outreach_candidate_id: int = 0
    outreach_query: str = ""
    outreach_target_role: str = ""
    outreach_auto_send: bool = False
    outreach_timezone: str = "Asia/Dhaka"
    outreach_max_search_pages: int = 10
    outreach_search_page_size: int = 100
    outreach_min_delay_seconds: int = 5
    outreach_max_delay_seconds: int = 15
    outreach_kill_switch: bool = False
    trial_days: int = 15
    trial_card_required: bool = False
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""
    redis_url: str = "redis://redis:6379/0"
    signup_enabled: bool = True
    maintenance_mode: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    turnstile_secret_key: str = ""
    turnstile_site_key: str = ""
    sentry_dsn: str = ""


settings = Settings()

if settings.environment == "production":
    if not settings.admin_username or not settings.admin_password:
        raise RuntimeError("ADMIN_USERNAME and ADMIN_PASSWORD are required in production.")
    if settings.app_secret == "development-only-change-me" or len(settings.app_secret) < 32:
        raise RuntimeError("APP_SECRET must be a unique value of at least 32 characters in production.")
    if "*" in {host.strip() for host in settings.allowed_hosts.split(",")}:
        raise RuntimeError("ALLOWED_HOSTS cannot contain '*' in production.")
    if settings.database_url.startswith("sqlite"):
        raise RuntimeError("DATABASE_URL must use PostgreSQL in production.")
    if not settings.encryption_key:
        raise RuntimeError("ENCRYPTION_KEY is required in production.")


def update_settings(**kwargs) -> None:
    for key, value in kwargs.items():
        if hasattr(settings, key) and value is not None:
            setattr(settings, key, value)
