import re
from pathlib import Path

from app.main import app

DOC = Path("docs/api_overview.md").read_text(encoding="utf-8")
# 엔드포인트 목록 표의 첫 칸: | `GET /events/{event_id}` ...
TABLE_ENDPOINT = re.compile(r"^\| `(GET|POST|PUT|DELETE) (/[^`]*)`", re.M)
USER_HEADER_ENDPOINTS = {
    (method.upper(), path)
    for path, ops in app.openapi()["paths"].items()
    for method, op in ops.items()
    if any(p["name"] == "x-user-id" for p in op.get("parameters", []))
}


def _spec_endpoints() -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops}


def test_endpoint_tables_match_the_api_exactly() -> None:
    documented = set(TABLE_ENDPOINT.findall(DOC))
    spec = _spec_endpoints()

    assert not spec - documented, f"문서에 빠진 엔드포인트 (docs/api_overview.md 4절에 추가): {sorted(spec - documented)}"
    assert not documented - spec, f"더 이상 없는 엔드포인트가 문서에 남아 있음: {sorted(documented - spec)}"


def _unmarked_header_endpoints(doc: str) -> list[tuple[str, str]]:
    """X-User-Id가 필요한데 행에도, 소속 섹션 제목에도 🔑가 없는 엔드포인트."""
    unmarked = []
    for section in doc.split("\n### ")[1:]:
        heading, _, body = section.partition("\n")
        for row in body.splitlines():
            match = TABLE_ENDPOINT.match(row)
            if match and match.groups() in USER_HEADER_ENDPOINTS and "🔑" not in heading + row:
                unmarked.append(match.groups())
    return unmarked


def test_header_auth_endpoints_are_marked() -> None:
    assert _unmarked_header_endpoints(DOC) == []


def test_header_mark_check_catches_a_missing_mark() -> None:
    broken = DOC.replace("| `GET /compliance-reports/categories` 🔑", "| `GET /compliance-reports/categories`")

    assert _unmarked_header_endpoints(broken) == [("GET", "/compliance-reports/categories")]


def test_llm_endpoints_are_marked() -> None:
    for method, path in [("POST", "/events/parse"), ("POST", "/daily-actual-logs/checkin"), ("POST", "/compliance-reports")]:
        assert re.search(rf"^\| `{method} {re.escape(path)}` 🤖", DOC, re.M), (method, path)


def test_documented_enum_values_match_the_api() -> None:
    schemas = app.openapi()["components"]["schemas"]
    for schema_name in ("EventType", "EventInstanceStatus", "ChildEventKind", "NonComplianceCategory"):
        for value in schemas[schema_name]["enum"]:
            assert f"`{value}`" in DOC, (schema_name, value)
