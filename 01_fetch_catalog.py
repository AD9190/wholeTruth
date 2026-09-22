"""
Stage 1 — pull the full product catalog from The Whole Truth Foods' llmFeed.json
and load it into SQLite.

This is the cheap, complete part: one HTTP request gets every SKU with name,
price, availability, pack size, and canonical URL. No headless browser needed.

Usage:
    python 01_fetch_catalog.py [--db twt.db]
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import requests

FEED_URL = "https://thewholetruthfoods.com/llmFeed.json"
USER_AGENT = "TWT-ScoringBot/1.0 (+contact: replace-with-your-contact-email)"

# Keywords used to flag rows relevant to the protein-scoring use case.
# Adjust as needed — this is a coarse pre-filter, not a hard exclusion.
PROTEIN_KEYWORDS = re.compile(
    r"protein|whey|isolate|concentrate|plant protein|casein", re.IGNORECASE
)


def sku_id_from_url(canonical_url: str) -> str:
    """The feed's canonical_url carries ?sku_id=... — that's the real unique key,
    since `sku` is sometimes null or shared across variants."""
    qs = parse_qs(urlparse(canonical_url).query)
    sku_id = qs.get("sku_id", [None])[0]
    if sku_id:
        return sku_id
    # Fallback: no sku_id present (shouldn't normally happen) — use the URL itself.
    return canonical_url


def fetch_feed() -> list[dict]:
    resp = requests.get(FEED_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    products = data.get("products", [])
    if not products:
        raise RuntimeError("Feed returned no products — check the URL/shape hasn't changed.")
    return products


def load_into_db(products: list[dict], db_path: str) -> tuple[int, int]:
    conn = sqlite3.connect(db_path)
    conn.executescript(open("schema.sql").read())

    now = datetime.now(timezone.utc).isoformat()
    inserted = relevant = 0

    for p in products:
        canonical_url = p.get("canonical_url")
        if not canonical_url:
            continue  # can't dedupe/enrich without a URL, skip
        sku_id = sku_id_from_url(canonical_url)

        name = p.get("name") or ""
        desc = p.get("description") or ""
        is_relevant = bool(PROTEIN_KEYWORDS.search(name) or PROTEIN_KEYWORDS.search(desc))
        if is_relevant:
            relevant += 1

        conn.execute(
            """
            INSERT INTO products (
                sku_id, sku, name, brand, description_html, price, currency,
                availability, pack_size, canonical_url, image_url,
                source_last_updated, fetched_at, is_protein_relevant
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku_id) DO UPDATE SET
                sku=excluded.sku, name=excluded.name, brand=excluded.brand,
                description_html=excluded.description_html, price=excluded.price,
                currency=excluded.currency, availability=excluded.availability,
                pack_size=excluded.pack_size, canonical_url=excluded.canonical_url,
                image_url=excluded.image_url, source_last_updated=excluded.source_last_updated,
                fetched_at=excluded.fetched_at, is_protein_relevant=excluded.is_protein_relevant
            """,
            (
                sku_id, p.get("sku"), name, p.get("brand"), p.get("description"),
                p.get("price"), p.get("currency"), p.get("availability"),
                p.get("pack_size"), canonical_url, p.get("image_url"),
                p.get("last_updated"), now, int(is_relevant),
            ),
        )
        inserted += 1

    conn.commit()
    conn.close()
    return inserted, relevant


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="twt.db")
    args = ap.parse_args()

    print(f"Fetching {FEED_URL} ...")
    products = fetch_feed()
    print(f"Feed returned {len(products)} product rows (includes variants).")

    inserted, relevant = load_into_db(products, args.db)
    print(f"Upserted {inserted} rows into {args.db}. {relevant} flagged as protein-relevant.")
    print("Next: run 02_enrich_details.py to pull nutrition/allergen data per product page.")


if __name__ == "__main__":
    sys.exit(main())