from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from google import genai
from google.genai import types
from google.cloud import secretmanager
from dotenv import load_dotenv
# Load API key from .env
load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API key (cached after first successful Secret Manager fetch)
# ---------------------------------------------------------------------------


def get_gemini_api_key() -> Optional[str]:
    """Fetch the Gemini API key from GCP Secret Manager.

    """
    project_id = ""
    secret_id = "STAGE-Agentic"
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    try:
        sm_client = secretmanager.SecretManagerServiceClient()
        response = sm_client.access_secret_version(request={"name": name})
        api_key = response.payload.data.decode("UTF-8")
        logger.debug("gemini_service: API key fetched from Secret Manager")
        return api_key
    except Exception as exc:
        logger.warning(
            "gemini_service: could not retrieve API key from Secret Manager: %s", exc
        )
        return None

def test_api_key(api_key: str) -> bool:
    """Verify the key works by making a minimal Gemini API call."""
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents="Reply with exactly: OK",
    )
    text = (response.text or "").strip()
    print(f"API call succeeded. Response: {text!r}")
    return True


if __name__ == "__main__":
    api_key = get_gemini_api_key()
    if not api_key:
        print("FAIL: Could not fetch API key from Secret Manager")
        raise SystemExit(1)

    print(f"Key fetched from Secret Manager : {api_key}")
    try:
        test_api_key(api_key)
        print("RESULT: API key is working")
    except Exception as exc:
        print(f"FAIL: API call failed: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc