"""Built React → secured HTTP API → isolated PostgreSQL account acceptance journeys."""

import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from tender_system.api.api import create_app
from tender_system.api.auth import password_hash
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import Configuration
from tender_system.persistence.storage import TelegramNotificationRow, UserRow, session_factory
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.web import WebFixture, web_fixture

pytestmark = [pytest.mark.database, pytest.mark.browser]
ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts/account-browser"
PASSWORD = "account-browser-fixture-password"


@pytest.fixture
def account_workspace(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, WebFixture, str, list[str], list[str]]]:
    """Existing fixture owns source/model boundaries; public app enforces authentication."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    from tender_system.collection.documents import DocumentProcessor
    from tender_system.operator import telegram

    downloads: list[str] = []
    deliveries: list[str] = []
    original_init = DocumentProcessor.__init__

    def fetch(url: str, headers: object, budget: object) -> tuple[int, bytes, dict[str, str]]:
        downloads.append(url)
        return 200, b"Network router specification", {"content-type": "text/plain"}

    def init(self: DocumentProcessor, sessions: sessionmaker[Session]) -> None:
        original_init(self, sessions, fetch=fetch)

    def send(destination: str, message: str, *args: object) -> None:
        deliveries.append(destination)

    monkeypatch.setattr(DocumentProcessor, "__init__", init)
    monkeypatch.setattr(telegram, "send_email", send)
    config = Configuration.model_validate(
        {
            "global": {"source": fixture_source_settings().model_dump(mode="json")},
            "runtime": {
                "matching_enabled": True,
                "ai_semantic_enabled": True,
                "manual_only": False,
                "operator_worker_enabled": True,
                "worker_poll_seconds": 0.1,
            },
        }
    )
    with web_fixture(migrated_engine, config) as fixture:
        fixture.document_urls = ["https://documents.example.test/spec.txt"]
        # Setup uses the explicit legacy operator fixture; browser gets only the secured app.
        operator = TestClient(fixture.app)
        added = operator.post("/api/v1/web/sources", json={"url": fixture.source_url})
        assert added.status_code == 201, added.text
        source_id = str(added.json()["source_id"])
        saved = operator.post(
            f"/api/v1/web/sources/{source_id}/recipe",
            json={"text": fixture.recipe, "max_tenders": 2},
        )
        assert saved.status_code == 202, saved.text
        activated = operator.get(f"/api/v1/web/operations/{saved.json()['id']}")
        assert activated.json()["status"] == "activated", activated.text
        configured = operator.patch(f"/api/v1/web/sources/{source_id}", json={"manual_only": False})
        assert configured.status_code == 200, configured.text
        sessions = session_factory(migrated_engine)
        with sessions.begin() as session:
            session.add(
                UserRow(
                    id=uuid4(),
                    email="admin@account-fixture.example",
                    password_hash=password_hash(PASSWORD),
                    role="admin",
                    active=True,
                    created_at=SystemClock().now(),
                )
            )
        app = create_app(
            sessions, fixture.runtime.config, runtime=fixture.runtime, operator_mode=False
        )
        app.mount("/", StaticFiles(directory=ROOT / "frontend/dist", html=True))
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if server.started and httpx.get(url, timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Secured account test server unavailable")
            yield url, fixture, source_id, downloads, deliveries
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            operator.close()


def account_navigate(page: Page, name: str) -> None:
    menu = page.get_by_role("button", name="Open menu", exact=True)
    if menu.is_visible():
        menu.click()
    page.get_by_role("navigation", name="Main navigation").get_by_role(
        "link", name=name, exact=True
    ).click()
    expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()


def sign_in(page: Page, email: str) -> None:
    expect(page.get_by_role("heading", name="Sign in to Quarry")).to_be_visible()
    page.get_by_label("Email address", exact=True).fill(email)
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Tenders", exact=True)).to_be_visible()


def register_account(page: Page, email: str) -> None:
    page.get_by_role("button", name="Create an account", exact=True).click()
    page.get_by_label("Email address", exact=True).fill(email)
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Create account", exact=True).click()
    expect(page.get_by_role("heading", name="Monitoring", exact=True)).to_be_visible()


def accessible(page: Page, label: str) -> None:
    page.add_script_tag(path=str(ROOT / "frontend/node_modules/axe-core/axe.min.js"))
    violations = page.evaluate(
        """async () => (await axe.run(document, {
            runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa']}
        })).violations.map(v => ({id: v.id, nodes: v.nodes.map(n => n.target)}))"""
    )
    (ARTIFACTS / f"{label}-a11y.json").write_text(json.dumps(violations, indent=2))
    assert violations == [], f"{label}: {violations}"


def responsive(page: Page, label: str) -> None:
    for width in (320, 375, 414, 768, 1365):
        page.set_viewport_size({"width": width, "height": 900})
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth"), (
            label,
            width,
        )
        page.screenshot(path=str(ARTIFACTS / f"{label}-{width}.png"), full_page=True)


def test_self_service_and_administration_browser_journeys(
    account_workspace: tuple[str, WebFixture, str, list[str], list[str]],
) -> None:
    url, fixture, source_id, downloads, deliveries = account_workspace
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1365, "height": 900})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            expect(page.get_by_role("heading", name="Sign in to Quarry")).to_be_visible()
            accessible(page, "login")
            responsive(page, "login")
            register_account(page, "alice@account-fixture.example")
            assert page.get_by_role("link", name="Administration", exact=True).count() == 0
            assert page.get_by_role("link", name="Matching", exact=True).count() == 0
            page.get_by_role("button", name="Create campaign", exact=True).click()
            dialog = page.get_by_role("dialog", name="Create monitoring campaign")
            dialog.get_by_label("Campaign name", exact=True).fill("Network procurement")
            dialog.get_by_role("checkbox", name=re.compile("APPROVED")).check()
            dialog.get_by_label("Business requirements", exact=True).fill(
                "Network equipment for office connectivity"
            )
            dialog.get_by_label("Check for new tenders", exact=True).select_option("10800")
            expect(dialog.get_by_label("Notification email", exact=True)).to_have_value(
                "alice@account-fixture.example"
            )
            expect(dialog.get_by_label("Notification email", exact=True)).to_have_attribute(
                "readonly", ""
            )
            accessible(page, "monitoring-dialog")
            responsive(page, "monitoring-dialog")
            dialog.get_by_role("button", name="Save monitoring", exact=True).click()
            expect(
                page.get_by_role("button", name="Network procurement", exact=True)
            ).to_be_visible()
            response = context.request.get(url + "/api/v1/account/campaigns")
            assert response.status == 200
            campaign = response.json()["items"][0]
            assert campaign["criteria"]["source_ids"] == [source_id]
            assert campaign["interval_seconds"] == 10800
            assert campaign["criteria"]["notifications"]["email"] == "alice@account-fixture.example"
            # The real worker schedules and executes against the controlled local fixture.
            assert fixture.runtime.thread is not None and fixture.runtime.thread.is_alive()
            page.wait_for_function(
                "async () => (await (await fetch('/api/v1/account/tenders')).json()).total > 0",
                timeout=30000,
                polling=500,
            )
            account_navigate(page, "Tenders")
            expect(
                page.get_by_role("button", name="Network equipment 0", exact=True)
            ).to_be_visible(timeout=30000)
            accessible(page, "tenders")
            responsive(page, "tenders")
            page.get_by_role("button", name="Network equipment 0", exact=True).click()
            expect(
                page.get_by_role("heading", name="Relevance analysis", exact=True)
            ).to_be_visible()
            expect(page.get_by_text("RELEVANT", exact=True)).to_be_visible()
            assert fixture.calls
            deadline = time.monotonic() + 30
            while len(deliveries) < 2 and time.monotonic() < deadline:
                time.sleep(0.1)
            assert downloads == fixture.document_urls * 2
            assert deliveries == ["alice@account-fixture.example"] * 2
            statuses: list[str] = []
            while time.monotonic() < deadline:
                with fixture.runtime.sessions() as session:
                    statuses = list(session.scalars(select(TelegramNotificationRow.status)))
                if len(statuses) == 2 and set(statuses) == {"delivered"}:
                    break
                time.sleep(0.1)
            assert len(statuses) == 2 and set(statuses) == {"delivered"}
            accessible(page, "tender-details")
            responsive(page, "tender-details")
            tender_id = page.url.split("#/tenders/", 1)[1].split("/")[0]
            account_navigate(page, "Sources")
            page.get_by_role("button", name="Add website", exact=True).click()
            request = page.get_by_role("dialog", name="Add a public tender website")
            request.get_by_label("Website name", exact=True).fill("Requested tenders")
            request.get_by_label("Public website URL", exact=True).fill(
                "https://tenders.example.test/opportunities"
            )
            request.get_by_role("button", name="Submit request", exact=True).click()
            expect(page.get_by_text("Requested tenders", exact=True)).to_be_visible()
            expect(page.get_by_text("UNDER REVIEW", exact=True)).to_be_visible()
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_role("heading", name="Sign in to Quarry")).to_be_visible()
            register_account(page, "bob@account-fixture.example")
            account_navigate(page, "Tenders")
            expect(page.get_by_role("heading", name="No records yet", exact=True)).to_be_visible()
            assert context.request.get(url + "/api/v1/account/tenders").json()["total"] == 0
            assert context.request.get(url + f"/api/v1/account/tenders/{tender_id}").status == 404
            assert context.request.get(url + "/api/v1/web/sources").status == 403
            assert context.request.get(url + "/api/v1/admin/users").status == 403
            page.get_by_role("button", name="Sign out", exact=True).click()
            sign_in(page, "admin@account-fixture.example")
            account_navigate(page, "Administration")
            review = page.locator(".user-campaign").filter(has_text="Requested tenders")
            expect(review).to_contain_text("Verification: not passed")
            review.get_by_role("button", name="Approve", exact=True).click()
            expect(review).to_contain_text("Decision: approved")
            expect(review).to_contain_text("Verification: not passed")
            accessible(page, "administration")
            responsive(page, "administration")
            page.get_by_label("Search users by email", exact=True).fill("bob@")
            table = page.get_by_role("table", name="Users", exact=True)
            expect(table.get_by_role("button", name="bob@account-fixture.example")).to_be_visible()
            expect(table.get_by_role("button", name="alice@account-fixture.example")).to_have_count(
                0
            )
            table.get_by_role("button", name="Make admin", exact=True).click()
            page.get_by_role("button", name="Confirm change", exact=True).click()
            expect(table.get_by_role("button", name="Remove admin", exact=True)).to_be_visible()
            table.get_by_role("button", name="Deactivate", exact=True).click()
            page.get_by_role("button", name="Confirm change", exact=True).click()
            expect(table.get_by_text("disabled", exact=True)).to_be_visible()
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.get_by_role("heading", name="Sign in to Quarry")).to_be_visible()
            page.get_by_label("Email address", exact=True).fill("bob@account-fixture.example")
            page.get_by_label("Password", exact=True).fill(PASSWORD)
            page.get_by_role("button", name="Sign in", exact=True).click()
            expect(page.get_by_role("alert")).to_be_visible()
            expect(page.get_by_role("heading", name="Sign in to Quarry")).to_be_visible()
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_campaign_pause_and_opposite_analysis_browser(
    account_workspace: tuple[str, WebFixture, str, list[str], list[str]],
) -> None:
    url, fixture, source_id, _, _ = account_workspace
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1365, "height": 900})
        page = context.new_page()
        try:
            page.goto(url)
            register_account(page, "campaign-context@account-fixture.example")
            campaigns = []
            for keyword in ("router", "concrete"):
                reply = context.request.post(
                    url + "/api/v1/account/campaigns",
                    data={
                        "criteria": {
                            "name": keyword,
                            "source_ids": [source_id],
                            "include_keywords": [keyword],
                            "notifications": {"enabled": False},
                        },
                    },
                )
                assert reply.status == 201, reply.text()
                campaigns.append(reply.json())
            operator = TestClient(fixture.app)
            collected = operator.post(
                f"/api/v1/web/sources/{source_id}/collect", json={"max_tenders": 2}
            )
            operator.close()
            assert collected.status_code == 202, collected.text
            deadline = time.monotonic() + 30
            recorded = context.request.get(url + "/api/v1/account/tenders").json()
            while recorded["total"] != 4 and time.monotonic() < deadline:
                time.sleep(0.1)
                recorded = context.request.get(url + "/api/v1/account/tenders").json()
            assert recorded["total"] == 4, recorded
            assert any(item["relevance"] == "NOT_RELEVANT" for item in recorded["items"]), recorded
            account_navigate(page, "Tenders")
            page.get_by_label("Relevance filter").select_option("NOT_RELEVANT")
            row = (
                page.get_by_role("row")
                .filter(has_text="Network equipment 0")
                .filter(has_text="concrete")
            )
            expect(row).to_be_visible()
            row.get_by_role("button", name="Network equipment 0", exact=True).click()
            expect(
                page.get_by_role("heading", name="Relevance analysis", exact=True)
            ).to_be_visible()
            expect(page.get_by_text("NOT RELEVANT", exact=True)).to_be_visible()
            expect(page.get_by_text("Campaign: concrete", exact=True)).to_be_visible()
            assert page.url.endswith("/" + campaigns[1]["id"])
            detail_url = page.url
            account_navigate(page, "Monitoring")
            campaign_row = page.get_by_role("row").filter(
                has=page.get_by_role("button", name="concrete", exact=True)
            )
            campaign_row.get_by_role("button", name="Pause", exact=True).click()
            expect(campaign_row.get_by_role("button", name="Resume", exact=True)).to_be_visible()
            paused = context.request.get(url + "/api/v1/account/campaigns").json()["items"]
            assert (
                next(c for c in paused if c["id"] == campaigns[1]["id"])[
                    "current_criteria_version_id"
                ]
                == campaigns[1]["current_criteria_version_id"]
            )
            assert context.request.get(url + "/api/v1/account/tenders").json()["total"] == 4
            campaign_row.get_by_role("button", name="Resume", exact=True).click()
            expect(campaign_row.get_by_role("button", name="Pause", exact=True)).to_be_visible()
            page.goto(detail_url)
            expect(
                page.get_by_role("heading", name="Relevance analysis", exact=True)
            ).to_be_visible()
            expect(page.get_by_text("NOT RELEVANT", exact=True)).to_be_visible()
            assert context.request.get(url + "/api/v1/account/tenders").json()["total"] == 4
        finally:
            context.close()
            browser.close()
