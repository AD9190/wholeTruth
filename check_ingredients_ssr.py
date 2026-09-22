"""
The nutrition JSON-LD is confirmed server-rendered (plain HTTP GET, no
Playwright). This checks whether the "What's inside" ingredient composition
breakdown (e.g. "cashews 35%", "whey protein concentrate 17%") is ALSO in
the raw HTML — possibly embedded in a Next.js RSC/flight data blob rather
than clean JSON-LD — or whether it truly requires JS execution to appear.

If it's findable here, we can drop Playwright for the whole pipeline.

Usage:
    python check_ingredients_ssr.py --url "https://thewholetruthfoods.com/products/double-cocoa-mini-bars?sku_id=38750752"
"""
import argparse
import re
import sys

import requests

USER_AGENT = "TWT-ScoringBot/1.0 (+contact: replace-with-your-contact-email)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    # A known ingredient string from the rendered page, to search for verbatim.
    ap.add_argument("--needle", default="cashews")
    args = ap.parse_args()

    resp = requests.get(args.url, headers={"User-Agent": USER_AGENT}, timeout=20)
    html = resp.text
    print(f"Raw HTML length: {len(html)} chars")

    # Search 1: literal substring anywhere in raw HTML (covers both visible HTML and embedded JSON/RSC data)
    idx = html.lower().find(args.needle.lower())
    if idx == -1:
        print(f"'{args.needle}' NOT found anywhere in raw HTML — ingredient breakdown is client-fetched/rendered, Playwright still needed for this part.")
        return

    print(f"'{args.needle}' FOUND at offset {idx} in raw HTML. Context:\n")
    print(html[max(0, idx - 300): idx + 500])

    # Search 2: also check for "What's inside" as a landmark, and "instantised" (a distinctive word
    # from the actual ingredient text seen in rendered output) to triangulate the structure.
    for landmark in ["What's inside", "instantised", "35%"]:
        idx2 = html.find(landmark)
        print(f"\nLandmark {landmark!r}: {'found at offset ' + str(idx2) if idx2 != -1 else 'NOT found'}")


if __name__ == "__main__":
    sys.exit(main())