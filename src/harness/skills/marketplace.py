"""Read-only client for the public SkillMD catalog."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import math
import re
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_API = "https://api.skillmd.com"
_ID_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class SkillSearchResult:
    id: str
    name: str
    description: str
    source: str
    verified: bool
    agents: str
    category: str
    avg_rating: float
    rating_count: int
    url: str


JsonRequest = Callable[[str], dict]


class SkillsCatalogClient:
    """Search and fetch public Skill packages without invoking an installer CLI."""

    def __init__(self, *, request_json: JsonRequest | None = None) -> None:
        self._request_json = request_json or _request_json

    def search(self, query: str, *, limit: int = 5) -> tuple[SkillSearchResult, ...]:
        if not isinstance(query, str) or len(query.strip()) < 2:
            raise ValueError("Skill search query must contain at least two characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("Skill search limit must be between 1 and 10.")
        url = f"{_API}/v1/search?{urlencode({'q': query.strip(), 'limit': limit})}"
        response = _request_with_retry(self._request_json, url)
        rows = response.get("items") if isinstance(response, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("Skills catalog returned an invalid search response.")
        results: list[SkillSearchResult] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            skill_id = row.get("slug")
            name = row.get("title")
            if not isinstance(skill_id, str) or not _valid_skill_id(skill_id):
                continue
            if row.get("type") != "single":
                continue
            if not isinstance(name, str) or not name.strip():
                continue
            description = row.get("description")
            if not isinstance(description, str):
                description = ""
            agents = row.get("agents")
            category = row.get("category")
            rating, rating_count = row.get("avg_rating", 0), row.get("rating_count", 0)
            if isinstance(rating, bool) or not isinstance(rating, (int, float)) or not math.isfinite(rating):
                rating = 0
            if isinstance(rating_count, bool) or not isinstance(rating_count, int) or rating_count < 0:
                rating_count = 0
            owner = skill_id.split("/", 1)[0]
            results.append(
                SkillSearchResult(
                    skill_id,
                    name.strip(),
                    description.strip(),
                    owner,
                    bool(row.get("verified")),
                    agents if isinstance(agents, str) else "",
                    category if isinstance(category, str) else "",
                    float(rating),
                    rating_count,
                    f"https://skillmd.com/skills/{skill_id}.md",
                )
            )
        return tuple(results)

    def fetch(self, skill_id: str) -> dict[str, str]:
        if not _valid_skill_id(skill_id):
            raise ValueError("Skill ID must come from a Skills catalog search result.")
        path = "/".join(quote(part, safe="") for part in skill_id.split("/"))
        detail = _request_with_retry(self._request_json, f"{_API}/v1/skills/{path}")
        if not isinstance(detail, dict) or detail.get("slug") != skill_id:
            raise RuntimeError("Skills catalog returned a different Skill than requested.")
        if detail.get("type") not in {None, "single"}:
            raise RuntimeError("Only individual Skills can be installed.")
        bundle = _request_with_retry(self._request_json, f"{_API}/api/skills/{path}/bundle?format=json")
        if not isinstance(bundle, dict) or bundle.get("slug") != skill_id:
            raise RuntimeError("Skills catalog returned a different Skill package than requested.")
        rows = bundle.get("files")
        if not isinstance(rows, list):
            raise RuntimeError("This catalog entry has no downloadable Skill package.")
        if len(rows) > 128:
            raise RuntimeError("Skill package contains too many files.")
        files: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("Skills catalog returned an invalid package file.")
            file_path, encoded = row.get("path"), row.get("content_base64")
            if not isinstance(file_path, str) or not isinstance(encoded, str):
                raise RuntimeError("Skill package files must contain inline UTF-8 text.")
            if file_path in files:
                raise RuntimeError("Skill package contains duplicate file paths.")
            try:
                raw = base64.b64decode(encoded, validate=True)
                contents = raw.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise RuntimeError("Skill package contains invalid base64 or non-UTF-8 content.") from error
            digest = row.get("sha256")
            if digest is not None and (not isinstance(digest, str) or hashlib.sha256(raw).hexdigest() != digest):
                raise RuntimeError("Skill package file failed its SHA-256 check.")
            files[file_path] = contents
        if "SKILL.md" not in files:
            raise RuntimeError("Skill package is missing SKILL.md.")
        return files


def _valid_skill_id(skill_id: str) -> bool:
    if not isinstance(skill_id, str):
        return False
    parts = skill_id.split("/")
    return len(parts) == 2 and all(_ID_PART.fullmatch(part) and part not in {".", ".."} for part in parts)


def _request_json(url: str) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "MiniCC"})
    opener = build_opener(_CatalogRedirectHandler())
    timeout = 30 if "/bundle?" in url else 20
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("Skills catalog response exceeds 5 MB.")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Skills catalog returned invalid JSON.") from error
    if not isinstance(value, dict):
        raise RuntimeError("Skills catalog returned an invalid response.")
    return value


def _request_with_retry(requester: JsonRequest, url: str) -> dict:
    for attempt in range(2):
        try:
            return requester(url)
        except HTTPError as error:
            if attempt or (error.code != 429 and error.code < 500):
                raise
        except (TimeoutError, ConnectionError, URLError):
            if attempt:
                raise
        sleep(0.25)
    raise RuntimeError("Skills catalog request failed after retry.")


class _CatalogRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        target = urlsplit(new_url)
        if target.scheme != "https" or target.hostname != "api.skillmd.com" or target.port not in {None, 443}:
            raise HTTPError(request.full_url, code, "Skill catalog redirected outside its API host.", headers, file)
        return super().redirect_request(request, file, code, message, headers, new_url)
