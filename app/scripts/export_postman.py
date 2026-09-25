"""FastAPI OpenAPI 스펙으로 Postman Collection(v2.1) 파일을 만든다.

API가 바뀌면 이 스크립트를 다시 실행해 컬렉션을 갱신한다 (tests/scripts/test_export_postman.py가
커밋된 파일이 최신인지 검사한다).

실행 (프로젝트 루트에서): python -m app.scripts.export_postman [출력 경로]
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

from app.main import app

DEFAULT_OUTPUT = Path("docs/postman/Schreduler.postman_collection.json")
POSTMAN_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
COLLECTION_VARIABLES = [
    {"key": "baseUrl", "value": "http://localhost:8000", "description": "API 서버 주소"},
    {"key": "userId", "value": "1", "description": "X-User-Id 헤더와 요청 본문의 user_id에 쓰는 사용자 id"},
]
# 경로 변수 기본값. 없으면 "1".
PATH_VARIABLE_DEFAULTS = {"name": "Hana"}
USER_HEADER = "x-user-id"


def _resolve_schema(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref is None:
        return schema
    return spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]


def _request_examples(spec: dict[str, Any], operation: dict[str, Any]) -> list[Any]:
    body = operation.get("requestBody")
    if body is None:
        return []
    schema = _resolve_schema(spec, body["content"]["application/json"]["schema"])
    # OpenAPI 생성 시 예시 키가 알파벳순으로 정렬되므로, 읽기 쉽게 스키마 필드 순서로 되돌린다.
    field_order = list(schema.get("properties", {}))
    return [
        {key: example[key] for key in sorted(example, key=lambda k: field_order.index(k) if k in field_order else len(field_order))}
        for example in schema.get("examples") or [{}]
    ]


def _raw_body(example: Any) -> str:
    raw = json.dumps(example, ensure_ascii=False, indent=2)
    # 컬렉션 변수로 바꿔 두면 userId만 바꿔서 여러 사용자로 호출해 볼 수 있다 (Postman은 raw 본문의 {{var}}를 치환한다).
    return re.sub(r'"user_id": 1\b', '"user_id": {{userId}}', raw)


def _url(path: str, operation: dict[str, Any]) -> dict[str, Any]:
    postman_path = re.sub(r"\{(\w+)\}", r":\1", path)
    parameters = operation.get("parameters", [])

    query = []
    for param in parameters:
        if param["in"] != "query":
            continue
        schema = param.get("schema", {})
        value = "{{userId}}" if param["name"] == "user_id" else str(schema.get("default", ""))
        query.append(
            {
                "key": param["name"],
                "value": value,
                "description": param.get("description", ""),
                "disabled": not param.get("required", False),
            }
        )

    variables = [
        {"key": param["name"], "value": PATH_VARIABLE_DEFAULTS.get(param["name"], "1")}
        for param in parameters
        if param["in"] == "path"
    ]

    enabled_query = "&".join(f"{q['key']}={q['value']}" for q in query if not q["disabled"])
    url: dict[str, Any] = {
        "raw": "{{baseUrl}}" + postman_path + (f"?{enabled_query}" if enabled_query else ""),
        "host": ["{{baseUrl}}"],
        "path": [segment for segment in postman_path.split("/") if segment],
    }
    if query:
        url["query"] = query
    if variables:
        url["variable"] = variables
    return url


def _description(operation: dict[str, Any]) -> str:
    lines = [operation.get("description", "").strip()]
    errors = {code: r["description"] for code, r in operation.get("responses", {}).items() if not code.startswith("2")}
    if errors:
        lines.append("\n**오류 응답**\n" + "\n".join(f"- `{code}`: {text}" for code, text in sorted(errors.items())))
    return "\n".join(line for line in lines if line)


def _items(spec: dict[str, Any], path: str, method: str, operation: dict[str, Any]) -> list[dict[str, Any]]:
    headers = []
    if any(p["in"] == "header" and p["name"].lower() == USER_HEADER for p in operation.get("parameters", [])):
        headers.append({"key": "X-User-Id", "value": "{{userId}}"})

    examples = _request_examples(spec, operation)
    bodies = [_raw_body(example) for example in examples] or [None]

    items = []
    for index, raw in enumerate(bodies):
        request: dict[str, Any] = {
            "method": method.upper(),
            "header": list(headers),
            "url": _url(path, operation),
            "description": _description(operation),
        }
        if raw is not None:
            request["header"].append({"key": "Content-Type", "value": "application/json"})
            request["body"] = {"mode": "raw", "raw": raw, "options": {"raw": {"language": "json"}}}
        name = operation["summary"] if index == 0 else f"{operation['summary']} (예시 {index + 1})"
        items.append({"name": name, "request": request, "response": []})
    return items


def build_collection(spec: dict[str, Any]) -> dict[str, Any]:
    folders: dict[str, dict[str, Any]] = {
        tag["name"]: {"name": tag["name"], "description": tag.get("description", ""), "item": []}
        for tag in spec.get("tags", [])
    }
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            tag = operation["tags"][0]
            folder = folders.setdefault(tag, {"name": tag, "description": "", "item": []})
            folder["item"].extend(_items(spec, path, method, operation))

    return {
        "info": {
            "name": f"{spec['info']['title']} API",
            "description": spec["info"].get("description", "").strip(),
            "version": spec["info"]["version"],
            "schema": POSTMAN_SCHEMA,
        },
        "variable": COLLECTION_VARIABLES,
        "item": [folder for folder in folders.values() if folder["item"]],
    }


def render_collection() -> str:
    return json.dumps(build_collection(app.openapi()), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_collection(), encoding="utf-8")
    collection = json.loads(output.read_text(encoding="utf-8"))
    count = sum(len(folder["item"]) for folder in collection["item"])
    print(f"Postman 컬렉션 생성: {output} (폴더 {len(collection['item'])}개, 요청 {count}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
