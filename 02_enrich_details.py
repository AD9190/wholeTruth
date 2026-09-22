"""
Stage 2 (v2) — no Playwright. Every field we need turned out to be
server-rendered in the raw HTML:
  - JSON-LD <script type="application/ld+json"> block: name/sku/price/basic nutrition
  - Next.js flight data (self.__next_f.push(...) chunks): whats_inside
    (ingredient % composition) and nutritional_facts (full macro breakdown)
  - FAQPage JSON-LD: protein-type / lactose confirmation in plain English

This is a straight rewrite of the old Playwright-based version — same DB
schema, same CLI shape, dramatically simpler and faster since it's one
HTTP GET per product page.

IMPORTANT LIMITATION — read before trusting allergen data at scale:
There is no explicit `allergens` or `may_contain` field anywhere in this
site's data. `allergens` below is INFERRED from ingredient names via a
keyword map (whey/milk -> Milk, almonds/cashews -> Tree Nuts, etc.). This
catches allergens that are literal ingredients, but CANNOT catch
"may contain traces of X" cross-contamination warnings, which manufacturers
disclose separately and simply aren't present in this scraped data. Per
scoring_framework.md, allergens/may_contain drive a hard exclusion — do not
treat an empty inferred-allergens list as "confirmed safe" for someone with
a serious allergy without a manual check of the actual product packaging/page.

Usage:
    python 02_enrich_details.py --db twt.db [--limit 20] [--force]
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

USER_AGENT = "TWT-ScoringBot/1.0 (+contact: replace-with-your-contact-email)"
REQUEST_DELAY_SECONDS = 1.0  # plain HTTP GETs are cheap, but stay polite

# --- ingredient keyword -> allergen inference ------------------------------
# Coarse and intentionally conservative in scope (see module docstring).
ALLERGEN_KEYWORD_MAP = [
    (re.compile(r"\bwhey\b|\bmilk\b|\bcasein\b|\bbutter\b|\bghee\b", re.I), "Milk"),
    (re.compile(r"\bsoy\b|\bsoya\b", re.I), "Soy"),
    (re.compile(r"\balmond|\bcashew|\bwalnut|\bpistachio|\bhazelnut|\bpecan", re.I), "Tree Nuts"),
    (re.compile(r"\bpeanut", re.I), "Peanut"),
    (re.compile(r"\bwheat\b", re.I), "Wheat"),
    (re.compile(r"\begg\b", re.I), "Egg"),
]

# --- protein-type inference from ingredient list ---------------------------
PROTEIN_TYPE_KEYWORD_MAP = [
    (re.compile(r"whey\s+protein\s+isolate", re.I), "whey_isolate"),
    (re.compile(r"whey\s+protein\s+concentrate", re.I), "whey_concentrate"),
    (re.compile(r"\bcasein\b", re.I), "casein"),
    (re.compile(r"pea\s+protein", re.I), "pea_protein"),
    (re.compile(r"brown\s+rice\s+protein", re.I), "brown_rice_protein"),
    (re.compile(r"soy\s+protein", re.I), "soy_protein"),
    (re.compile(r"plant\s+protein", re.I), "plant_protein_blend"),
]


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_jsonld_blocks(html: str) -> list[dict]:
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    parsed = []
    for b in blocks:
        try:
            parsed.append(json.loads(b))
        except json.JSONDecodeError:
            continue
    return parsed


def extract_next_f_chunks(html: str) -> list[str]:
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
    try:
        arr = json.loads(raw)
    except Exception:
        return None
    if isinstance(arr, list) and len(arr) >= 2 and isinstance(arr[1], str):
        inner = arr[1]
        m = re.match(r"^[0-9a-fA-F]+:(.*)$", inner, re.DOTALL)
        return m.group(1) if m else inner
    return None


def extract_json_object_at(text: str, key_start_idx: int) -> str | None:
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


def get_flight_object(full_decoded_text: str, key: str) -> dict | None:
    idx = full_decoded_text.find(f'"{key}"')
    if idx == -1:
        return None
    obj_str = extract_json_object_at(full_decoded_text, idx)
    if not obj_str:
        return None
    try:
        return json.loads(obj_str)
    except json.JSONDecodeError:
        return None


def parse_grams(value: str) -> float | None:
    """'6.9g' -> 6.9, '13.2mg' -> 0.0132 (normalised to grams), else None."""
    if not value:
        return None
    m = re.match(r"([\d.]+)\s*(g|mg)", value.strip(), re.I)
    if not m:
        return None
    num, unit = float(m.group(1)), m.group(2).lower()
    return num / 1000 if unit == "mg" else num


def infer_allergens(ingredient_names: list[str]) -> list[str]:
    found = set()
    joined = " | ".join(ingredient_names)
    for pattern, allergen in ALLERGEN_KEYWORD_MAP:
        if pattern.search(joined):
            found.add(allergen)
    return sorted(found)


def protein_type_breakdown(ingredient_pairs: list[tuple[str, float | None]]) -> list[dict]:
    """Structured version of infer_protein_type's logic, for scoring_engine.py
    to compute a weighted-average DIAAS score across a blend rather than
    treating it as one type. Returns e.g.
    [{"type": "whey_isolate", "pct": 42.0}, {"type": "whey_concentrate", "pct": 37.1}]
    ordered by descending percentage. Percentages are the ingredient's share
    of the PRODUCT (from the ingredient list), not renormalized to sum to
    100 across just the protein components — scoring_engine.py should
    renormalize (divide each pct by their sum) before using them as DIAAS
    blend weights, since e.g. 42+37.1=79.1, not 100.
    """
    matched = []
    for name, pct in ingredient_pairs:
        for pattern, ptype in PROTEIN_TYPE_KEYWORD_MAP:
            if pattern.search(name):
                matched.append({"type": ptype, "pct": pct})
                break
    matched.sort(key=lambda d: -(d["pct"] or 0))
    return matched


def infer_protein_type(ingredient_pairs: list[tuple[str, float | None]]) -> str | None:
    """Pick protein type by actual ingredient percentage, not pattern-list order.
    If more than one distinct protein-type ingredient is present (e.g. a real
    isolate+concentrate blend), return them joined by '+' in descending %
    order — e.g. 'whey_isolate+whey_concentrate' — rather than silently
    collapsing to whichever pattern happened to be checked first. This
    matters because isolate and concentrate have meaningfully different
    DIAAS scores; a mislabeled blend quietly loses scoring accuracy."""
    matched = []  # (pct, type)
    for name, pct in ingredient_pairs:
        for pattern, ptype in PROTEIN_TYPE_KEYWORD_MAP:
            if pattern.search(name):
                matched.append((pct or 0.0, ptype))
                break  # first matching pattern per ingredient only, to avoid double-counting one ingredient
    if not matched:
        return None
    matched.sort(key=lambda x: -x[0])  # highest percentage first
    ordered_types, seen = [], set()
    for pct, t in matched:
        if t not in seen:
            ordered_types.append(t)
            seen.add(t)
    return "+".join(ordered_types)


def enrich_one(url: str) -> dict:
    result = {"detail_fetch_status": "failed"}
    try:
        html = fetch_html(url)
    except requests.RequestException as e:
        result["error"] = str(e)
        return result

    try:
        jsonld_blocks = parse_jsonld_blocks(html)
        product_block = next((b for b in jsonld_blocks if b.get("@type") == "Product"), None)
        faq_block = next((b for b in jsonld_blocks if b.get("@type") == "FAQPage"), None)

        raw_chunks = extract_next_f_chunks(html)
        decoded_chunks = [d for d in (decode_chunk(r) for r in raw_chunks) if d]
        full_text = "\n".join(decoded_chunks)

        whats_inside = get_flight_object(full_text, "whats_inside")
        nutritional_facts = get_flight_object(full_text, "nutritional_facts")

        # --- ingredients + allergen/protein-type inference ---
        ingredient_names = []
        ingredient_pairs = []  # (clean_name, pct_float_or_None) — needed for percentage-aware protein_type inference
        if whats_inside:
            for section in whats_inside.get("whats_inside_details") or []:
                for item in section.get("details") or []:
                    name = re.sub(r"\s+", " ", item.get("key") or "").strip()
                    pct_raw = item.get("value")
                    if name:
                        try:
                            pct = float(pct_raw) if pct_raw not in (None, "") else None
                        except (TypeError, ValueError):
                            pct = None
                        ingredient_names.append(f"{name} ({pct_raw}%)" if pct_raw else name)
                        ingredient_pairs.append((name, pct))

        if ingredient_names:
            result["ingredients_raw"] = "; ".join(ingredient_names)
            result["allergens"] = json.dumps(infer_allergens(ingredient_names))
            result["protein_type"] = infer_protein_type(ingredient_pairs)
            result["protein_type_breakdown"] = json.dumps(protein_type_breakdown(ingredient_pairs))

        # --- nutrition facts ---
        if nutritional_facts:
            for section in nutritional_facts.get("nutritional_fact_details") or []:
                serving_desc = section.get("description") or ""
                # Try the pattern seen on bars first: "...(27g) approx values per serving"
                m = re.search(r"\(([\d.]+)\s*g\)", serving_desc)
                if not m:
                    # Fallback for phrasing without parentheses, e.g. "serving size = 30g",
                    # "servingsize:30g" — take the first standalone gram figure in the description.
                    m = re.search(r"([\d.]+)\s*g\b", serving_desc, re.I)
                if m:
                    result["serving_size_g"] = float(m.group(1))
                if serving_desc:
                    result["serving_label_raw"] = serving_desc.strip()

                for item in section.get("nutritional_fact_items") or []:
                    key = (item.get("key") or "").strip().lower()
                    val = item.get("value") or ""
                    if key == "protein":
                        result["protein_g"] = parse_grams(val)
                    elif key.startswith("energy"):
                        m2 = re.match(r"([\d.]+)", val)
                        if m2:
                            result["calories_per_serving"] = float(m2.group(1))

        # --- fallback: JSON-LD nutrition, if flight-data parse missed it ---
        if product_block and result.get("protein_g") is None:
            nutrition = product_block.get("nutrition") or {}
            parsed_protein = parse_grams(nutrition.get("proteinContent") or "")
            if parsed_protein is not None:
                result["protein_g"] = parsed_protein
            cal = nutrition.get("calories") or ""
            m3 = re.match(r"([\d.]+)", cal)
            if m3:
                result["calories_per_serving"] = float(m3.group(1))

        # --- FAQ text: confirm/refine protein_type and grab lactose info as a claim note ---
        if faq_block:
            for qa in faq_block.get("mainEntity") or []:
                q = (qa.get("name") or "").lower()
                a = (qa.get("acceptedAnswer") or {}).get("text") or ""
                if "lactose" in q or "wpc" in a.lower() or "whey" in a.lower():
                    if not result.get("protein_type") and re.search(r"wpc\s*80|whey.*80%", a, re.I):
                        result["protein_type"] = "whey_concentrate"

        result["raw_api_capture"] = json.dumps({
            "jsonld_product": product_block,
            "whats_inside": whats_inside,
            "nutritional_facts": nutritional_facts,
        })

        result["detail_fetch_status"] = "ok" if (result.get("protein_g") is not None or ingredient_names) else "partial"
    except Exception as e:
        # Never let one product's unexpected data shape kill the whole batch —
        # record what we managed to get (if anything) and move on.
        result["detail_fetch_status"] = "partial" if len(result) > 1 else "failed"
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="twt.db")
    ap.add_argument("--limit", type=int, default=None, help="cap number of distinct product pages to enrich this run")
    ap.add_argument("--force", action="store_true", help="re-enrich even rows already marked ok/partial")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.executescript(open("schema.sql").read())
    for col_def in [
        "raw_api_capture TEXT",
        "protein_type_breakdown TEXT",
        "serving_label_raw TEXT",
        "detail_fetch_error TEXT",
    ]:
        try:
            conn.execute(f"ALTER TABLE product_details ADD COLUMN {col_def}")
            conn.commit()
            print(f"Migrated: added {col_def.split()[0]} column to existing DB.")
        except sqlite3.OperationalError:
            pass  # column already exists, fine

    where_clause = "1=1" if args.force else "(d.sku_id IS NULL OR d.detail_fetch_status IN ('failed','not_attempted'))"
    rows = conn.execute(
        f"""
        SELECT p.sku_id, p.canonical_url FROM products p
        LEFT JOIN product_details d ON d.sku_id = p.sku_id
        WHERE p.is_protein_relevant = 1
          AND {where_clause}
        """
    ).fetchall()

    url_to_sku_ids: dict[str, list[str]] = {}
    for row in rows:
        url_to_sku_ids.setdefault(row["canonical_url"], []).append(row["sku_id"])

    urls = list(url_to_sku_ids.keys())
    if args.limit:
        urls = urls[: args.limit]

    print(f"{len(urls)} distinct product pages queued.")

    for i, url in enumerate(urls, 1):
        sku_ids = url_to_sku_ids[url]
        print(f"[{i}/{len(urls)}] {url}")
        try:
            data = enrich_one(url)
        except Exception as e:
            data = {"detail_fetch_status": "failed", "error": f"{type(e).__name__}: {e}"}
        if data["detail_fetch_status"] == "failed":
            print(f"    failed: {data.get('error')}")
        else:
            print(f"    status={data['detail_fetch_status']} protein_g={data.get('protein_g')} "
                  f"protein_type={data.get('protein_type')} allergens={data.get('allergens')}")

        now = datetime.now(timezone.utc).isoformat()
        for sku_id in sku_ids:
            conn.execute(
                """
                INSERT INTO product_details (
                    sku_id, protein_type, protein_type_breakdown, protein_g,
                    serving_size_g, serving_label_raw, calories_per_serving,
                    allergens, ingredients_raw,
                    raw_api_capture, detail_fetch_status, detail_fetch_error, detail_fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku_id) DO UPDATE SET
                    protein_type=excluded.protein_type,
                    protein_type_breakdown=excluded.protein_type_breakdown,
                    protein_g=excluded.protein_g,
                    serving_size_g=excluded.serving_size_g,
                    serving_label_raw=excluded.serving_label_raw,
                    calories_per_serving=excluded.calories_per_serving,
                    allergens=excluded.allergens,
                    ingredients_raw=excluded.ingredients_raw,
                    raw_api_capture=excluded.raw_api_capture,
                    detail_fetch_status=excluded.detail_fetch_status,
                    detail_fetch_error=excluded.detail_fetch_error,
                    detail_fetched_at=excluded.detail_fetched_at
                """,
                (
                    sku_id, data.get("protein_type"), data.get("protein_type_breakdown"),
                    data.get("protein_g"), data.get("serving_size_g"), data.get("serving_label_raw"),
                    data.get("calories_per_serving"), data.get("allergens"), data.get("ingredients_raw"),
                    data.get("raw_api_capture"), data["detail_fetch_status"], data.get("error"), now,
                ),
            )
        conn.commit()
        time.sleep(REQUEST_DELAY_SECONDS)

    print("\nDone. Remember: 'allergens' is inferred from ingredients only — it does NOT capture")
    print("'may contain traces of' cross-contamination warnings, which aren't in this site's data at all.")
    print("Don't treat an empty allergens list as a confirmed-safe signal for severe allergies.")


if __name__ == "__main__":
    sys.exit(main())