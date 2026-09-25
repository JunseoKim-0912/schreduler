import re

import pytest

from app.core.openapi import TAGS_METADATA
from app.main import app

SPEC = app.openapi()
OPERATIONS = [(method, path, op) for path, ops in SPEC["paths"].items() for method, op in ops.items()]
HANGUL = re.compile(r"[가-힣]")


def test_app_metadata() -> None:
    assert SPEC["info"]["title"] == "Schreduler"
    assert "X-User-Id" in SPEC["info"]["description"]
    assert [tag["name"] for tag in SPEC["tags"]] == [tag["name"] for tag in TAGS_METADATA]


def test_every_tag_in_use_is_described_and_every_described_tag_is_used() -> None:
    used = {tag for _, _, op in OPERATIONS for tag in op.get("tags", [])}
    described = {tag["name"] for tag in SPEC["tags"]}

    assert used == described
    assert all(tag["description"] for tag in SPEC["tags"])


@pytest.mark.parametrize(("method", "path", "operation"), OPERATIONS, ids=[f"{m.upper()} {p}" for m, p, _ in OPERATIONS])
def test_every_operation_is_documented(method: str, path: str, operation: dict) -> None:
    assert len(operation.get("tags", [])) == 1
    assert HANGUL.search(operation.get("summary", "")), "요약이 자동 생성된 영어 함수명 그대로다"
    assert operation.get("description", "").strip()


def test_user_header_and_query_params_are_described() -> None:
    for _, _, operation in OPERATIONS:
        for param in operation.get("parameters", []):
            if param["in"] in ("query", "header"):
                assert param.get("description"), (operation["summary"], param["name"])


def test_request_bodies_have_examples() -> None:
    for _, _, operation in OPERATIONS:
        body = operation.get("requestBody")
        if body is None:
            continue
        name = body["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
        assert SPEC["components"]["schemas"][name].get("examples"), name
