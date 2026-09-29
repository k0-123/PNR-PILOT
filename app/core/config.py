"""Configuration: .env settings (pydantic-settings), result fields and site profiles (YAML)."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
RESULT_FIELDS_PATH = CONFIG_DIR / "result_fields.yaml"
SITE_PROFILES_DIR = CONFIG_DIR / "websites"


class Settings(BaseSettings):
    """Values from the environment / .env. Secrets are SecretStr so they never show in reprs."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: SecretStr = SecretStr("")
    # Flash with thinking off (realplan section 3). gemini-2.5-flash is retired (404 for new
    # users, checked 2026-09-24); Google names gemini-3.6-flash as its replacement.
    gemini_model_extract: str = "gemini-3.6-flash"
    gemini_model_result: str = "gemini-3.1-flash-lite"
    # "off" disables thinking (cheapest); Pro models need "low" or higher.
    gemini_thinking_extract: Literal["off", "low", "medium", "high"] = "off"
    gemini_thinking_result: Literal["off", "low", "medium", "high"] = "off"
    gemini_concurrency: int = Field(8, ge=1, le=64)  # parallel Gemini calls in the worker
    gemini_timeout_seconds: int = Field(120, gt=0)
    gemini_max_retries: int = Field(3, ge=0)

    # gemini-3.6-flash paid tier until 2026-12-31 (doubles on 2027-01-01: 1.50 / 7.50).
    price_input_per_m: float = Field(0.75, ge=0)
    price_output_per_m: float = Field(3.75, ge=0)
    price_result_input_per_m: float = Field(0.25, ge=0)
    price_result_output_per_m: float = Field(1.50, ge=0)

    # FLT credits: only this signed-in account may add FLT (plans / top-ups).
    flt_admin_email: str = "shekhawatk271@gmail.com"

    confidence_threshold: float = Field(0.85, ge=0.0, le=1.0)

    app_email: str = ""
    app_password: SecretStr = SecretStr("")
    data_dir: Path = Path("./data")
    max_file_size_mb: float = Field(20, gt=0)
    max_images_per_job: int = Field(500, gt=0)
    delete_files_after_days: int = Field(7, ge=1)
    log_level: str = "INFO"

    # Extension API (app/api.py)
    api_host: str = "127.0.0.1"
    api_port: int = Field(8000, gt=0, lt=65536)
    # Comma-separated origins allowed by CORS, e.g. chrome-extension://abcdef...
    allowed_extension_origins: str = ""
    lease_minutes: float = Field(10, gt=0)
    max_capture_text_kb: int = Field(512, gt=0)
    max_capture_screenshot_mb: float = Field(8, gt=0)

    @property
    def extension_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_extension_origins.split(",") if o.strip()]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    def secret_values(self) -> list[str]:
        """Raw secret strings, for log redaction."""
        return [s.get_secret_value() for s in (self.gemini_api_key, self.app_password)
                if s.get_secret_value()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ------------------------------------------------------------ result fields

class ResultField(BaseModel):
    key: str
    label: str
    description: str | None = None
    # "passenger": differs per person on a booking (name, e-ticket, frequent flyer).
    # "booking": the same for everyone on the PNR (flights, route, contact, status).
    scope: Literal["passenger", "booking"] = "booking"

    @field_validator("key")
    @classmethod
    def _key_is_identifier(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError(f"result field key {v!r} must be lowercase snake_case")
        return v


class ResultFieldsConfig(BaseModel):
    fields: list[ResultField] = Field(min_length=1)

    @property
    def booking_keys(self) -> list[str]:
        return [f.key for f in self.fields if f.scope == "booking"]

    @field_validator("fields")
    @classmethod
    def _unique_keys(cls, v: list[ResultField]) -> list[ResultField]:
        keys = [f.key for f in v]
        if len(keys) != len(set(keys)) or "confidence" in keys:
            raise ValueError("result field keys must be unique and not 'confidence'")
        return v


# ------------------------------------------------------------- site profiles
# How the browser extension works on one airline site (newplan section 5, realplan section 5).
# Selectors are set with the selector picker (step C) or by hand; "TODO" means not set yet.

class Selector(BaseModel):
    selector: str


class ProfileFields(BaseModel):
    surname: Selector
    pnr: Selector


class ResultDetect(BaseModel):
    url_contains: str | None = None
    selector: str | None = None
    # Result = the search form is gone and the PNR is visible on the page. For sites whose
    # result page has no stable selector (e.g. it's on another domain after a redirect).
    pnr_visible: bool = False


class TextDetect(BaseModel):
    selector: str | None = None
    text_contains: list[str] = []


class OpenForm(BaseModel):
    """What opens the search form when it's hidden behind a tab (e.g. MH's "My booking" tab).
    The extension clicks it; that only shows the form, it never starts a search."""
    selector: str | None = None
    text: str | None = None  # exact visible text of the tab, e.g. "My booking"


class AutoSubmit(BaseModel):
    """Auto-continue: the extension presses the search form's Continue button by itself, once
    staff switch it on in the side panel. Off unless the profile allows it. Searches are at least
    min_gap_ms apart, and it switches itself off at the first CAPTCHA / block."""
    allowed: bool = False
    min_gap_ms: int = Field(6000, ge=2000, le=120_000)
    # Upper bound of the gap: each search waits a random time in [min_gap_ms, max_gap_ms] so the
    # pace isn't a fixed, bot-like interval. 0 (or <= min) disables jitter (a fixed min_gap_ms).
    max_gap_ms: int = Field(0, ge=0, le=120_000)
    button_text: str | None = None  # the form's submit button is used; this text is the fallback

    @model_validator(mode="after")
    def _check_gap(self) -> "AutoSubmit":
        if self.max_gap_ms and self.max_gap_ms < self.min_gap_ms:
            raise ValueError("max_gap_ms must be >= min_gap_ms (or 0 to disable jitter)")
        return self


class Capture(BaseModel):
    container_selector: str = "body"
    screenshot: Literal["always", "fallback", "never"] = "fallback"


DEFAULT_BLOCK_WORDS = ["captcha", "verify you are human", "access denied", "unusual traffic",
                       "are you a robot", "request blocked"]


class SiteProfile(BaseModel):
    name: str
    search_url: str
    match_urls: list[str] = Field(min_length=1)
    fields: ProfileFields
    focus_after_fill: Literal["surname", "pnr"] = "pnr"
    open_form: OpenForm | None = None
    auto_submit: AutoSubmit = AutoSubmit()
    result_detect: ResultDetect
    not_found_detect: TextDetect = TextDetect()
    block_detect: TextDetect = TextDetect(text_contains=DEFAULT_BLOCK_WORDS)
    capture: Capture = Capture()
    settle_ms: int = Field(400, ge=0, le=10000)
    # A booking that was searched but produced no result page within this long is skipped (SKIPPED),
    # so one stuck page doesn't halt the run. 0 disables the watchdog. Blocks still pause, not skip.
    row_timeout_ms: int = Field(30_000, ge=0, le=300_000)
    # If the PNR isn't anywhere in the captured text, flag MISMATCH instead of saving it.
    pnr_check: bool = True

    @property
    def is_placeholder(self) -> bool:
        """True while selectors still say TODO (site not set up yet)."""
        return "TODO" in self.model_dump_json()


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a YAML mapping")
    return data


def load_result_fields(path: Path | None = None) -> ResultFieldsConfig:
    return ResultFieldsConfig.model_validate(_load_yaml(path or RESULT_FIELDS_PATH))


def load_site_profile(path: Path) -> SiteProfile:
    return SiteProfile.model_validate(_load_yaml(path))


def list_site_profiles(directory: Path | None = None) -> dict[str, Path]:
    """Site profile files: config/websites/<name>.yaml -> {name: path}."""
    directory = directory or SITE_PROFILES_DIR
    if not directory.is_dir():
        return {}
    return {p.stem: p for p in sorted(directory.glob("*.y*ml"))}
