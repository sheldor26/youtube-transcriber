from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from app.youtube import media_id, media_platform

# Hardcoded on purpose: this is a fixed allowlist, not a setting. See D-0006 / D-0008.
CLAUDE_ACADEMY_WEBINARS_URL = "https://academy.claude.com/assets/data/webinars-latest.json"


def parse_claude_academy_webinars(payload: Any) -> List[Dict[str, str]]:
    """Return only public, on-demand webinar recordings supported by this app."""
    records = payload.get("webinars") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError("Claude Academy returned an invalid webinar catalog.")

    webinars: List[Dict[str, str]] = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        recording = record.get("recording")
        if not isinstance(recording, dict):
            continue
        url = recording.get("url")
        identifier = media_id(url) if isinstance(url, str) else None
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        webinars.append(
            {
                "title": record.get("title") if isinstance(record.get("title"), str) else "Untitled webinar",
                "url": url,
                "platform": media_platform(url),
                "id": identifier,
            }
        )
    return webinars


def fetch_claude_academy_webinars() -> List[Dict[str, str]]:
    request = UrlRequest(CLAUDE_ACADEMY_WEBINARS_URL, headers={"User-Agent": "YouTube-Transcriber/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return parse_claude_academy_webinars(payload)
