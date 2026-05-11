"""
Fast Gym Class Booker for Railway
===================================
Self-contained script for Evolve Fitness Munich.
Pre-warms browser, polls API aggressively, books in ~15 seconds.
Deployed as a Railway cron job with persistent volume for state.

Usage:
    python booker.py --list
    python booker.py --run-next          (find and run next pending booking)
    python booker.py --run-now ID        (run booking ID immediately)
    python booker.py --dry-run ID        (dry run, no actual booking)
    python booker.py --dry-run-next      (dry run next pending)
"""

import asyncio
import argparse
import json
import os
import re
import sys
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# Force unbuffered output for GitHub Actions (no TTY = buffered by default)
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# === CONFIGURATION ===
OWNER_SLUG = "5e08c198"
BASE_URL = f"https://app.acuityscheduling.com/schedule/{OWNER_SLUG}"

CLASSES = {
    "mobility": {
        "name": "Mobility & Flexibility",
        "type_id": "14437427",
        "calendar_id": "3433314",
    },
    "strength": {
        "name": "Strength & Evolve",
        "type_id": "71511428",
        "calendar_id": "3433314",
    },
}

BOOKING_INFO = {
    "first_name": "Saloni",
    "last_name": "Mehta",
    "email": "saloni.mehta073@gmail.com",
    "phone": "15205870195",
    "code": "wellpass",
}

CET = timezone(timedelta(hours=2))

# Vacation bookings: release_time is when the slot becomes available
# Only booking classes you'll attend after returning Jun 8
VACATION_BOOKINGS = [
    # === TEST BOOKING (cancel after confirming pipeline works) ===
    {"id": 99, "release_time": datetime(2026, 5, 9, 11, 15, tzinfo=CET), "class_type": "mobility", "target": "Sat May 23, 11:15"},
        {"id": 98, "release_time": datetime(2026, 5, 11, 12, 0, tzinfo=CET), "class_type": "strength", "target": "Mon May 25, 12:00"},
    # === REAL BOOKINGS (during vacation) ===
    {"id": 1, "release_time": datetime(2026, 5, 29, 17, 0, tzinfo=CET), "class_type": "strength", "target": "Fri Jun 12, 17:00"},
    {"id": 2, "release_time": datetime(2026, 5, 30, 11, 15, tzinfo=CET), "class_type": "mobility", "target": "Sat Jun 13, 11:15"},
    {"id": 3, "release_time": datetime(2026, 6, 1, 12, 0, tzinfo=CET), "class_type": "strength", "target": "Mon Jun 15, 12:00"},
    {"id": 4, "release_time": datetime(2026, 6, 5, 17, 0, tzinfo=CET), "class_type": "strength", "target": "Fri Jun 19, 17:00"},
    {"id": 5, "release_time": datetime(2026, 6, 6, 11, 15, tzinfo=CET), "class_type": "mobility", "target": "Sat Jun 20, 11:15"},
]

# State persistence — use Railway volume mount if available, else local
DATA_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATUS_FILE = DATA_DIR / "booking_status.json"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"


def load_status():
    if STATUS_FILE.exists():
        return json.loads(STATUS_FILE.read_text())
    return {}


def save_status(status):
    STATUS_FILE.write_text(json.dumps(status, indent=2, default=str))


def parse_target_date(target_date: str):
    match = re.match(r'\w+ (\w+) (\d+), (\d+):(\d+)', target_date)
    if not match:
        raise ValueError(f"Could not parse: {target_date}")
    month_str, day_str, hour_str, min_str = match.groups()
    month_map = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                 "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    return datetime(2026, month_map[month_str], int(day_str),
                    int(hour_str), int(min_str), tzinfo=CET)


def find_next_booking():
    """Find the next booking that should run now (release time within next 15 min, not yet booked)."""
    status = load_status()
    now = datetime.now(CET)
    
    for booking in VACATION_BOOKINGS:
        bid = str(booking["id"])
        if status.get(bid, {}).get("booked"):
            continue
        # Accept bookings releasing within next 15 min (gives cron delay buffer)
        if booking["release_time"] <= now + timedelta(minutes=30) and booking["release_time"] >= now - timedelta(minutes=5):
            return booking
    return None


async def fast_book(class_type: str, target_date: str, release_time: datetime = None, dry_run: bool = False):
    """
    Fast booking with pre-warm and aggressive polling.
    Designed for slots that fill in <60 seconds.
    
    Args:
        release_time: When the slot actually becomes available (from booking config).
                      If None, falls back to polling immediately.
    """
    class_config = CLASSES[class_type]
    target_dt = parse_target_date(target_date)
    target_date_str = target_dt.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  BOOKING: {class_config['name']}")
    print(f"  Target: {target_date}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  Time: {datetime.now(CET).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Release: {target_dt.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*60}\n")

    # === PHASE 1: PRE-WARM ===
    print("[PHASE 1] Pre-warming...")
    
    api_session = requests.Session()
    api_session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    api_session.get(f"https://app.acuityscheduling.com/schedule/{OWNER_SLUG}")
    
    api_url = "https://app.acuityscheduling.com/api/scheduling/v1/availability/times"
    poll_params = {
        "owner": OWNER_SLUG,
        "appointmentTypeId": class_config["type_id"],
        "calendarId": class_config["calendar_id"],
        "startDate": target_date_str,
        "maxDays": "1",
        "timezone": "Europe/Berlin",
    }
    
    test_resp = api_session.get(api_url, params=poll_params, timeout=5)
    print(f"  ✓ API: {test_resp.status_code} ({test_resp.elapsed.total_seconds()*1000:.0f}ms)")

    # Launch browser
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"]
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        locale="en-US",
    )
    page = await context.new_page()

    url = f"{BASE_URL}/appointment/{class_config['type_id']}/calendar/{class_config['calendar_id']}"
    await page.goto(url, wait_until="networkidle")
    print(f"  ✓ Browser ready")

    # === PHASE 2: WAIT FOR RELEASE TIME ===
    now = datetime.now(CET)
    # Use the actual release_time from booking config, NOT the target class date
    if release_time:
        wait_until = release_time - timedelta(seconds=5)
    else:
        # Fallback: poll immediately if no release_time provided
        wait_until = now - timedelta(seconds=1)
    
    if wait_until > now:
        wait_secs = (wait_until - now).total_seconds()
        if wait_secs > 600:  # More than 10 min - something's wrong
            print(f"  ⚠️ Release is {wait_secs/60:.0f} min away - this seems too long, polling immediately")
        else:
            print(f"\n[PHASE 2] Waiting {wait_secs:.0f}s until release ({release_time.strftime('%H:%M:%S %Z')})...")
            await asyncio.sleep(wait_secs)
    else:
        print(f"\n[PHASE 2] Release time already passed, polling immediately...")
    
    # === PHASE 3: AGGRESSIVE POLLING ===
    print(f"\n[PHASE 3] Polling for slot (every 500ms)...")
    
    slot_found = False
    max_polls = 600  # 5 minutes at 500ms
    poll_start = time.time()
    
    for i in range(max_polls):
        try:
            resp = api_session.get(api_url, params=poll_params, timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                if target_date_str in data and len(data[target_date_str]) > 0:
                    elapsed = time.time() - poll_start
                    print(f"  ✓ SLOT FOUND! ({elapsed:.1f}s) {data[target_date_str]}")
                    slot_found = True
                    break
        except:
            pass
        
        if i % 20 == 0 and i > 0:
            print(f"  Polling... ({time.time() - poll_start:.0f}s)")
        
        await asyncio.sleep(0.5)
    
    if not slot_found:
        elapsed = time.time() - poll_start
        print(f"  ✗ Slot not found after {elapsed:.0f}s ({i+1} polls)")
        try:
            await page.screenshot(path=str(SCREENSHOTS_DIR / "error_slot_not_found.png"))
            print(f"  📸 Screenshot saved: error_slot_not_found.png")
        except:
            pass
        await browser.close()
        await pw.stop()
        return False, f"Slot not found after {elapsed:.0f}s"

    # === PHASE 4: RACE TO BOOK ===
    print(f"\n[PHASE 4] RACING...")
    race_start = time.time()

    try:
        # Reload to see new slot
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(1000)
        
        # Find target slot (click More Times if needed)
        target_slot = None
        for _ in range(15):
            time_buttons = await page.query_selector_all("button.time-selection")
            for btn in time_buttons:
                aria = await btn.get_attribute("aria-label") or ""
                day_num = str(target_dt.day)
                month_name = target_dt.strftime("%B")
                short_month = target_dt.strftime("%b")
                if (month_name in aria or short_month in aria) and f" {day_num}" in aria:
                    target_slot = btn
                    break
            if target_slot:
                break
            more_btn = page.locator("button[aria-label='More Times']")
            if await more_btn.count() > 0:
                await more_btn.click()
                await page.wait_for_timeout(800)
            else:
                if time_buttons:
                    target_slot = time_buttons[-1]
                break
        
        if not target_slot:
            await page.screenshot(path=str(SCREENSHOTS_DIR / "error_no_slot_on_page.png"))
            raise Exception("Slot in API but not on page")

        # Click slot
        slot_aria = await target_slot.get_attribute("aria-label") or "unknown"
        print(f"  Clicking: {slot_aria}")
        await target_slot.click()
        await page.wait_for_timeout(500)
        
        # Select and continue
        select_btn = page.get_by_text("Select and continue").first
        await select_btn.dispatch_event("click")
        await page.wait_for_timeout(2000)
        
        # Wait for form
        for _ in range(5):
            if await page.query_selector("#client\\.firstName"):
                break
            await page.wait_for_timeout(1000)
        
        if not await page.query_selector("#client\\.firstName"):
            raise Exception("Form did not load")

        # Fill form (no delays between fields)
        await page.fill("#client\\.firstName", BOOKING_INFO["first_name"])
        await page.fill("#client\\.lastName", BOOKING_INFO["last_name"])
        await page.locator("#client\\.phone").fill(BOOKING_INFO["phone"])
        await page.fill("#client\\.email", BOOKING_INFO["email"])
        await page.locator("label[for='7965428-0-yes']").click()
        
        form_time = time.time() - race_start
        print(f"  ✓ Form filled ({form_time:.1f}s)")

        if dry_run:
            await page.screenshot(path=str(SCREENSHOTS_DIR / "dry_run.png"))
            print(f"\n  ⚠️  DRY RUN complete ({form_time:.1f}s)")
            await browser.close()
            await pw.stop()
            return True, f"Dry run OK ({form_time:.1f}s)"

        # Continue to payment
        await page.get_by_text("CONTINUE TO PAYMENT").first.click()
        await page.wait_for_timeout(3000)

        # Enter code
        code_section = page.get_by_text("Package, gift, or coupon code")
        if await code_section.count() > 0:
            await code_section.first.click()
            await page.wait_for_timeout(500)

        code_input = page.locator("input[placeholder='Enter code']")
        if await code_input.count() == 0:
            raise Exception("Code input not found")

        await code_input.fill(BOOKING_INFO["code"])
        await code_input.press("Enter")
        await page.wait_for_timeout(3000)

        # Verify code
        body_text = await page.inner_text("body")
        if "0.00" not in body_text and "€0" not in body_text:
            await page.screenshot(path=str(SCREENSHOTS_DIR / "error_code.png"))
            raise Exception("Code did not zero out total")
        
        print(f"  ✓ Code applied ({time.time() - race_start:.1f}s)")

        # CONFIRM
        confirm_btn = page.get_by_role("button", name="CONFIRM")
        if await confirm_btn.count() == 0:
            confirm_btn = page.get_by_text("PAY & CONFIRM")
        await confirm_btn.first.click()
        await page.wait_for_timeout(8000)

        # Check success
        body_text = await page.inner_text("body")
        total_time = time.time() - race_start
        
        if "confirmed" in body_text.lower() or "booked" in body_text.lower():
            print(f"\n  🎉 CONFIRMED! ({total_time:.1f}s)")
            await page.screenshot(path=str(SCREENSHOTS_DIR / "confirmed.png"))
            return True, f"Confirmed in {total_time:.1f}s"
        else:
            await page.screenshot(path=str(SCREENSHOTS_DIR / "unclear.png"))
            return False, f"Unclear after {total_time:.1f}s: {body_text[:100]}"

    except Exception as e:
        total_time = time.time() - race_start
        print(f"\n  ✗ Error ({total_time:.1f}s): {e}")
        try:
            await page.screenshot(path=str(SCREENSHOTS_DIR / "error.png"))
        except:
            pass
        return False, f"Error: {str(e)[:100]}"
    finally:
        await browser.close()
        await pw.stop()


async def run_booking(booking_id: int, dry_run: bool = False):
    """Run a booking with retries."""
    booking = next((b for b in VACATION_BOOKINGS if b["id"] == booking_id), None)
    if not booking:
        print(f"Booking #{booking_id} not found!")
        return False

    status = load_status()
    bid = str(booking_id)

    if status.get(bid, {}).get("booked"):
        print(f"Booking #{booking_id} already completed!")
        return True

    max_retries = 3
    message = ""
    for attempt in range(1, max_retries + 1):
        print(f"\n{'='*40} Attempt {attempt}/{max_retries} {'='*40}")
        success, message = await fast_book(
            booking["class_type"],
            booking["target"],
            release_time=booking["release_time"],
            dry_run=dry_run,
        )

        if success:
            if not dry_run:
                status[bid] = {"booked": True, "time": datetime.now(CET).isoformat(), "message": message}
                save_status(status)
            return True
        else:
            # Save status after each failed attempt
            status[bid] = {
                "booked": False,
                "failed": attempt == max_retries,
                "attempts": attempt,
                "time": datetime.now(CET).isoformat(),
                "error": message,
            }
            save_status(status)
            if attempt < max_retries:
                print(f"  Retrying in 10s...")
                await asyncio.sleep(10)

    return False


async def run_next(dry_run: bool = False):
    """Find and run the next pending booking."""
    booking = find_next_booking()
    if booking:
        print(f"Next booking: #{booking['id']} - {booking['class_type']} → {booking['target']}")
        return await run_booking(booking["id"], dry_run=dry_run)
    else:
        print("No pending bookings to run.")
        return False


def list_bookings():
    status = load_status()
    now = datetime.now(CET)
    print(f"\nBookings (now: {now.strftime('%Y-%m-%d %H:%M %Z')}):\n")
    for b in VACATION_BOOKINGS:
        bid = str(b["id"])
        s = status.get(bid, {})
        if s.get("booked"):
            state = "✓ BOOKED"
        elif s.get("failed"):
            state = "✗ FAILED"
        elif b["release_time"] < now:
            state = "⚠️ MISSED"
        else:
            delta = b["release_time"] - now
            state = f"⏳ {delta.days}d {delta.seconds//3600}h"
        print(f"  #{b['id']} {b['release_time'].strftime('%a %b %d %H:%M')} {b['class_type']:<10} → {b['target']:<20} {state}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--run-next", action="store_true")
    group.add_argument("--run-now", type=int, metavar="ID")
    group.add_argument("--dry-run", type=int, metavar="ID")
    group.add_argument("--dry-run-next", action="store_true")

    args = parser.parse_args()
    SCREENSHOTS_DIR.mkdir(exist_ok=True)

    if args.list:
        list_bookings()
    elif args.run_next:
        asyncio.run(run_next())
    elif args.run_now:
        asyncio.run(run_booking(args.run_now))
    elif args.dry_run:
        asyncio.run(run_booking(args.dry_run, dry_run=True))
    elif args.dry_run_next:
        asyncio.run(run_next(dry_run=True))
