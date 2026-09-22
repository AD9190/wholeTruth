"""
Confirms whether the JSON-LD <script type="application/ld+json"> block
(which carries structured NutritionInformation) is present in the RAW HTML
from a plain HTTP request — i.e. server-rendered, no JS execution needed —
or only appears after client-side rendering.

If it's in the raw HTML, we can drop Playwright entirely for nutrition data
and just use requests + a JSON-LD parse, which is dramatically faster and
more reliable than driving a headless browser per product.

Usage:
    python dump_jsonld.py --url "https://thewholetruthfoods.com/products/double-cocoa-mini-bars?sku_id=38750752"
"""
import argparse
import json
import re
import sys

import requests

USER_AGENT = "TWT-ScoringBot/1.0 (+contact: replace-with-your-contact-email)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    args = ap.parse_args()

    resp = requests.get(args.url, headers={"User-Agent": USER_AGENT}, timeout=20)
    print(f"Plain HTTP GET status: {resp.status_code}, body length: {len(resp.text)} chars")

    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        resp.text,
        re.DOTALL | re.IGNORECASE,
    )
    print(f"Found {len(blocks)} JSON-LD script block(s) in raw HTML (no browser needed).\n")

    if not blocks:
        print("None found via plain HTTP — the JSON-LD is likely injected client-side. "
              "Playwright would still be required.")
        return

    for i, block in enumerate(blocks):
        print(f"=== JSON-LD block #{i+1} ===")
        try:
            data = json.loads(block)
            print(json.dumps(data, indent=2)[:4000])
        except json.JSONDecodeError as e:
            print(f"(failed to parse as JSON: {e})")
            print(block[:1000])
        print()


if __name__ == "__main__":
    sys.exit(main())