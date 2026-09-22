-- Schema designed to feed scoring_engine.py per scoring_framework.md.
-- Two-table design: `products` (from llmFeed.json, cheap/complete) and
-- `product_details` (from per-page enrichment, slower/partial — many
-- fields will be NULL until crawled, which is fine: the scoring engine
-- is documented to degrade gracefully on missing data).

CREATE TABLE IF NOT EXISTS products (
    sku_id          TEXT PRIMARY KEY,   -- from llmFeed sku_id query param (unique per variant)
    sku             TEXT,               -- llmFeed "sku" field (nullable, sometimes shared across variants)
    name            TEXT NOT NULL,
    brand           TEXT,
    description_html TEXT,
    price           REAL,
    currency        TEXT,
    availability    TEXT,               -- InStock / OutOfStock
    pack_size       TEXT,
    canonical_url   TEXT NOT NULL,
    image_url       TEXT,
    source_last_updated TEXT,           -- last_updated from the feed itself
    fetched_at      TEXT NOT NULL,      -- when we pulled this row
    is_protein_relevant INTEGER DEFAULT 0  -- 1 if name/desc suggests protein powder/bar (filter for scoring)
);

CREATE TABLE IF NOT EXISTS product_details (
    sku_id              TEXT PRIMARY KEY REFERENCES products(sku_id),
    protein_type        TEXT,    -- human-readable summary, e.g. "whey_isolate+whey_concentrate" for blends (dominant % first)
    protein_type_breakdown TEXT, -- JSON array [{"type":"whey_isolate","pct":42.0},...] — use THIS for weighted DIAAS calc, not the joined string
    protein_g            REAL,   -- protein grams per serving
    serving_size_g       REAL,
    serving_label_raw    TEXT,   -- raw serving description text, e.g. "serving size = 1 scoop (30g) approx. values per serving"
    servings_per_pack    INTEGER,
    calories_per_serving  REAL,
    flavor_raw           TEXT,   -- as displayed, e.g. "Cold Coffee"
    allergens             TEXT,  -- INFERRED from ingredient names (JSON array). Does NOT cover "may contain traces" warnings — see 02_enrich_details.py docstring.
    may_contain            TEXT, -- not populated: no "may contain" field exists in the site's data. Requires manual check per product before trusting for severe-allergy exclusions.
    ingredients_raw       TEXT,
    certifications_claims TEXT,  -- e.g. "third-party lab tested", "NABL accredited", "organic" — not currently populated, no structured field found for this site
    is_vegan              INTEGER,  -- 0/1/NULL if undetermined
    lab_test_report_url   TEXT,
    raw_api_capture        TEXT,  -- full decoded JSON-LD + flight-data blobs, for debugging/future parsing
    detail_fetch_status    TEXT,  -- ok / partial / failed / not_attempted
    detail_fetch_error     TEXT,  -- exception message when status is failed/partial, for debugging without re-running
    detail_fetched_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_products_relevant ON products(is_protein_relevant);