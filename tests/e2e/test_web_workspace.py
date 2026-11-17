"""Browser → built React → HTTP services → isolated PostgreSQL, owned source/mock model."""

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from playwright.sync_api import BrowserContext, Page, expect, sync_playwright
from sqlalchemy import Engine

from tender_system.core.configuration import load_configuration
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.web import WebFixture, web_fixture

pytestmark = [pytest.mark.database, pytest.mark.browser]
ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts/tests/frontend-replacement/browser"


def free_port() -> int:
    for _ in range(100):
        port = 10000 + secrets.randbelow(20000)
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    raise RuntimeError("No local test port available")


@pytest.fixture
def browser_context(request: pytest.FixtureRequest) -> Iterator[BrowserContext]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        try:
            yield context
        finally:
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            context.tracing.stop(path=str(ARTIFACTS / f"{request.node.name}.zip"))
            context.close()
            browser.close()


@pytest.fixture
def workspace(
    migrated_engine: Engine, request: pytest.FixtureRequest
) -> Iterator[tuple[str, WebFixture]]:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    config = load_configuration(environ={}) if getattr(request, "param", None) == "public" else None
    with web_fixture(migrated_engine, config) as fixture:
        api_port, web_port = free_port(), free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                fixture.app, host="127.0.0.1", port=api_port, log_level="error", access_log=False
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        with (ARTIFACTS / f"vite-{web_port}.log").open("w") as log:
            process = subprocess.Popen(
                [
                    "node",
                    "node_modules/vite/bin/vite.js",
                    "preview",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                    "--strictPort",
                ],
                cwd=ROOT / "frontend",
                env={**os.environ, "QUARRY_API_URL": f"http://127.0.0.1:{api_port}"},
                stdout=log,
                stderr=log,
            )
            url = f"http://127.0.0.1:{web_port}"
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
                    pytest.fail("Test servers unavailable")
                yield url, fixture
            finally:
                process.terminate()
                process.wait(timeout=10)
                server.should_exit = True
                thread.join(timeout=10)


def navigate(page: Page, name: str) -> None:
    if page.get_by_role("button", name="Open menu", exact=True).is_visible():
        page.get_by_role("button", name="Open menu", exact=True).click()
    page.get_by_role("navigation", name="Main navigation").get_by_role(
        "link", name=name, exact=True
    ).click()
    expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()


def login(page: Page, url: str) -> None:
    page.goto(url)
    expect(page.get_by_role("heading", name="Tenders", exact=True)).to_be_visible()


def source_ready(page: Page, fixture: WebFixture) -> None:
    navigate(page, "Sources")
    page.get_by_role("button", name="Add source", exact=True).click()
    page.get_by_label("Website URL", exact=True).fill(fixture.source_url)
    page.get_by_role("button", name="Add website", exact=True).click()
    expect(page.get_by_label("Recipe JSON", exact=True)).to_be_visible()
    page.get_by_role("button", name="Disable source", exact=True).click()
    expect(page.get_by_role("button", name="Enable source", exact=True)).to_be_visible()
    page.get_by_role("button", name="Enable source", exact=True).click()
    page.get_by_label("Recipe JSON", exact=True).fill(fixture.recipe)
    page.get_by_role("button", name="Save and validate recipe", exact=True).click()
    expect(
        page.get_by_role("region", name="Operation progress").locator(".actions > .state")
    ).to_have_text("activated", timeout=30000)
    expect(page.get_by_role("button", name="Scrape tenders", exact=True)).to_be_enabled()


def collect(page: Page) -> None:
    page.get_by_label("Tenders to collect", exact=True).fill("2")
    page.get_by_role("button", name="Scrape tenders", exact=True).click()
    progress = page.get_by_role("region", name="Operation progress")
    expect(progress.get_by_role("heading", name="Tender collection", exact=True)).to_be_visible()
    expect(progress.locator(".actions > .state")).to_have_text("completed", timeout=30000)


def run_matching(page: Page, ai: bool = False, force: bool = False) -> None:
    page.get_by_role("button", name="Run matching", exact=True).click()
    dialog = page.get_by_role("dialog", name="Run matching")
    if ai:
        dialog.get_by_label("Matcher", exact=True).select_option("semantic")
    if force:
        dialog.get_by_label("Create a new evaluation for these versions", exact=True).check()
    dialog.get_by_role("button", name="Review AI run" if ai else "Review rules run").click()
    expect(
        dialog.get_by_role("button", name="Confirm AI run" if ai else "Confirm rules run")
    ).to_be_visible()
    dialog.get_by_role("button", name="Confirm AI run" if ai else "Confirm rules run").click()
    expect(
        dialog.get_by_role("region", name="Operation progress").get_by_text("completed", exact=True)
    ).to_be_visible(timeout=30000)
    dialog.get_by_role("button", name="Close dialog").click()


def test_complete_operator_workflow(
    workspace: tuple[str, WebFixture], browser_context: BrowserContext
) -> None:
    from fastapi.testclient import TestClient

    url, fixture = workspace
    page = browser_context.new_page()
    page.set_viewport_size({"width": 1280, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    login(page, url)
    source_ready(page, fixture)
    page.get_by_text("Source settings", exact=True).click()
    page.get_by_label("Source timezone", exact=True).fill("Asia/Tashkent")
    page.get_by_role("button", name="Save source settings").click()
    page.get_by_text("Date and number parsing", exact=True).click()
    page.get_by_label("Transport attempts per request", exact=True).fill("2")
    page.get_by_label("Date-only deadline", exact=True).select_option("end_of_day")
    page.get_by_role("button", name="Save source settings").click()
    expect(page.get_by_role("status")).to_contain_text("Source settings saved")
    collect(page)
    page.screenshot(path=str(ARTIFACTS / "source-ready.png"), full_page=True)
    # Failed replacement never destroys the active recipe.
    page.get_by_text("Collection recipe", exact=True).click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Load saved recipe", exact=True).click()
    expect(page.get_by_label("Recipe JSON", exact=True)).not_to_be_empty()
    broken = json.loads(fixture.recipe)
    broken["steps"][1]["parameters"]["selector"] = "absent"
    page.get_by_label("Recipe JSON", exact=True).fill(json.dumps(broken))
    page.get_by_role("button", name="Save and validate recipe").click()
    expect(page.get_by_role("region", name="Operation progress")).to_contain_text(
        "failed", timeout=30000
    )
    expect(page.get_by_role("button", name="Scrape tenders")).to_be_enabled()
    fixture.changed_title = True
    collect(page)
    navigate(page, "Tenders")
    expect(page.get_by_role("table", name="Tenders")).to_contain_text("Network equipment")
    page.get_by_role("button", name="More filters", exact=True).click()
    page.get_by_label("Buyer filter", exact=True).fill("Public procurement")
    page.get_by_label("Currency filter", exact=True).fill("USD")
    expect(page.get_by_role("table", name="Tenders")).to_contain_text("Network equipment")
    page.get_by_label("Location filter", exact=True).fill("unavailable-region")
    expect(page.get_by_text("No records yet", exact=True)).to_be_visible()
    page.get_by_label("Location filter", exact=True).fill("")
    page.get_by_label("Search tenders", exact=True).fill("equipment 0 changed")
    expect(page.get_by_role("table", name="Tenders").get_by_role("checkbox")).to_have_count(2)
    page.get_by_label("Procurement status filter").select_option("closed")
    expect(page.get_by_text("No records yet", exact=True)).to_be_visible()
    page.get_by_label("Procurement status filter").select_option("open")
    page.get_by_role("button", name="Network equipment 0 changed", exact=True).click()
    expect(page.get_by_role("table", name="Product items")).to_contain_text("Network router")
    page.get_by_text("History and provenance", exact=True).click()
    expect(page.get_by_text("First recorded", exact=False)).to_be_visible()
    expect(page.get_by_text("Updated", exact=False)).to_be_visible()
    original = page.get_by_role("link", name="Open original source")
    expect(original).to_have_attribute("href", fixture.source_url + "/detail/0")
    page.get_by_role("button", name="Back to Tenders").click()
    page.get_by_role("button", name="Add tender", exact=True).click()
    page.get_by_label("Tender title", exact=True).fill(
        "Manual network router — Сетевое оборудование — тармоқ"
    )
    page.get_by_label("Value", exact=True).fill("100")
    page.get_by_label("Currency", exact=True).fill("USD")
    page.get_by_role("button", name="Save manual tender").click()
    expect(page.get_by_role("heading", name="Manual network router", exact=False)).to_be_visible()
    expect(page.get_by_text("Manual entry", exact=True)).to_be_visible()
    navigate(page, "Campaigns")
    page.get_by_role("button", name="Create campaign").click()
    page.get_by_label("Campaign name", exact=True).fill("Network buying")
    page.get_by_label("Included keywords", exact=True).fill("network")
    page.get_by_label("Campaign AI limit (USD)", exact=True).fill("10")
    page.get_by_role("button", name="Save campaign").click()
    expect(page.get_by_role("table", name="Campaigns")).to_contain_text("Network buying")
    page.get_by_role("button", name="Edit campaign").click()
    page.get_by_label("Campaign name", exact=True).fill("Network buying revised")
    page.get_by_role("button", name="Save campaign").click()
    expect(page.get_by_role("table", name="Campaigns")).to_contain_text("version 2")
    page.get_by_role("button", name="Pause", exact=True).click()
    expect(page.get_by_role("button", name="Activate", exact=True)).to_be_visible()
    page.get_by_role("button", name="Activate", exact=True).click()
    navigate(page, "Matching")
    page.get_by_label("Campaign", exact=True).select_option(label="Network buying revised · v2")
    page.get_by_label("Select Network equipment 0 changed", exact=True).check()
    run_matching(page)
    expect(page.get_by_role("table", name="Matching selection")).to_contain_text("Relevant")
    expect(page.get_by_role("table", name="Relevant tenders", exact=True)).to_contain_text(
        "Network equipment 0 changed"
    )
    assert fixture.calls == []
    page.get_by_role("button", name="Run matching", exact=True).click()
    dialog = page.get_by_role("dialog", name="Run matching")
    dialog.get_by_label("Matcher", exact=True).select_option("semantic")
    dialog.get_by_role("button", name="Review AI run").click()
    expect(dialog.get_by_text("qwen/qwen3.8-27b", exact=True)).to_be_visible()
    assert fixture.calls == []
    dialog.get_by_role("button", name="Confirm AI run").click()
    expect(
        dialog.get_by_role("region", name="Operation progress").get_by_text("completed", exact=True)
    ).to_be_visible(timeout=30000)
    dialog.get_by_role("button", name="Close dialog").click()
    assert len(fixture.calls) == 1
    page.get_by_role("table", name="Matching selection").get_by_role(
        "button", name="Inspect result"
    ).first.click()
    page.get_by_text("AI opinion and source evidence", exact=True).click()
    expect(page.get_by_text("verified", exact=True)).to_be_visible()
    expect(
        page.get_by_role("dialog")
        .get_by_text("Network router is explicitly present", exact=True)
        .first
    ).to_be_visible()
    page.get_by_role("button", name="Close dialog").click()
    fixture.model_behavior = "invalid_evidence"
    run_matching(page, ai=True, force=True)
    expect(page.get_by_role("table", name="Matching selection")).to_contain_text("Needs review")
    expect(page.get_by_role("table", name="Relevant tenders", exact=True)).not_to_be_visible()
    expect(page.get_by_text("No current relevant tenders for this campaign.")).to_be_visible()
    page.get_by_text("Historical evaluations", exact=True).click()
    expect(
        page.get_by_role("table", name="Historical matching results").get_by_role(
            "button", name="Inspect result"
        )
    ).to_have_count(3)
    expect(page.get_by_role("table", name="Historical matching results")).to_contain_text(
        "Unverified evidence"
    )
    page.screenshot(path=str(ARTIFACTS / "matching.png"), full_page=True)
    fixture.break_source = True
    navigate(page, "Sources")
    page.get_by_role("button", name="Open source").click()
    page.get_by_role("button", name="Scrape tenders").click()
    expect(page.get_by_role("region", name="Operation progress")).to_contain_text(
        "failed", timeout=30000
    )
    navigate(page, "Activity")
    page.get_by_label("Operation status").select_option("failed")
    expect(page.get_by_role("table", name="Activity")).to_contain_text("Tender collection")
    page.get_by_role("button", name="Inspect operation").first.click()
    expect(page.get_by_role("region", name="Operation progress")).to_contain_text("failed")
    assert errors == []
    with TestClient(fixture.app) as client:
        assert client.get("/api/v1/web/tenders").json()["total"] == 3


def test_automatic_ai_budget_stop_resume_and_authorization(
    workspace: tuple[str, WebFixture], browser_context: BrowserContext
) -> None:
    url, fixture = workspace
    page = browser_context.new_page()
    login(page, url)
    navigate(page, "Campaigns")
    page.get_by_role("button", name="Create campaign").click()
    expect(page.get_by_label("Automatically match new and changed tenders")).not_to_be_checked()
    expect(
        page.get_by_label("I authorize automatic AI requests for this campaign")
    ).not_to_be_checked()
    page.get_by_label("Campaign name", exact=True).fill("Automatic network")
    page.get_by_label("Included keywords", exact=True).fill("network")
    page.get_by_label("Automatically match new and changed tenders").check()
    page.get_by_label("Automatic matcher", exact=True).select_option("semantic")
    page.get_by_label("I authorize automatic AI requests for this campaign").check()
    page.get_by_label("Campaign AI limit (USD)", exact=True).fill("1")
    page.get_by_role("button", name="Save campaign").click()
    expect(page.get_by_role("table", name="Campaigns")).to_contain_text("AI authorized")
    page.get_by_role("button", name="AI spending", exact=True).click()
    dialog = page.get_by_role("dialog", name="AI spending controls")
    dialog.get_by_label("Global AI limit (USD)", exact=True).fill("0")
    dialog.get_by_role("button", name="Save spending controls").click()
    expect(dialog.get_by_role("status")).to_contain_text("saved")
    dialog.get_by_role("button", name="Close", exact=True).click()
    source_ready(page, fixture)
    collect(page)
    navigate(page, "Activity")
    row = page.get_by_role("row").filter(has_text="Automatic matching")
    row.get_by_role("button", name="Inspect operation").first.click()
    progress = page.get_by_role("region", name="Operation progress")
    expect(progress.get_by_text("failed", exact=True)).to_be_visible(timeout=30000)
    expect(progress).to_contain_text("Global or campaign AI budget exhausted")
    expect(progress).to_contain_text("Evaluations pending")
    assert fixture.calls == []
    page.screenshot(path=str(ARTIFACTS / "budget-stopped.png"), full_page=True)
    navigate(page, "Campaigns")
    page.get_by_role("button", name="AI spending", exact=True).click()
    dialog = page.get_by_role("dialog", name="AI spending controls")
    dialog.get_by_label("Global AI limit (USD)", exact=True).fill("1")
    dialog.get_by_role("button", name="Save spending controls").click()
    expect(dialog.get_by_role("status")).to_contain_text("saved")
    dialog.get_by_role("button", name="Close", exact=True).click()
    navigate(page, "Activity")
    page.get_by_role("row").filter(has_text="Automatic matching").get_by_role(
        "button", name="Inspect operation"
    ).first.click()
    page.get_by_role("button", name="Resume operation", exact=True).click()
    expect(
        page.get_by_role("region", name="Operation progress").get_by_text("completed", exact=True)
    ).to_be_visible(timeout=30000)
    assert len(fixture.calls) == 2
    navigate(page, "Matching")
    page.get_by_label("Campaign", exact=True).select_option(label="Automatic network · v1")
    expect(page.get_by_role("table", name="Matching selection")).to_contain_text("Relevant")
    navigate(page, "Campaigns")
    page.get_by_role("button", name="Edit campaign").click()
    page.get_by_label("I authorize automatic AI requests for this campaign").uncheck()
    page.get_by_role("button", name="Save campaign").click()
    expect(page.get_by_role("table", name="Campaigns")).to_contain_text("AI not authorized")
    navigate(page, "Matching")
    page.get_by_label("Campaign", exact=True).select_option(label="Automatic network · v2")
    expect(page.get_by_text("Automatic AI not authorized", exact=True)).to_be_visible()
    assert len(fixture.calls) == 2


def test_rules_selection_pagination_reuse_and_historical_results(
    workspace: tuple[str, WebFixture], browser_context: BrowserContext
) -> None:
    from fastapi.testclient import TestClient

    url, fixture = workspace
    with TestClient(fixture.app) as client:
        for i in range(30):
            assert (
                client.post(
                    "/api/v1/web/tenders",
                    json={"request_id": str(uuid4()), "title": f"Network router {i}"},
                ).status_code
                == 201
            )
        campaign = client.post(
            "/api/v1/web/campaigns",
            json={"criteria": {"name": "Routers", "include_keywords": ["router"]}},
        ).json()
    page = browser_context.new_page()
    login(page, url)
    navigate(page, "Matching")
    page.get_by_label("Campaign", exact=True).select_option(campaign["id"])
    page.get_by_label("Select all tenders on page").check()
    expect(page.get_by_text("25 selected", exact=True)).to_be_visible()
    page.get_by_role("button", name="Next", exact=True).click()
    page.get_by_label("Select all tenders on page").check()
    expect(page.get_by_text("30 selected", exact=True)).to_be_visible()
    run_matching(page)
    run_matching(page)
    page.get_by_text("Historical evaluations", exact=True).click()
    history = page.get_by_role("table", name="Historical matching results")
    expect(history.get_by_role("button", name="Inspect result")).to_have_count(25)
    run_matching(page, force=True)
    with TestClient(fixture.app) as client:
        assert (
            client.get("/api/v1/web/results", params={"campaign_id": campaign["id"]}).json()[
                "total"
            ]
            == 60
        )
    assert fixture.calls == []


@pytest.mark.parametrize("width", [320, 375, 414, 768, 1280, 1920])
def test_responsive_accessibility_keyboard_and_multilingual(
    workspace: tuple[str, WebFixture], browser_context: BrowserContext, width: int
) -> None:
    from fastapi.testclient import TestClient

    url, fixture = workspace
    title = "Табиий пардозбоп тошларни қайта ишлаш объектлари қурилиши — " * 5
    with TestClient(fixture.app) as client:
        assert (
            client.post(
                "/api/v1/web/tenders", json={"request_id": str(uuid4()), "title": title}
            ).status_code
            == 201
        )
    page = browser_context.new_page()
    page.set_viewport_size({"width": width, "height": 960})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    login(page, url)
    for section in ["Tenders", "Sources", "Campaigns", "Matching", "Activity"]:
        navigate(page, section)
        page.wait_for_load_state("networkidle")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), section
        page.add_script_tag(path=str(ROOT / "frontend/node_modules/axe-core/axe.min.js"))
        violations = page.evaluate(
            "async()=> (await axe.run(document,{runOnly:{type:'tag',"
            "values:['wcag2a','wcag2aa','wcag21aa']}})).violations"
            ".map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)}))"
        )
        assert violations == [], (section, width, violations)
        page.screenshot(path=str(ARTIFACTS / f"{section.lower()}-{width}.png"), full_page=True)
    navigate(page, "Tenders")
    page.get_by_role("button", name=title, exact=True).click()
    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
    expect(page.get_by_text("Product information was not provided by the source.")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.get_by_role("button", name="Back to Tenders").click()
    page.get_by_role("button", name="Add tender", exact=True).click()
    dialog = page.get_by_role("dialog", name="Add tender", exact=True)
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(page.get_by_role("button", name="Add tender", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_role("dialog", name="Add tender", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    assert errors == []


def test_failure_loading_recovery_and_readonly_idle_requests(
    workspace: tuple[str, WebFixture], browser_context: BrowserContext
) -> None:
    url, _ = workspace
    page = browser_context.new_page()
    page.route(
        "**/api/v1/web/tenders?*",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"error":{"message":"Collection service unavailable"}}',
        ),
    )
    login(page, url)
    expect(page.get_by_role("alert")).to_contain_text("Collection service unavailable")
    page.unroute("**/api/v1/web/tenders?*")
    page.get_by_role("button", name="Try again").click()
    expect(page.get_by_text("No records yet", exact=True)).to_be_visible()
    requests: list[str] = []
    page.on(
        "request", lambda request: requests.append(request.url) if "/api/" in request.url else None
    )
    page.wait_for_timeout(3500)
    assert requests == [], requests
    navigate(page, "Sources")
    page.get_by_role("button", name="Add source", exact=True).click()
    page.get_by_label("Website URL", exact=True).fill("https://example.com/?api_key=unsafe")
    page.get_by_role("button", name="Add website", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("without secret-bearing query strings")


def test_production_static_serving_routes_and_persistence(
    migrated_engine: Engine, browser_context: BrowserContext
) -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    environment = {
        **os.environ,
        "DATABASE_URL": migrated_engine.url.render_as_string(hide_password=False),
        "QUARRY_API_PORT": str(port),
        "QUARRY_API_HOST": "127.0.0.1",
        "QUARRY_CONFIG_FILE": "",
        "QUARRY_REGISTER_CATALOG": "false",
        "QUARRY_MANUAL_ONLY": "true",
        "QUARRY_OPERATOR_WORKER_ENABLED": "false",
        "QUARRY_AI_SEMANTIC_ENABLED": "false",
        **{
            "QUARRY_SOURCE__" + key.upper(): json.dumps(value)
            for key, value in fixture_source_settings().model_dump(mode="json").items()
        },
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page = browser_context.new_page()
    for iteration in range(2):
        with (ARTIFACTS / "production-startup.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "tender_system.api.server"],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        if httpx.get(url, timeout=1).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail("Production server unavailable")
                login(page, url)
                if iteration == 0:
                    page.get_by_role("button", name="Add tender", exact=True).click()
                    page.get_by_label("Tender title", exact=True).fill("Persistent procurement")
                    page.get_by_role("button", name="Save manual tender").click()
                    expect(
                        page.get_by_role("heading", name="Persistent procurement", exact=True)
                    ).to_be_visible()
                    page.reload()
                    expect(
                        page.get_by_role("heading", name="Persistent procurement", exact=True)
                    ).to_be_visible()
                    navigate(page, "Campaigns")
                    page.get_by_role("button", name="Create campaign").click()
                    page.get_by_label("Campaign name", exact=True).fill("Persistent campaign")
                    page.get_by_role("button", name="Save campaign").click()
                    expect(page.get_by_role("table", name="Campaigns")).to_contain_text(
                        "Persistent campaign"
                    )
                else:
                    expect(page.get_by_role("table", name="Tenders")).to_contain_text(
                        "Persistent procurement"
                    )
                    navigate(page, "Campaigns")
                    expect(page.get_by_role("table", name="Campaigns")).to_contain_text(
                        "Persistent campaign"
                    )
            finally:
                process.terminate()
                process.wait(timeout=10)


def test_refinement_fixture_product_information_has_no_inferred_quantity(
    workspace: tuple[str, WebFixture], browser_context: BrowserContext
) -> None:
    from fastapi.testclient import TestClient

    url, fixture = workspace
    captured = json.loads((ROOT / "tests/fixtures/matcher/tender_8915917.json").read_text())
    tender = captured["tender"]
    with TestClient(fixture.app) as client:
        response = client.post(
            "/api/v1/web/tenders",
            json={
                "request_id": str(uuid4()),
                "title": tender["title"],
                "description": tender.get("description"),
                "buyer": tender.get("buyer"),
                "value": str(tender["value"]),
                "currency": tender.get("currency"),
                "deadline": tender.get("deadline"),
                "product_items": tender["product_items"],
            },
        )
        assert response.status_code == 201, response.text
    page = browser_context.new_page()
    page.set_viewport_size({"width": 1280, "height": 960})
    page.goto(url + "/#/tenders/" + response.json()["id"])
    expect(page.get_by_role("heading", name=tender["title"], exact=True)).to_be_visible()
    products = page.get_by_role("table", name="Product items")
    expect(products).to_contain_text("Category:")
    expect(products.get_by_role("cell", name="Not provided", exact=True)).to_be_visible()
    assert "uuid" not in products.inner_text().lower()
    import re

    assert re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", products.inner_text()) is None
    page.screenshot(
        path=str(ARTIFACTS / "refined-tender-1280.png"), full_page=True, animations="disabled"
    )
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.screenshot(
        path=str(ARTIFACTS / "refined-tender-1920.png"), full_page=True, animations="disabled"
    )


def test_automatic_rules_matching_uses_no_provider(
    workspace: tuple[str, WebFixture], browser_context: BrowserContext
) -> None:
    url, fixture = workspace
    page = browser_context.new_page()
    login(page, url)
    navigate(page, "Campaigns")
    page.get_by_role("button", name="Create campaign").click()
    page.get_by_label("Campaign name", exact=True).fill("Automatic rules")
    page.get_by_label("Included keywords", exact=True).fill("network")
    page.get_by_label("Automatically match new and changed tenders").check()
    expect(page.get_by_label("Automatic matcher", exact=True)).to_have_value("rules")
    page.get_by_role("button", name="Save campaign").click()
    expect(page.get_by_role("table", name="Campaigns")).to_contain_text("Automatic rules")
    source_ready(page, fixture)
    # Navigate before collection finishes: completion must refresh other sections.
    page.get_by_label("Tenders to collect", exact=True).fill("2")
    page.get_by_role("button", name="Scrape tenders", exact=True).click()
    navigate(page, "Matching")
    page.get_by_label("Campaign", exact=True).select_option(label="Automatic rules · v1")
    expect(
        page.get_by_role("table", name="Matching selection").get_by_text("Relevant", exact=True)
    ).to_have_count(2, timeout=30000)
    assert fixture.calls == []
