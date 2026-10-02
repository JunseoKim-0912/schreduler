"""Click through the web UI in a real browser and fail on any console error or failed request.

    python -m app.scripts.smoke_ui                     # starts its own server on a throwaway database
    python -m app.scripts.smoke_ui --headed            # watch it
    SMOKE_EMAIL=... SMOKE_PASSWORD=... python -m app.scripts.smoke_ui --base-url https://<your domain>
    ... --base-url https://<your domain> --with-demo   # also start a demo account there (deleted after 24 hours)
    python -m app.scripts.smoke_ui --base-url https://<your domain> --demo-only   # no credentials: demo path only

Steps: login screen (prototype notice, About opens and closes) -> sign in -> open every tab -> log out -> login screen
again, then the demo: [Try the demo]
-> demo banner -> every tab -> [Exit demo] -> login screen. Nothing here calls the LLM: the local server gets no
LLM_API_KEY and the browser refuses any request to an LLM endpoint. Against --base-url it only reads unless
--with-demo is given (the demo account it starts is a throwaway, removed by the hourly cleanup after 24 hours).

Needs Playwright (pip install -r requirements-dev.txt) and an installed Chrome or Edge (--browser), so no browser
download is required.
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
LLM_PATHS = ("/assistant/chat", "/daily-actual-logs/checkin", "/compliance-reports")
SMOKE_EMAIL = "smoke@test.local"


@dataclass
class Report:
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.console_errors and not self.failed_requests


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base_url: str, process: subprocess.Popen[bytes], timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"the server exited early (code {process.returncode})")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise SystemExit("the server did not answer /health in time")


def _create_account(database_url: str, email: str, password: str) -> None:
    from app.models.user import User
    from app.services.auth_service import hash_password

    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(User(name="Smoke", email=email, password_hash=hash_password(password), preferred_language="en"))
        session.commit()
    engine.dispose()


def _stop(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        # A venv's python.exe is a launcher that starts the real interpreter as a child; stop the whole tree.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


@contextmanager
def local_server() -> Iterator[tuple[str, str, str]]:
    """A fresh server on a temporary SQLite file with one account and the default personas."""
    with tempfile.TemporaryDirectory(prefix="schreduler-smoke-", ignore_cleanup_errors=True) as tmp:
        database_url = f"sqlite:///{(Path(tmp) / 'smoke.db').as_posix()}"
        env = {
            **os.environ,
            "DATABASE_URL": database_url,
            "BACKUP_DIR": str(Path(tmp) / "backups"),
            "RUN_SCHEDULER": "false",
            "LLM_API_KEY": "",
            "SIGNUP_MODE": "closed",
            "DEMO_MODE_ENABLED": "true",
            "SESSION_COOKIE_SECURE": "false",
            "ALLOWED_ORIGINS": "",
            "TRUST_PROXY_HEADERS": "false",
            "FIREBASE_CREDENTIALS_PATH": "",
            "FIREBASE_CREDENTIALS_JSON": "",
            "TELEGRAM_BOT_TOKEN": "",
        }
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True)
        subprocess.run([sys.executable, "-m", "app.scripts.seed_personas"], cwd=ROOT, env=env, check=True, capture_output=True)
        password = secrets.token_urlsafe(12)
        _create_account(database_url, SMOKE_EMAIL, password)

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        log = open(Path(tmp) / "server.log", "wb")  # noqa: SIM115 - closed below, after the process
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(base_url, process)
            yield base_url, SMOKE_EMAIL, password
        finally:
            _stop(process)
            log.close()


def run_browser(
    base_url: str, email: str | None, password: str | None, *, browser: str, headed: bool, seed: bool, demo: bool
) -> Report:
    """Without an email the signed-in account part is skipped (--demo-only)."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    report = Report()
    signed_in = False

    def on_console(message) -> None:  # noqa: ANN001 - Playwright ConsoleMessage
        if message.type != "error":
            return
        url = (message.location or {}).get("url", "")
        # Before signing in, GET /auth/me answers 401 by design and Chrome logs it as a failed resource.
        if not signed_in and "Failed to load resource" in message.text and url.endswith("/auth/me"):
            return
        report.console_errors.append(f"{message.text} ({url})")

    def on_response(response) -> None:  # noqa: ANN001
        path = response.url.removeprefix(base_url)
        if response.status < 400:
            return
        if path.startswith("/auth/me") and response.status == 401 and not signed_in:
            return
        report.failed_requests.append(f"{response.request.method} {path} -> {response.status}")

    with sync_playwright() as playwright:
        launcher = playwright.chromium
        options = {"headless": not headed}
        if browser != "chromium":
            options["channel"] = browser
        try:
            instance = launcher.launch(**options)
        except PlaywrightError as exc:
            raise SystemExit(f"could not start {browser}: {exc.message.splitlines()[0]} (try --browser msedge)") from exc
        context = instance.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("console", on_console)
        page.on("pageerror", lambda error: report.console_errors.append(f"uncaught: {error}"))
        page.on("requestfailed", lambda request: report.failed_requests.append(f"{request.method} {request.url} ({request.failure})"))
        page.on("response", on_response)

        def refuse_llm(route) -> None:  # noqa: ANN001
            report.failed_requests.append(f"LLM endpoint was called: {route.request.method} {route.request.url}")
            route.abort()

        for path in LLM_PATHS:
            page.route(f"**{path}", refuse_llm)

        page.goto(f"{base_url}/app/", wait_until="networkidle")
        page.locator("#login-form").wait_for(state="visible", timeout=10_000)
        report.steps.append("login screen shown")

        notice = page.text_content("#proto-notice") or ""
        if not page.locator("#proto-notice").is_visible() or "Early prototype." not in notice:
            report.failed_requests.append(f"prototype notice missing on the login screen: {notice!r}")
        else:
            report.steps.append("prototype notice shown")

        def check_about(close_with: str) -> None:
            page.locator("#about-open").filter(has_text="Prototype · v").wait_for(timeout=5_000)
            page.click("#about-open")
            dialog = page.locator("#about-dialog")
            dialog.wait_for(state="visible", timeout=5_000)
            if dialog.locator('a[href="https://github.com/JunseoKim-0912/schreduler"]').count() != 1:
                report.failed_requests.append("About has no GitHub link")
            box = dialog.bounding_box()
            if box is None or box["width"] > page.viewport_size["width"]:
                report.failed_requests.append(f"About does not fit the screen: {box}")
            if close_with == "Escape":
                page.keyboard.press("Escape")
            else:
                page.click("#about-close")
            dialog.wait_for(state="hidden", timeout=5_000)
            report.steps.append(f"About opened and closed ({close_with})")

        check_about("button")

        def open_every_tab(who: str) -> None:
            tabs = page.locator('[role="tab"]')
            for index in range(tabs.count()):
                tab = tabs.nth(index)
                name = tab.get_attribute("data-tab")
                tab.click()
                page.wait_for_load_state("networkidle")
                page.locator(f"#panel-{name}").wait_for(state="visible", timeout=5_000)
                report.steps.append(f"{who}: tab {name} opened")

        if email and password:
            page.fill("#login-email", email)
            page.fill("#login-password", password)
            signed_in = True
            page.click("#login-submit")
            page.locator("#account").wait_for(state="visible", timeout=10_000)
            report.steps.append(f"signed in as {page.text_content('#account-email')}")

            if seed:
                # A little data so the lists and the calendar have something to draw (local throwaway server only).
                page.request.post(f"{base_url}/tasks", data={"title": "Smoke task", "end_time": _tomorrow_at(18)})
                page.request.post(
                    f"{base_url}/events", data={"title": "Smoke event", "start_time": _tomorrow_at(9), "end_time": _tomorrow_at(10)}
                )

            open_every_tab("account")
            check_about("Escape")

            page.click("#logout")
            signed_in = False
            page.locator("#login-form").wait_for(state="visible", timeout=10_000)
            page.wait_for_load_state("networkidle")
            report.steps.append("logged out, login screen shown again")

        if demo:
            page.locator("#demo-start").wait_for(state="visible", timeout=10_000)
            signed_in = True
            page.click("#demo-start")
            page.locator("#demo-banner").wait_for(state="visible", timeout=20_000)
            banner = page.text_content("#demo-banner-text") or ""
            if "Demo account" not in banner or not page.locator("#account").is_hidden():
                report.failed_requests.append(f"demo banner looks wrong: {banner!r}")
            demo_id = page.evaluate("fetch('/auth/me').then((r) => r.json()).then((me) => me.id)")
            report.steps.append(f"demo started (user id {demo_id}): {banner}")
            open_every_tab("demo")
            check_about("button")
            if page.locator("#event-list li").count() == 0:
                report.failed_requests.append("the demo account has no sample events")
            page.click("#demo-exit")
            signed_in = False
            page.locator("#login-form").wait_for(state="visible", timeout=10_000)
            page.wait_for_load_state("networkidle")
            report.steps.append("exited the demo, login screen shown again")
        context.close()
        instance.close()
    return report


def _tomorrow_at(hour: int) -> str:
    from datetime import datetime, time as dt_time, timedelta

    from app.core.clock import local_today

    return datetime.combine(local_today() + timedelta(days=1), dt_time(hour)).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", help="check a running deployment instead (SMOKE_EMAIL / SMOKE_PASSWORD env vars)")
    parser.add_argument("--browser", default="chrome", choices=["chrome", "msedge", "chromium"], help="installed browser to drive")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--with-demo", action="store_true", help="with --base-url: also start a demo account there")
    parser.add_argument("--demo-only", action="store_true", help="with --base-url: no credentials, check only the demo path")
    args = parser.parse_args(argv)

    if args.base_url:
        email, password = os.environ.get("SMOKE_EMAIL"), os.environ.get("SMOKE_PASSWORD")
        if args.demo_only:
            email = password = None
        elif not email or not password:
            parser.error("--base-url needs SMOKE_EMAIL and SMOKE_PASSWORD in the environment")
        report = run_browser(
            args.base_url.rstrip("/"), email, password, browser=args.browser, headed=args.headed, seed=False, demo=args.with_demo or args.demo_only
        )
    else:
        with local_server() as (base_url, email, password):
            report = run_browser(base_url, email, password, browser=args.browser, headed=args.headed, seed=True, demo=True)

    for step in report.steps:
        print(f"  ok  {step}")
    for problem in report.console_errors:
        print(f"  console error: {problem}")
    for problem in report.failed_requests:
        print(f"  failed request: {problem}")
    print(f"smoke_ui: {'PASS' if report.ok else 'FAIL'} — {len(report.console_errors)} console errors, {len(report.failed_requests)} failed requests")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
