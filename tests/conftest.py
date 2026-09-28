import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.db import Database
from app.core.models import GeminiRow, JobStatus
from app.core.storage import LocalStorage
from app.extraction.gemini_client import GeminiClient
from app.extraction.validators import validate_row

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class FakeModels:
    """Replaces genai.Client().models.

    `responses`: queue of dict/str response texts or exceptions, or a single callable
    `fn(contents) -> dict | str | Exception` answering every call."""

    def __init__(self, responses):
        self.responder = responses if callable(responses) else None
        self.responses = [] if self.responder else list(responses)
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.responder:
            item = self.responder(contents)
        elif not self.responses:
            raise AssertionError("unexpected extra Gemini call")
        else:
            item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return SimpleNamespace(text=text, usage_metadata=SimpleNamespace(
            prompt_token_count=1000, candidates_token_count=200, thoughts_token_count=None))


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, gemini_api_key="test-secret-key", data_dir=tmp_path / "data",
                    gemini_max_retries=3, confidence_threshold=0.85,
                    price_input_per_m=0.30, price_output_per_m=2.50,
                    # One call at a time: FakeModels answers from a queue in call order. The
                    # parallel path is tested explicitly (test_worker_extracts_images_in_parallel).
                    gemini_concurrency=1)


@pytest.fixture
def make_client(settings):
    def _make(responses):
        fake = FakeModels(responses)
        return GeminiClient(settings, client=SimpleNamespace(models=fake), sleep=lambda s: None), fake
    return _make


@pytest.fixture
def db(settings):
    d = Database(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def storage(settings):
    return LocalStorage(settings.data_dir)


def png(tag: int = 0) -> bytes:
    return PNG_HEADER + b"\x00" * 16 + bytes([tag])


def gemini_row(**overrides):
    row = {
        "line_no": "086", "pax_count": 2, "surname": "SHUKLA", "first_name": "SIDDHESHVA",
        "title": None, "pnr": "BOGZGQ", "class_code": "U", "status": "HK", "date": "14FEB",
        "office_id": "SWI1G", "surname_confidence": 0.98, "pnr_confidence": 0.97, "notes": None,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------- lookup helpers
MOCK_FIELDS = Path(__file__).parent / "mock_site" / "result_fields.yaml"


@pytest.fixture(autouse=True)
def _mock_result_fields(monkeypatch):
    """Tests use the mock site's result fields, independent of the production config."""
    from app.core import config
    monkeypatch.setattr(config, "RESULT_FIELDS_PATH", MOCK_FIELDS)


@pytest.fixture(scope="session")
def mock_site():
    from tests.mock_site.server import Handler, start_server
    server, base_url = start_server(0)
    yield base_url, Handler.hits
    server.shutdown()


def make_job(db, rows, status=JobStatus.READY_FOR_LOOKUP, name="t"):
    """Job with one image and READY rows [(surname, pnr), ...], already in `status`."""
    job = db.create_job(name)
    image_id, _ = db.add_image(job, "a.png", f"uploads/job_{job}/a.png", "image/png", f"sha-{job}")
    extracted = [validate_row(GeminiRow(**gemini_row(line_no=f"{i:03d}", surname=s, pnr=p)), 0.85)
                 for i, (s, p) in enumerate(rows)]
    db.save_image_extraction(job, image_id, extracted, raw_ai_response="{}", tokens_in=0,
                             tokens_out=0, cost_usd=0, attempts=1, from_cache=False)
    db.update_job(job, status=status)
    return job
