"""The Postman collection must keep up with the routes.

Spec 01 section 2 asks for a collection that exercises every endpoint in
this module with saved example responses. A collection that silently falls
behind the code is worse than none, so the coverage is checked here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.app import create_app
from src.common.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTION = (
    REPO_ROOT / "docs" / "postman" / "ap-autopilot-spec-01.postman_collection.json"
)

_PARAM = re.compile(r"\{\{?[^}]+\}?\}")


def normalise(path: str) -> str:
    """Reduce a path to its shape, so parameter spellings do not matter."""
    return _PARAM.sub(":param", path.rstrip("/")) or "/"


def load() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(COLLECTION.read_text("utf-8"))
    return payload


def walk_requests(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the collection's folder tree into its requests."""
    found: list[dict[str, Any]] = []
    for item in node.get("item", []):
        if "request" in item:
            found.append(item)
        else:
            found.extend(walk_requests(item))
    return found


def collection_paths() -> set[tuple[str, str]]:
    """Method and path shape for every request in the collection."""
    pairs: set[tuple[str, str]] = set()
    for item in walk_requests(load()):
        request = item["request"]
        url = request["url"]
        raw = url["raw"] if isinstance(url, dict) else str(url)
        without_query = raw.split("?", 1)[0]
        path = without_query.replace("{{baseUrl}}", "")
        if path.startswith("http"):
            continue  # a Google API call, not one of our routes
        pairs.add((str(request["method"]).upper(), normalise(path)))
    return pairs


def app_routes() -> set[tuple[str, str]]:
    """Method and path shape for every route the application serves.

    Read from the OpenAPI schema rather than app.routes, because FastAPI
    nests included routers and the nesting has changed between versions.
    """
    app = create_app(Settings(replay_fixtures=True, oidc_dev_token="x"))
    schema = app.openapi()
    return {
        (method.upper(), normalise(path))
        for path, operations in schema.get("paths", {}).items()
        for method in operations
        if method.upper() not in {"HEAD", "OPTIONS"}
    }


def test_the_collection_is_valid_json_with_a_schema() -> None:
    payload = load()
    assert "schema" in payload["info"]
    assert payload["info"]["schema"].endswith("collection.json")


def test_every_route_appears_in_the_collection() -> None:
    missing = app_routes() - collection_paths()
    assert not missing, f"Postman collection is missing: {sorted(missing)}"


def test_the_collection_describes_no_route_that_does_not_exist() -> None:
    extra = collection_paths() - app_routes()
    assert not extra, f"Postman collection describes unknown routes: {sorted(extra)}"


def test_every_request_has_at_least_one_saved_example() -> None:
    """Section 2 asks for saved examples, not bare requests."""
    without = [
        item["name"] for item in walk_requests(load()) if not item.get("response")
    ]
    assert not without, f"requests with no saved example: {without}"


def test_the_document_ai_calls_are_covered() -> None:
    """Section 2 asks for the Document AI calls as well as our endpoints."""
    google = {
        item["name"]
        for item in walk_requests(load())
        if str(item["request"]["url"]["raw"]).startswith("http")
    }
    assert len(google) >= 4


@pytest.mark.parametrize("code", [200, 202, 401, 404, 413, 415])
def test_the_significant_status_codes_are_all_shown(code: int) -> None:
    """A collection that only shows the happy path teaches nothing."""
    codes = {
        int(example["code"])
        for item in walk_requests(load())
        for example in item.get("response", [])
    }
    assert code in codes


def test_the_collection_carries_no_real_credential() -> None:
    text = COLLECTION.read_text("utf-8")
    assert "REPLACE_WITH_PROCESSOR_ID" in text
    assert "change-me-in-local-only" in text
