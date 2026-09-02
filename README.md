# Toronto Old City Hall Wedding Slot Monitor

Checks the City of Toronto FrontDeskSuite wedding booking flow every 10 minutes and opens a GitHub Issue when a new Old City Hall appointment appears.

## Why Playwright?

The time-selection page is session-dependent. Opening its deep link without first creating a booking-flow session can fail, so this monitor starts at the wedding booking flow and navigates forward in a real headless Chromium browser.

## Setup

1. Create a **public** GitHub repository (public keeps standard GitHub Actions usage free).
2. Upload all files in this project, preserving the `.github/workflows/check.yml` path.
3. In GitHub, open **Actions** and enable workflows if prompted.
4. Open the workflow **Check Toronto Wedding Chamber** and click **Run workflow** once.
5. Confirm the run succeeds.
6. In the repository, go to **Settings → Notifications** / watch the repository so GitHub issues trigger the notifications you want.

No API keys or third-party notification accounts are required.

## How it detects openings

Toronto renders actual open appointments as buttons containing:

```css
.available-time
```

The monitor reads the appointment button's `aria-label`, which contains a human-readable time and date.

## Duplicate handling

`seen_slots.json` stores the **currently visible** slots. You are notified when a slot is newly present compared with the previous run. If a slot disappears and later reappears, you will be notified again.

## Manual test

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
playwright install chromium
python scraper.py
```

## If Toronto changes the booking flow

The run intentionally fails rather than silently saying “no availability.” On failure, GitHub Actions uploads `failure.png` and `failure.html` as a debug artifact so the selectors can be updated.
