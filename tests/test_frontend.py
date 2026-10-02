import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import frontend_serving
from app.main import FRONTEND_DIR, app

client = TestClient(app)
INDEX = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")


def test_app_path_serves_index_html() -> None:
    response = client.get("/app/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Log in" in response.text  # 처음 화면은 영어 (i18n.js가 고른 언어로 바꾼다)
    assert 'id="login-form"' in response.text and 'id="signup-invite"' in response.text
    assert "user-id" not in response.text and "X-User-Id" not in response.text


def test_app_without_trailing_slash_redirects_to_index() -> None:
    response = client.get("/app", follow_redirects=False)

    assert response.status_code in (301, 307, 308)
    assert response.headers["location"].endswith("/app/")
    assert client.get("/app").status_code == 200


@pytest.mark.parametrize(
    ("asset", "content_type"),
    [("styles.css", "text/css"), ("api.js", "javascript"), ("app.js", "javascript")],
)
def test_assets_are_served_with_correct_type(asset: str, content_type: str) -> None:
    response = client.get(f"/app/{asset}")

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


def test_every_asset_referenced_by_index_exists() -> None:
    references = re.findall(r'(?:src|href)="([^"#:]+)"', INDEX)
    local = [ref for ref in references if not ref.startswith("/")]

    assert {"styles.css", "app.js"} <= set(local)
    for ref in local:
        assert (FRONTEND_DIR / ref).is_file(), ref
        assert client.get(f"/app/{ref}").status_code == 200


def test_layout_has_five_tabs_with_calendar_first() -> None:
    tabs = re.findall(r'role="tab"[^>]*aria-controls="([\w-]+)"[^>]*data-i18n="([\w.]+)"[^>]*>([^<]+)<', INDEX)

    assert [key for _, key, _ in tabs] == ["tab.calendar", "tab.events", "tab.tasks", "tab.points", "tab.chat"]
    assert [label for _, _, label in tabs] == ["Calendar", "Events", "Tasks", "Points", "Persona chat"]
    for panel_id, _, _ in tabs:
        assert f'id="{panel_id}"' in INDEX
    # 첫 탭(캘린더) 패널만 처음부터 보이고 나머지는 숨겨져 있다 (JS가 켜지기 전에도 한 섹션만 보이게)
    assert re.search(r'id="panel-calendar"[^>]*role="tabpanel"[^>]*>', INDEX).group(0).find("hidden") == -1
    assert len(re.findall(r'role="tabpanel"[^>]*hidden', INDEX)) == 4


def test_missing_file_is_404_and_api_routes_still_work() -> None:
    assert client.get("/app/nope.js").status_code == 404
    assert client.get("/health").json()["status"] == "ok"
    assert "/app" not in app.openapi()["paths"]


def test_frontend_is_copied_into_docker_image() -> None:
    assert "COPY frontend ./frontend" in Path("Dockerfile").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js가 없어 프론트엔드 JS 테스트를 건너뛴다")
def test_frontend_js_unit_tests() -> None:
    test_files = sorted(str(path) for path in Path("tests/frontend").glob("*.test.mjs"))
    result = subprocess.run(
        ["node", "--test", *test_files],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("asset", ["app.js", "i18n.js", "styles.css"])
def test_directly_requested_files_are_revalidated(asset: str) -> None:
    first = client.get(f"/app/{asset}")

    assert first.headers["cache-control"] == "no-cache"
    again = client.get(f"/app/{asset}", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_index_points_scripts_at_a_versioned_path_so_edits_show_up(tmp_path, monkeypatch) -> None:
    """index.html은 매번 새로 받고, app.js·styles.css는 파일이 바뀌면 달라지는 /app/v/<버전>/ 주소로 불러온다.
    app.js가 import하는 모듈도 같은 버전 경로 아래에서 풀리므로 예전에 캐시한 모듈을 쓰지 않는다."""
    response = client.get("/app/")

    assert response.headers["cache-control"] == "no-cache"
    version = re.search(r'src="v/([0-9a-f]{12})/app\.js"', response.text).group(1)
    assert f'href="v/{version}/styles.css"' in response.text
    module = client.get(f"/app/v/{version}/api.js")
    assert module.status_code == 200 and "javascript" in module.headers["content-type"]
    assert "immutable" in module.headers["cache-control"]
    assert client.get(f"/app/v/{version}/../app/main.py").status_code == 404

    site = tmp_path / "frontend"
    site.mkdir()
    (site / "index.html").write_text('<script src="app.js"></script>', encoding="utf-8")
    (site / "app.js").write_text("1", encoding="utf-8")
    before = frontend_serving.frontend_version(site)
    (site / "app.js").write_text("22", encoding="utf-8")
    assert frontend_serving.frontend_version(site) != before, "파일을 고치면 버전(주소)이 바뀐다"
