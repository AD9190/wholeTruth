"""
Debug tool — loads ONE product page, gets the fully rendered visible text,
and prints every occurrence of "protein" (case-insensitive) with ~80 chars
of context on each side, plus its character offset.

Purpose: figure out exactly where the real nutrition-table protein value
sits in the page text versus nav/footer boilerplate mentions of "protein",
so we can write a precise, trustworthy extraction rule instead of guessing.

Usage:
    python debug_protein_context.py --url "https://thewholetruthfoods.com/products/double-cocoa-mini-bars?sku_id=38750752"
"""
import argparse
import re
import sys

from playwright.sync_api import sync_playwright

USER_AGENT = "TWT-ScoringBot/1.0 (+contact: replace-with-your-contact-email)"


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

        body_text = page.inner_text("body")

        # Also check whether a <main> landmark exists and how its text differs —
        # if present, it very likely excludes nav/footer boilerplate entirely.
        main_text = None
        try:
            if page.locator("main").count() > 0:
                main_text = page.locator("main").first.inner_text()
        except Exception:
            pass

        browser.close()

    print(f"body_text length: {len(body_text)} chars")
    print(f"main_text: {'FOUND, length ' + str(len(main_text)) if main_text else 'not found'}")

    def show_occurrences(label, text):
        print(f"\n=== 'protein' occurrences in {label} ===")
        for m in re.finditer(r"protein", text, re.IGNORECASE):
            start, end = m.start(), m.end()
            ctx_start = max(0, start - 80)
            ctx_end = min(len(text), end + 80)
            snippet = text[ctx_start:ctx_end].replace("\n", " \\n ")
            print(f"  [offset {start}] ...{snippet}...")

    show_occurrences("body_text", body_text)
    if main_text:
        show_occurrences("main_text", main_text)


if __name__ == "__main__":
    sys.exit(main())