"""
Debug tool — finds the "Nutritional Facts" section (collapsed behind a '+'
toggle per the user) and expands it, then prints what's revealed.

Tries two things:
  1. Check if the values are hidden-but-present in the DOM (textContent vs
     innerText) before bothering to click anything.
  2. Click the heading/toggle near "Nutritional Facts" and capture the
     newly-visible text.

Usage:
    python debug_nutrition_facts.py --url "https://thewholetruthfoods.com/products/double-cocoa-mini-bars?sku_id=38750752"
"""
import argparse
import re
import sys

from playwright.sync_api import sync_playwright

USER_AGENT = "TWT-ScoringBot/1.0 (+contact: replace-with-your-contact-email)"
HEADING_PATTERN = re.compile(r"nutrition(al)?\s*facts", re.IGNORECASE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    args = ap.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        page.goto(args.url, wait_until="networkidle", timeout=25000)
        page.wait_for_timeout(1500)

        main_locator = page.locator("main").first

        # --- Step 1: hidden-but-present check ---
        inner_before = main_locator.inner_text()
        raw_before = main_locator.evaluate("el => el.textContent")
        raw_before_collapsed = re.sub(r"\s+", " ", raw_before)

        print(f"BEFORE click — innerText: {len(inner_before)} chars, textContent: {len(raw_before)} chars")
        idx = raw_before_collapsed.lower().find("nutrition")
        if idx != -1:
            print("Context around 'nutrition' in textContent (pre-click):")
            print(" ", raw_before_collapsed[idx: idx + 500])
        else:
            print("'nutrition' not found in textContent at all pre-click — likely lazy-rendered on click, not just CSS-hidden.")

        # --- Step 2: find and click the heading/toggle ---
        print("\nSearching for a 'Nutritional Facts' heading/toggle to click...")
        candidates = page.get_by_text(HEADING_PATTERN)
        count = candidates.count()
        print(f"Found {count} element(s) matching /nutrition(al)? facts/i")

        if count == 0:
            print("No matching element found. Dumping full main innerText for manual inspection:")
            print(inner_before)
            browser.close()
            return

        for i in range(count):
            el = candidates.nth(i)
            try:
                text = el.inner_text()
            except Exception:
                text = "(could not read text)"
            print(f"  [{i}] tag/text preview: {text[:80]!r}")

        # Click the first match
        target = candidates.first
        try:
            target.click(timeout=5000)
        except Exception as e:
            print(f"Direct click failed ({e}), trying to click its parent element instead...")
            try:
                target.locator("xpath=..").click(timeout=5000)
            except Exception as e2:
                print(f"Parent click also failed: {e2}")

        page.wait_for_timeout(1000)

        inner_after = main_locator.inner_text()
        print(f"\nAFTER click — innerText: {len(inner_after)} chars (was {len(inner_before)})")

        idx = inner_after.lower().find("nutrition")
        if idx != -1:
            print("\nContext around 'nutrition' in innerText (post-click):")
            print(inner_after[idx: idx + 600])
        else:
            print("Still not found post-click — dumping last 1500 chars of main innerText for inspection:")
            print(inner_after[-1500:])

        browser.close()


if __name__ == "__main__":
    sys.exit(main())