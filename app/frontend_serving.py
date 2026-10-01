"""정적 프론트엔드(frontend/) 서빙과 캐시 무효화.

index.html은 매번 새로 받게(no-cache) 하고, 그 안의 app.js·styles.css는 /app/v/<버전>/ 아래 주소로 바꿔 내보낸다.
버전은 프론트엔드 파일들의 크기·수정 시각으로 만든 해시라 파일을 고치면 주소가 바뀐다. app.js가 import하는 모듈
("./api.js")도 같은 버전 경로 아래에서 풀리므로, 브라우저가 예전에 캐시한 파일을 계속 쓰는 일이 없다. 버전 경로의
파일은 내용이 바뀌면 주소도 바뀌므로 오래 캐시해도 된다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
VERSIONED_ASSETS = ("app.js", "styles.css")

router = APIRouter(include_in_schema=False)


def frontend_version(directory: Path = FRONTEND_DIR) -> str:
    """frontend/ 파일들의 이름·크기·수정 시각 해시 (앞 12자). 파일 하나라도 바뀌면 달라진다."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        stat = path.stat()
        digest.update(f"{path.relative_to(directory).as_posix()}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:12]


def versioned_index(directory: Path = FRONTEND_DIR) -> str:
    html = (directory / "index.html").read_text(encoding="utf-8")
    version = frontend_version(directory)
    for asset in VERSIONED_ASSETS:
        html = html.replace(f'="{asset}"', f'="v/{version}/{asset}"')
    return html


@router.get("/app/")
@router.get("/app/index.html")
def get_index() -> HTMLResponse:
    return HTMLResponse(versioned_index(), headers={"Cache-Control": "no-cache"})


@router.get("/app/v/{version}/{path:path}")
def get_versioned_asset(version: str, path: str) -> FileResponse:
    target = (FRONTEND_DIR / path).resolve()
    if FRONTEND_DIR not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})


class RevalidatedStaticFiles(StaticFiles):
    """버전 경로 밖에서 직접 받는 파일(/app/app.js 등)은 매번 바뀌었는지 묻게 한다 (no-cache + ETag → 안 바뀌었으면 304)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response
