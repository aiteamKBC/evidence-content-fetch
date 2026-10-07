"""Read-only probe using live-verified Aptem selectors. No database access."""
import json
import re
from contextlib import contextmanager
from urllib.parse import urlsplit

import requests
from django.conf import settings

from .aptem_auth import login_to_aptem


class ProbeError(RuntimeError):
    pass


@contextmanager
def aptem_page():
    from playwright.sync_api import sync_playwright, expect

    expect.set_options(timeout=30000)

    if urlsplit(settings.APTEM_BASE_URL).scheme != "https":
        raise ProbeError("APTEM_BASE_URL must use HTTPS")
    # Reuse existing login; do not call refresh_aptem_cookie, which writes .env.
    with requests.Session() as session:
        result = login_to_aptem(
            settings.APTEM_LOGIN_EMAIL, settings.APTEM_LOGIN_PASSWORD, session=session
        )
        if not result.login_success:
            raise ProbeError("authentication_failed: existing Aptem login failed")
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain,
             "path": c.path or "/", "secure": c.secure,
             **({"expires": c.expires} if c.expires else {})}
            for c in session.cookies
        ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=getattr(settings, 'APTEM_BROWSER_HEADLESS', True))
        try:
            context = browser.new_context()
            try:
                context.set_default_timeout(30000)
                context.set_default_navigation_timeout(60000)
                context.add_cookies(cookies)
                yield context.new_page()
            finally:
                context.close()
        finally:
            browser.close()


def check_session(page):
    if page.get_by_role('heading', name='403 Forbidden', exact=True).count():
        raise ProbeError('access_denied: Aptem Azure Application Gateway returned HTTP 403')
    current = urlsplit(page.url)
    base = urlsplit(settings.APTEM_BASE_URL)
    if (current.netloc != base.netloc or not current.path.startswith("/pwa/")
            or re.search(r"/(login|logon|sign-in)(/|$)", current.path, re.I)
            or page.locator('input[type="password"]').count()):
        raise ProbeError("session_expired: authenticated Aptem page unavailable")


def run_probe(write=print, *, on_review=None):
    from playwright.sync_api import expect

    output = settings.BASE_DIR.parent / "debug"
    output.mkdir(exist_ok=True)
    events = []

    def record(page, step, selector, screenshot=None, **details):
        check_session(page)
        event = {"step": step, "url": page.url, "title": page.title(),
                 "element_located": selector, "result": "ok",
                 "iframe_count": page.locator("iframe").count(), **details}
        events.append(event)
        write(json.dumps(event, ensure_ascii=True))
        if screenshot:
            page.screenshot(path=str(output / screenshot), full_page=True)

    try:
        with aptem_page() as page:
            try:
                page.goto(settings.APTEM_BASE_URL, wait_until="domcontentloaded")
                page.get_by_role("link", name="Learners", exact=True).wait_for()
                record(page, "Open Aptem", "role=link name=Learners")
                page.get_by_role("link", name="Learners", exact=True).click()
                page.wait_for_url("**/pwa/learners")
                page.get_by_role("searchbox").wait_for()
                record(page, "Open Learners", "role=link name=Learners", "01-learners.png")

                page.locator("#learnerOwnershipQuickFilter").click()
                page.get_by_role("option", name="All Learners", exact=True).click()
                page.get_by_role("searchbox").fill("Betty Heaton")
                page.get_by_role("searchbox").press("Enter")
                row = page.get_by_role("row").filter(
                    has=page.get_by_text("Betty Heaton", exact=True)
                )
                expect(row).to_have_count(1)
                header = page.get_by_role("columnheader", name="Programme Status", exact=True)
                column = header.get_attribute("aria-colindex")
                if not column or not column.isdigit():
                    raise ProbeError("Programme Status column could not be resolved")
                status = row.locator(f'[role="gridcell"][aria-colindex="{column}"]').inner_text().strip()
                if status != "Active":
                    raise ProbeError(f"skipped_not_active: Programme Status={status}")
                record(page, "Search and verify Betty Heaton", "role=searchbox; Programme Status header -> gridcell", "02-search-result.png", status=status)
                row.get_by_role("option", name="Dashboard", exact=True).click()
                page.wait_for_url(re.compile(r"/pwa/learners/\d+/dashboard$"))
                learner_id = re.search(r"/learners/(\d+)/", page.url).group(1)
                # Development-only name search, followed by the live-mapped unique ID.
                if learner_id != "15997":
                    raise ProbeError("Learner ID differs from mapped Betty Heaton ID")
                expect(page.get_by_text("Active", exact=True)).to_be_visible()
                record(page, "Verify learner Dashboard", "row -> role=option name=Dashboard; exact text=Active", "03-learner-dashboard.png", learner_id=learner_id)

                page.locator('[auto-id="admin-menu-horizontal-reviews"]').click()
                page.wait_for_url(f"**/learners/{learner_id}/reviews/upcoming")
                page.get_by_role("button", name="Finished", exact=True).wait_for()
                record(page, "Open Reviews", "role=link name=Reviews", "04-reviews.png")
                page.get_by_role("button", name="Finished", exact=True).click()
                page.wait_for_url(f"**/learners/{learner_id}/reviews/finished")
                views = page.get_by_role("button", name="View", exact=True)
                expect(views.first).to_be_visible()
                # first intentionally means the requested first displayed finished review.
                review_row = page.get_by_role("row").filter(has=page.get_by_role("button", name="View", exact=True)).first
                expect(review_row.get_by_role("gridcell", name="Completed", exact=True)).to_be_visible()
                review_metadata = {}
                for header in page.get_by_role('columnheader').all():
                    col = header.get_attribute('aria-colindex')
                    if col and col.isdigit():
                        cell = review_row.locator(f'[role="gridcell"][aria-colindex="{col}"]')
                        if cell.count() == 1:
                            review_metadata[' '.join(header.inner_text().split())] = cell.inner_text().strip()
                record(page, "Open Finished", "role=button name=Finished; Completed row -> role=button name=View", "05-finished-reviews.png", visible_reviews=views.count(), first_review_row=review_row.inner_text(),
                       next_page_enabled=page.get_by_role("button", name="Go to the next page", exact=True).is_enabled())
                review_row.get_by_role("button", name="View", exact=True).click()
                page.wait_for_url(re.compile(rf"/learners/{learner_id}/reviews/review-content/\d+$"))
                toggles = page.locator('div[role="button"][aria-expanded]')
                expect(toggles.first).to_be_visible()
                review_id = page.url.rsplit("/", 1)[1]
                sections = toggles.all_text_contents()
                record(page, "View first finished review", "Completed row -> role=button name=View", "06-review-detail.png", review_id=review_id, sections=sections)
                for toggle in toggles.all():
                    if toggle.get_attribute("aria-expanded") == "false":
                        toggle.click()
                        expect(toggle).to_have_attribute("aria-expanded", "true")
                record(page, "Inspect expanded sections", "div[role=button][aria-expanded]", "07-expanded-sections.png", sections=sections)
                if on_review is not None:
                    on_review(page, learner_id, review_id, review_metadata)
            except Exception:
                # Do not capture identity-provider/login screens.
                check_session(page)
                page.screenshot(path=str(output / "failure.png"), full_page=True)
                raise
    except Exception as exc:
        events.append({"step": "Probe", "result": "failed", "error_type": type(exc).__name__})
        raise
    finally:
        (output / "navigation.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
    write("[OK] Read-only probe complete; no database writes or Aptem edit actions.")

