import importlib
import json
import pkgutil
from pathlib import Path

import pytest
from pydantic import BaseModel

import app.schemas as schemas_package
from app.main import app
from app.scripts.export_postman import DEFAULT_OUTPUT, POSTMAN_SCHEMA, build_collection, render_collection

SPEC = app.openapi()
COLLECTION = build_collection(SPEC)
REQUESTS = [item for folder in COLLECTION["item"] for item in folder["item"]]


def _schema_classes() -> dict[str, type[BaseModel]]:
    classes: dict[str, type[BaseModel]] = {}
    for module_info in pkgutil.iter_modules(schemas_package.__path__):
        module = importlib.import_module(f"app.schemas.{module_info.name}")
        for name, value in vars(module).items():
            if isinstance(value, type) and issubclass(value, BaseModel):
                classes[name] = value
    return classes


def test_committed_collection_is_up_to_date() -> None:
    committed = Path(DEFAULT_OUTPUT).read_text(encoding="utf-8")

    assert committed == render_collection(), "API가 바뀌었다 — python -m app.scripts.export_postman 으로 다시 생성하세요"


def test_collection_uses_postman_v21_format_and_variables() -> None:
    assert COLLECTION["info"]["schema"] == POSTMAN_SCHEMA
    assert {v["key"] for v in COLLECTION["variable"]} == {"baseUrl", "userId"}
    for item in REQUESTS:
        request = item["request"]
        assert request["url"]["raw"].startswith("{{baseUrl}}/")
        assert request["url"]["host"] == ["{{baseUrl}}"]
        assert request["method"] in {"GET", "POST", "PUT", "DELETE"}


def test_every_operation_is_in_the_collection_under_its_tag() -> None:
    expected = {
        (op["tags"][0], method.upper(), path.replace("{", ":").replace("}", ""))
        for path, ops in SPEC["paths"].items()
        for method, op in ops.items()
    }
    actual = {
        (folder["name"], item["request"]["method"], "/" + "/".join(item["request"]["url"]["path"]))
        for folder in COLLECTION["item"]
        for item in folder["item"]
    }

    assert actual == expected
    assert [folder["name"] for folder in COLLECTION["item"]] == [tag["name"] for tag in SPEC["tags"]]


def test_user_header_only_where_the_api_reads_it() -> None:
    needs_header = {
        (method.upper(), path.replace("{", ":").replace("}", ""))
        for path, ops in SPEC["paths"].items()
        for method, op in ops.items()
        if any(p["name"] == "x-user-id" for p in op.get("parameters", []))
    }

    for item in REQUESTS:
        request = item["request"]
        key = (request["method"], "/" + "/".join(request["url"]["path"]))
        has_header = {"key": "X-User-Id", "value": "{{userId}}"} in request["header"]
        assert has_header == (key in needs_header), item["name"]


@pytest.mark.parametrize("item", [i for i in REQUESTS if "body" in i["request"]], ids=lambda i: i["name"])
def test_example_bodies_pass_schema_validation(item: dict) -> None:
    request = item["request"]
    path = "/" + "/".join(request["url"]["path"])
    operation = next(
        op
        for p, ops in SPEC["paths"].items()
        for m, op in ops.items()
        if m.upper() == request["method"] and p.replace("{", ":").replace("}", "") == path
    )
    schema_name = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    body = json.loads(request["body"]["raw"].replace("{{userId}}", "1"))

    _schema_classes()[schema_name].model_validate(body)


def test_multiple_examples_become_separate_requests() -> None:
    names = [item["name"] for item in REQUESTS]

    assert "이벤트 생성 (예시 2)" in names
    assert "할 일(마감) 생성 (예시 2)" in names
    assert len(names) == len(set(names))
