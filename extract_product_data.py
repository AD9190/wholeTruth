"""
Extracts every `self.__next_f.push([id, "..."])` chunk from a product page's
raw HTML, properly decodes the escaped inner string (via json.loads on the
outer array — this handles \", \n, \u003e etc. correctly, unlike hand-rolled
unescaping), and searches the decoded text for the product data blocks we
care about: whats_inside (ingredients), nutritional_facts, and anything
mentioning allergens/certifications/vegan.

This confirms the whole pipeline can run on plain `requests` — no Playwright.

Usage:
    python extract_product_data.py --url "https://thewholetruthfoods.com/products/double-cocoa-mini-bars?sku_id=38750752"
"""
import argparse
import json
import re
import sys

import requests

USER_AGENT = "TWT-ScoringBot/1.0 (+contact: replace-with-your-contact-email)"

KEYWORDS = [
    "whats_inside", "nutritional_facts", "allergen", "clean_plant_protein",
    "certifications", "vegan", "gluten", "serving_size", "servingSize",
]


def extract_next_f_chunks(html: str) -> list[str]:
    """Find every self.__next_f.push(...) call and return the raw text
    between its outer parentheses, respecting quoted strings so we don't
    stop at a ')' that's actually inside a string value."""
    chunks = []
    for m in re.finditer(r"self\.__next_f\.push\(", html):
        i = m.end()
        depth = 1
        in_string = False
        escape = False
        start = i
        while i < len(html) and depth > 0:
            c = html[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
            i += 1
        chunks.append(html[start : i - 1])
    return chunks


def decode_chunk(raw: str) -> str | None:
    """A chunk is a JSON array like [id, "escaped inner string"]. Parsing it
    as JSON gives us the correctly-unescaped inner string for free."""
    try:
        arr = json.loads(raw)
    except Exception:
        return None
    if isinstance(arr, list) and len(arr) >= 2 and isinstance(arr[1], str):
        inner = arr[1]
        # Next.js flight format often prefixes with "<hex_id>:" before the real payload.
        m = re.match(r"^[0-9a-fA-F]+:(.*)$", inner, re.DOTALL)
        return m.group(1) if m else inner
    return None


def extract_json_object_at(text: str, key_start_idx: int) -> str | None:
    """Given the index where '"somekey":{' begins (at the '{'), extract the
    balanced-brace JSON object substring."""
    brace_start = text.find("{", key_start_idx)
    if brace_start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    i = brace_start
    while i < len(text):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_start : i + 1]
        i += 1
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    args = ap.parse_args()

    resp = requests.get(args.url, headers={"User-Agent": USER_AGENT}, timeout=20)
    html = resp.text
    print(f"Raw HTML length: {len(html)} chars")

    raw_chunks = extract_next_f_chunks(html)
    print(f"Found {len(raw_chunks)} self.__next_f.push(...) chunks")

    decoded_chunks = []
    for raw in raw_chunks:
        decoded = decode_chunk(raw)
        if decoded:
            decoded_chunks.append(decoded)
    print(f"Successfully decoded {len(decoded_chunks)} chunks\n")

    full_text = "\n".join(decoded_chunks)

    for kw in KEYWORDS:
        idx = full_text.find(kw)
        status = f"found at offset {idx}" if idx != -1 else "NOT found"
        print(f"  {kw:<20} {status}")

    # Try to pull out the two objects we most care about, cleanly, as parsed JSON.
    for target_key in ["whats_inside", "nutritional_facts"]:
        idx = full_text.find(f'"{target_key}"')
        if idx == -1:
            continue
        obj_str = extract_json_object_at(full_text, idx)
        if not obj_str:
            print(f"\nCould not extract balanced object for {target_key}")
            continue
        print(f"\n=== {target_key} (parsed) ===")
        try:
            parsed = json.loads(obj_str)
            print(json.dumps(parsed, indent=2)[:3000])
        except json.JSONDecodeError as e:
            print(f"(parse failed: {e}); raw snippet:")
            print(obj_str[:1500])


if __name__ == "__main__":
    sys.exit(main())