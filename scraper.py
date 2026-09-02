import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


START_URL = (
    "https://reservation.frontdesksuite.ca/torontoprod/wedding/Home/Index"
    "?Culture=en"
    "&PageId=c7b8201e-8573-4225-ba98-76bd4cf2d376"
    "&ShouldStartReserveTimeFlow=False"
    "&ButtonId=00000000-0000-0000-0000-000000000000"
)

BOOKING_URL = (
    "https://reservation.frontdesksuite.ca/torontoprod/wedding/"
    "ReserveTime/TimeSelection"
    "?pageId=95ac460d-d4ae-4d2b-bf96-de0f412f6975"
    "&buttonId=f5091379-600c-442e-9d9d-646b1d6a7e71"
    "&culture=en"
)

STATE_FILE = Path("seen_slots.json")
ARTIFACT_DIR = Path("artifacts")


def log(message: str) -> None:
    print(message, flush=True)


def click_link(page, pattern: str, description: str) -> None:
    """
    Click a link using a case-insensitive regex against its accessible name.
    This is more tolerant of whitespace/punctuation changes than exact text.
    """
    log(f"Clicking: {description}")

    locator = page.get_by_role(
        "link",
        name=re.compile(pattern, re.IGNORECASE),
    ).first

    locator.wait_for(state="visible", timeout=30_000)
    locator.click()

    # We deliberately avoid networkidle because analytics on the City site
    # can keep network activity alive.
    page.wait_for_load_state("domcontentloaded")


def navigate_to_times(page) -> None:
    """
    Reproduce the real Toronto Old City Hall booking flow so the reservation
    session/cookies are created correctly.
    """
    log("Opening Wedding Chambers booking page...")
    page.goto(
        START_URL,
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    log(f"Current URL: {page.url}")

    click_link(
        page,
        r"Reserve\s+a\s+Civil\s+Wedding\s+Ceremony\s+at\s+Old\s+City\s+Hall",
        "Reserve a Civil Wedding Ceremony at Old City Hall",
    )
    log(f"Current URL: {page.url}")

    click_link(
        page,
        r"By\s+proceeding\s+with\s+reserving\s+the\s+wedding\s+chamber\s+I\s+agree",
        "accept the wedding chamber terms and conditions",
    )
    log(f"Current URL: {page.url}")

    click_link(
        page,
        r"Reserve\s+Old\s+City\s+Hall",
        "Reserve Old City Hall",
    )
    log(f"Current URL: {page.url}")

    click_link(
        page,
        r"Reserve\s+30\s+Minutes",
        "Reserve 30 Minutes",
    )

    log("Waiting for appointment time page...")

    page.wait_for_url(
        re.compile(r".*/ReserveTime/TimeSelection.*", re.IGNORECASE),
        timeout=30_000,
    )

    page.locator("#dateTimesContainer").wait_for(
        state="attached",
        timeout=30_000,
    )

    log(f"Reached appointment page: {page.url}")


def wait_for_availability_content(page) -> None:
    """
    Wait until the Toronto appointment data has actually rendered, then wait
    briefly for the availability structure to stop changing.

    In the hydrated #dateTimesContainer, sold-out dates are rendered as:
        <div class="text-danger">No more available time slots</div>

    Available appointments contain:
        <span class="available-time">...</span>

    We deliberately key off those real hydrated elements rather than the
    pre-hydration markup elsewhere on the page.
    """
    log("Waiting for appointment availability content...")

    # Wait for real appointment data inside the hydrated container.
    page.wait_for_function(
        """
        () => {
            const container = document.querySelector('#dateTimesContainer');
            if (!container) return false;

            if (container.querySelector('.available-time')) {
                return true;
            }

            return Array.from(
                container.querySelectorAll('.text-danger')
            ).some((element) =>
                /no more available time slots/i.test(
                    element.textContent || ''
                )
            );
        }
        """,
        polling=250,
        timeout=30_000,
    )

    previous_state = None
    stable_checks = 0

    # The page may hydrate date sections incrementally. Wait until the pieces
    # relevant to availability have held the same counts for ~2 seconds.
    for _ in range(30):
        date_count = page.locator(
            "#dateTimesContainer section.accordion__section"
        ).count()

        slot_count = page.locator(
            "#dateTimesContainer .available-time"
        ).count()

        warning_count = page.locator(
            "#dateTimesContainer .text-danger"
        ).filter(
            has_text=re.compile(
                r"No more available time slots",
                re.IGNORECASE,
            )
        ).count()

        state = (date_count, slot_count, warning_count)

        if (slot_count > 0 or warning_count > 0) and state == previous_state:
            stable_checks += 1
        else:
            stable_checks = 0

        if stable_checks >= 4:
            log(
                "Appointment availability content is ready and stable "
                f"(dates={date_count}, slots={slot_count}, "
                f"warnings={warning_count})."
            )
            return

        previous_state = state
        page.wait_for_timeout(500)

    raise RuntimeError(
        "Availability content appeared but did not reach a stable structural "
        "state within 15 seconds."
    )


def extract_slots(page) -> list[dict]:
    """
    Extract actual clickable appointment slots.

    The Toronto page marks bookable times with:
        <span class="available-time">...</span>

    We intentionally do NOT infer availability from the absence/presence of
    "No more available time slots", because a page can contain both sold-out
    dates and available dates at the same time.
    """
    wait_for_availability_content(page)

    slot_buttons = page.locator("button:has(.available-time)")
    count = slot_buttons.count()

    log(f"Found {count} available slot button(s).")

    slots = []

    for i in range(count):
        button = slot_buttons.nth(i)

        label = (button.get_attribute("aria-label") or "").strip()
        onclick = button.get_attribute("onclick") or ""

        if not label:
            # Fallback in case Toronto removes aria-label someday.
            time_text = button.locator(".available-time").inner_text().strip()

            described_by = button.get_attribute("aria-describedby") or ""
            date_text = ""

            if described_by:
                date_header = page.locator(f"#{described_by}")
                if date_header.count():
                    date_text = date_header.first.inner_text().strip()

            label = f"{time_text} {date_text}".strip()

        # Pull the ISO timestamp out of selectTime(...).
        # Example:
        # 2026-09-10T13:00:00-04:00
        datetime_match = re.search(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}",
            onclick,
        )
        datetime_value = datetime_match.group(0) if datetime_match else None

        slots.append(
            {
                "label": label,
                "datetime": datetime_value,
                "booking_url": BOOKING_URL,
            }
        )

    # Stable ordering keeps seen_slots.json diffs clean.
    slots.sort(key=lambda slot: (slot.get("datetime") or "", slot["label"]))

    return slots


def load_previous_slots() -> list[dict]:
    if not STATE_FILE.exists():
        return []

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        log(f"Warning: could not read {STATE_FILE}: {exc}")
        return []


def save_current_slots(slots: list[dict]) -> None:
    STATE_FILE.write_text(
        json.dumps(slots, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def find_new_slots(current: list[dict], previous: list[dict]) -> list[dict]:
    """
    Compare against the immediately previous run.

    If a slot appears, disappears, and later appears again, it will correctly
    trigger another notification.
    """
    previous_keys = {
        (slot.get("datetime"), slot.get("label"))
        for slot in previous
    }

    return [
        slot
        for slot in current
        if (slot.get("datetime"), slot.get("label")) not in previous_keys
    ]


def write_github_outputs(new_slots: list[dict]) -> None:
    """
    Expose results to the GitHub Actions step with id='scrape'.
    """
    output_file = os.getenv("GITHUB_OUTPUT")

    if not output_file:
        return

    payload = json.dumps(
        new_slots,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    with open(output_file, "a", encoding="utf-8") as f:
        f.write(f"new_count={len(new_slots)}\n")
        f.write(f"new_slots_json={payload}\n")


def save_debug_artifacts(page) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        page.screenshot(
            path=str(ARTIFACT_DIR / "failure.png"),
            full_page=True,
        )
        log("Saved artifacts/failure.png")
    except Exception as exc:
        log(f"Could not save failure screenshot: {exc}")

    try:
        (ARTIFACT_DIR / "failure.html").write_text(
            page.content(),
            encoding="utf-8",
        )
        log("Saved artifacts/failure.html")
    except Exception as exc:
        log(f"Could not save failure HTML: {exc}")

    try:
        (ARTIFACT_DIR / "failure-url.txt").write_text(
            page.url + "\n",
            encoding="utf-8",
        )
        log("Saved artifacts/failure-url.txt")
    except Exception as exc:
        log(f"Could not save failure URL: {exc}")


def main() -> None:
    previous_slots = load_previous_slots()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            locale="en-CA",
            timezone_id="America/Toronto",
            viewport={"width": 1440, "height": 1000},
        )

        page = context.new_page()
        page.set_default_timeout(30_000)

        try:
            navigate_to_times(page)

            current_slots = extract_slots(page)
            new_slots = find_new_slots(current_slots, previous_slots)

            log("")
            log("Current availability:")
            if current_slots:
                for slot in current_slots:
                    log(f"  AVAILABLE: {slot['label']}")
            else:
                log("  No available slots.")

            log("")
            log(f"New slots since previous run: {len(new_slots)}")
            for slot in new_slots:
                log(f"  NEW: {slot['label']}")

            # Save the CURRENT state, not a permanent history.
            save_current_slots(current_slots)
            write_github_outputs(new_slots)

        except Exception:
            log("")
            log("SCRAPER FAILED.")
            log(f"URL at failure: {page.url}")
            save_debug_artifacts(page)
            raise

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
