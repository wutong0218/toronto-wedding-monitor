import json
import os
import re
from pathlib import Path
from typing import List, Dict

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE = "https://reservation.frontdesksuite.ca"
START_URL = f"{BASE}/torontoprod/wedding/Home/Index"
TARGET_LOCATION = os.getenv("TARGET_LOCATION", "Old City Hall")
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_slots.json"))


def _click_first(page, patterns):
    """Try a short list of text/role selectors and click the first visible match."""
    for kind, value in patterns:
        try:
            if kind == "text":
                locator = page.get_by_text(value, exact=False)
            elif kind == "button":
                locator = page.get_by_role("button", name=re.compile(value, re.I))
            elif kind == "link":
                locator = page.get_by_role("link", name=re.compile(value, re.I))
            else:
                continue
            if locator.count() and locator.first.is_visible():
                locator.first.click()
                page.wait_for_load_state("networkidle")
                return True
        except Exception:
            pass
    return False


def navigate_to_times(page):
    """
    Start a fresh booking flow so FrontDeskSuite creates the required session.
    Select Old City Hall and proceed to the time-selection page.

    Selectors intentionally use accessible text so small CSS changes are less likely
    to break the monitor. If Toronto materially changes the flow, the workflow will
    fail loudly instead of falsely reporting no slots.
    """
    page.goto(START_URL, wait_until="networkidle", timeout=60_000)

    # Some deployments redirect directly into the wedding landing page. Otherwise,
    # follow the visible Wedding Chambers entry.
    if "wedding" not in page.url.lower():
        _click_first(page, [("link", r"Wedding Chambers"), ("text", "Wedding Chambers")])

    # Terms / continue screen.
    _click_first(page, [
        ("button", r"agree|accept|continue|next"),
        ("link", r"agree|accept|continue|next"),
        ("text", "I Agree"),
    ])

    # Location screen. Prefer exact target text, then a button/link containing it.
    clicked_location = _click_first(page, [
        ("button", re.escape(TARGET_LOCATION)),
        ("link", re.escape(TARGET_LOCATION)),
        ("text", TARGET_LOCATION),
    ])
    if not clicked_location and TARGET_LOCATION.lower() not in page.content().lower():
        raise RuntimeError(f"Could not find location '{TARGET_LOCATION}' in booking flow")

    # If selecting the location didn't navigate immediately, look for a generic continue.
    if "TimeSelection" not in page.url:
        _click_first(page, [
            ("button", r"continue|next|select|reserve"),
            ("link", r"continue|next|select|reserve"),
        ])

    # Reservation duration screen: choose the first enabled duration option if one is
    # presented. For Old City Hall the linked flow currently leads to one duration.
    if "TimeSelection" not in page.url:
        # Radio labels / buttons commonly contain minute/hour text.
        duration = page.locator("input[type=radio]:enabled")
        if duration.count():
            duration.first.check()
        _click_first(page, [
            ("button", r"continue|next|select|reserve"),
            ("link", r"continue|next|select|reserve"),
        ])

    # Last-resort: a visible button that explicitly moves to date/time selection.
    if "TimeSelection" not in page.url:
        _click_first(page, [
            ("button", r"date.*time|time.*date|appointment"),
            ("link", r"date.*time|time.*date|appointment"),
        ])

    page.wait_for_selector("#dateTimesContainer", timeout=30_000)


def extract_slots(page) -> List[Dict[str, str]]:
    """Return every currently available appointment shown by Toronto's page."""
    buttons = page.locator("button:has(.available-time)")
    slots = []

    for i in range(buttons.count()):
        button = buttons.nth(i)
        label = (button.get_attribute("aria-label") or button.inner_text()).strip()
        onclick = button.get_attribute("onclick") or ""

        # Toronto currently embeds the ISO timestamp in selectTime(...).
        iso_match = re.search(r'"(\d{4}-\d{2}-\d{2}T[^\"]+)"', onclick)
        slots.append({
            "label": " ".join(label.split()),
            "datetime": iso_match.group(1) if iso_match else "",
        })

    return slots


def load_previous() -> List[Dict[str, str]]:
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_current(slots: List[Dict[str, str]]) -> None:
    STATE_FILE.write_text(json.dumps(slots, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def slot_key(slot):
    return slot.get("datetime") or slot.get("label")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(locale="en-CA", timezone_id="America/Toronto")
        page = context.new_page()
        try:
            navigate_to_times(page)
            slots = extract_slots(page)
        except Exception:
            # Save useful debugging artifacts in CI.
            Path("artifacts").mkdir(exist_ok=True)
            page.screenshot(path="artifacts/failure.png", full_page=True)
            Path("artifacts/failure.html").write_text(page.content(), encoding="utf-8")
            raise
        finally:
            browser.close()

    previous = load_previous()
    previous_keys = {slot_key(s) for s in previous}
    new_slots = [s for s in slots if slot_key(s) not in previous_keys]

    save_current(slots)

    print(json.dumps({"slots": slots, "new_slots": new_slots}, ensure_ascii=False))

    # GitHub Actions consumes these outputs.
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"count={len(slots)}\n")
            f.write(f"new_count={len(new_slots)}\n")
            f.write("new_slots_json<<EOF\n")
            f.write(json.dumps(new_slots, ensure_ascii=False) + "\n")
            f.write("EOF\n")
            f.write("slots_json<<EOF\n")
            f.write(json.dumps(slots, ensure_ascii=False) + "\n")
            f.write("EOF\n")


if __name__ == "__main__":
    main()
