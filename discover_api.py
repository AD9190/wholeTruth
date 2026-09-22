"""
Debug tool — loads ONE product page and logs every network request it makes
(URL, method, resource type, status, response size). No filtering, no
guessing by content.

We know from a captured Medusa reviews-API response that this site's product
CONTENT (descriptions, and likely nutrition facts / ingredients / allergens)
is served from a Strapi CMS, separate from the Medusa commerce API used for
orders/reviews/variants. This script's job is to find that Strapi (or
equivalent) endpoint by watching the actual traffic, so 02_enrich_details.py
can target it directly instead of guessing by page text or a loose keyword
heuristic on captured JSON.

Usage:
    python discover_api.py --url "https://thewholetruthfoods.com/products/double-cocoa-mini-bars?sku_id=38750752"

Output: prints every request, and writes full details (including response
bodies under ~20KB) to discover_output.json for closer inspection.
"""
import argparse
import json
import sys

from playwright.sync_api import sync_playwright

USER_AGENT = "TWT-ScoringBot/1.0 (+contact: replace-with-your-contact-email)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default="discover_output.json")
    args = ap.parse_args()

    captured = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        def on_response(response):
            entry = {
                "url": response.url,
                "method": response.request.method,
                "resource_type": response.request.resource_type,
                "status": response.status,
                "content_type": response.headers.get("content-type", ""),
            }
            # try to grab a small body preview for JSON responses
            try:
                if "application/json" in entry["content_type"]:
                    body = response.json()
                    entry["body_preview"] = json.dumps(body)[:500]
                    entry["_full_body"] = body
            except Exception:
                pass
            captured.append(entry)

        page.on("response", on_response)
        print(f"Loading {args.url} ...")
        page.goto(args.url, wait_until="networkidle", timeout=25000)
        page.wait_for_timeout(1500)
        browser.close()

    # Print a readable summary: every XHR/fetch request (skip images/fonts/css/js noise)
    print(f"\n{len(captured)} total responses captured. XHR/fetch/document requests:\n")
    interesting = [c for c in captured if c["resource_type"] in ("xhr", "fetch", "document")]
    for c in interesting:
        print(f"  [{c['status']}] {c['resource_type']:<10} {c['url']}")
        if "body_preview" in c:
            print(f"      preview: {c['body_preview'][:200]}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(captured, f, indent=2, default=str)
    print(f"\nFull detail (including larger JSON bodies) written to {args.out}")
    print("Look for URLs containing words like 'strapi', 'cms', 'content', 'nutrition', or the product handle —")
    print("that's very likely the endpoint carrying nutrition/ingredients/allergens.")


if __name__ == "__main__":
    sys.exit(main())