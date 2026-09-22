"""
Scoring engine implementing scoring_framework.md against twt.db (the DB
built by 01_fetch_catalog.py / 02_enrich_details.py).

Score = Protein Quality (0-40) + Goal/Age Alignment (0-30) + Safety (0-20)
        + Medical Condition Adjustment (-15 to +18) -> clamped to 0-100

Three hard exclusions run first (return 0 / a doctor-consult flag, not a
low score) — see check_hard_exclusions().

Design choices NOT fully specified by scoring_framework.md, flagged here
rather than silently baked in:
  - The exact DIAAS -> 0-40 curve. The framework says "DIAAS-based" but
    doesn't give a formula. PROTEIN_QUALITY_DIAAS_MIN/MAX below define a
    linear rescale; tune these against your actual product set's DIAAS
    spread once you have more SKUs scored.
  - DIAAS_REFERENCE values beyond the two the framework cites directly
    (whey_isolate 1.09, soy_isolate 0.90) are reasonable published/estimated
    values, same as scoring_framework.md's own stated approach for
    DIAAS_REFERENCE in the original spec — replace with per-SKU calculated
    DIAAS once amino_acid_profile_per_serving data exists (framework's own
    documented upgrade path).
  - Safety's lab-testing +5 bonus can't currently fire: certifications_claims
    is not populated by the scraper (no structured field for it exists on
    this site's product pages — see 02_enrich_details.py's docstring).
    Safety score is effectively capped at 17/20 (neutral 15, minus the
    chocolate penalty where applicable, never plus the testing bonus) until
    that data is sourced some other way.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from diaas_blend_helper import resolve_diaas

# ---------------------------------------------------------------------------
# Reference tables (see docstring above re: provenance / what needs refining)
# ---------------------------------------------------------------------------

DIAAS_REFERENCE = {
    "whey_isolate": 1.09,          # per scoring_framework.md, published
    "whey_concentrate": 1.00,      # estimate — refine with real data
    "casein": 1.18,                # published estimate (micellar casein)
    "soy_protein": 0.90,           # per scoring_framework.md (soy isolate), published
    "pea_protein": 0.82,           # published estimate
    "brown_rice_protein": 0.42,    # published estimate (isolated rice protein DIAAS is notably low)
    "plant_protein_blend": 0.75,   # rough blend estimate — refine per-SKU when possible
}
NEUTRAL_DIAAS = 0.75  # per scoring_framework.md: "protein_type unknown -> neutral DIAAS score"

# Linear rescale bounds for turning a DIAAS value into a 0-40 Protein Quality
# score. Not specified by the framework doc — tune once you have real spread.
PROTEIN_QUALITY_DIAAS_MIN = 0.40   # roughly "poor" quality protein
PROTEIN_QUALITY_DIAAS_MAX = 1.20   # roughly "excellent" quality protein

# Coarse keyword -> flavor-tag mapping for taste preference (section 5).
# "Cold Coffee" and "Mocha Millet" both tag as "coffee", per the framework's example.
FLAVOR_TAG_MAP = [
    (("coffee", "mocha", "cold coffee"), "coffee"),
    (("cocoa", "chocolate", "choco"), "chocolate"),
    (("vanilla",), "vanilla"),
    (("coconut",), "coconut"),
    (("peanut butter",), "peanut_butter"),
    (("peanut",), "peanut"),
    (("hazelnut",), "hazelnut"),
    (("walnut", "fudge"), "walnut"),
    (("almond", "badaam", "pista", "pistachio"), "nutty"),
    (("orange",), "orange"),
    (("lemon",), "lemon"),
    (("cranberry", "raisin"), "cranberry"),
    (("mango",), "mango"),
    (("strawberry",), "strawberry"),
    (("ragi",), "ragi"),
    (("unflavou?red", "unflavored", "unflavoured", "plain"), "unflavoured"),
]


def tag_flavor(product_name: str) -> Optional[str]:
    name_lower = product_name.lower()
    for keywords, tag in FLAVOR_TAG_MAP:
        if any(kw in name_lower for kw in keywords):
            return tag
    return None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Product:
    sku_id: str
    name: str
    price: Optional[float]
    availability: Optional[str]
    protein_type: Optional[str]
    protein_type_breakdown: Optional[list]  # parsed JSON: [{"type":..., "pct":...}, ...]
    protein_g: Optional[float]
    serving_size_g: Optional[float]
    calories_per_serving: Optional[float]
    allergens: list                          # parsed JSON list of strings
    certifications_claims: Optional[str]
    ingredients_raw: Optional[str]
    detail_fetch_status: str

    @property
    def flavor_tag(self) -> Optional[str]:
        return tag_flavor(self.name)

    @property
    def dominant_protein_type(self) -> Optional[str]:
        """For blends, use the highest-percentage component rather than
        substring-matching the joined type string — 'concentrate' matching
        inside 'whey_isolate+whey_concentrate' would wrongly treat a
        near-50/50 blend as if it were pure concentrate. Falls back to the
        plain protein_type (split on '+' defensively) when no structured
        breakdown is available."""
        if self.protein_type_breakdown:
            best = max(self.protein_type_breakdown, key=lambda c: c.get("pct") or 0)
            return best.get("type")
        if self.protein_type:
            return self.protein_type.split("+")[0]
        return None

    @property
    def is_whey_based(self) -> bool:
        return bool(self.protein_type) and "whey" in self.protein_type

    @property
    def is_concentrate_or_casein(self) -> bool:
        dt = self.dominant_protein_type
        return bool(dt) and ("concentrate" in dt or "casein" in dt)

    @property
    def is_isolate_or_plant(self) -> bool:
        dt = self.dominant_protein_type
        if not dt:
            return False
        return "isolate" in dt or any(
            t in dt for t in ("pea_protein", "brown_rice_protein", "soy_protein", "plant_protein")
        )


@dataclass
class UserProfile:
    age: Optional[int] = None
    goal: str = "general"              # "general" | "muscle_gain"
    allergies: list = field(default_factory=list)      # e.g. ["Milk", "Peanut"]
    is_vegan: bool = False
    has_ckd: bool = False
    has_diabetes: bool = False
    has_pcos: bool = False
    is_lactose_intolerant: bool = False
    preferred_flavor_tags: list = field(default_factory=list)
    disliked_flavor_tags: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# DB loading
# ---------------------------------------------------------------------------

def load_products(db_path: str, only_relevant: bool = True) -> list[Product]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where = "WHERE p.is_protein_relevant = 1" if only_relevant else ""
    rows = conn.execute(f"""
        SELECT p.sku_id, p.name, p.price, p.availability,
               d.protein_type, d.protein_type_breakdown, d.protein_g,
               d.serving_size_g, d.calories_per_serving, d.allergens,
               d.certifications_claims, d.ingredients_raw, d.detail_fetch_status
        FROM products p
        LEFT JOIN product_details d ON d.sku_id = p.sku_id
        {where}
    """).fetchall()
    conn.close()

    products = []
    for r in rows:
        allergens = []
        if r["allergens"]:
            try:
                allergens = json.loads(r["allergens"])
            except (json.JSONDecodeError, TypeError):
                pass
        breakdown = None
        if r["protein_type_breakdown"]:
            try:
                breakdown = json.loads(r["protein_type_breakdown"])
            except (json.JSONDecodeError, TypeError):
                pass
        products.append(Product(
            sku_id=r["sku_id"], name=r["name"], price=r["price"], availability=r["availability"],
            protein_type=r["protein_type"], protein_type_breakdown=breakdown, protein_g=r["protein_g"],
            serving_size_g=r["serving_size_g"], calories_per_serving=r["calories_per_serving"],
            allergens=allergens, certifications_claims=r["certifications_claims"],
            ingredients_raw=r["ingredients_raw"], detail_fetch_status=r["detail_fetch_status"] or "not_attempted",
        ))
    return products


# ---------------------------------------------------------------------------
# Hard exclusions (section: "Before scoring, three hard exclusions run first")
# ---------------------------------------------------------------------------

def check_hard_exclusions(product: Product, profile: UserProfile) -> Optional[dict]:
    """Returns None if no exclusion applies, else a dict describing why."""
    # 0. No usable nutrition/ingredient data — typically a build-your-own
    #    bundle/assortment product (e.g. "Personalised Box", "All in One")
    #    with no single fixed ingredient list. Scoring these with defaults
    #    would fabricate a plausible-looking number for a product we
    #    genuinely know nothing about — exclude rather than guess.
    if product.detail_fetch_status != "ok":
        return {
            "reason": "insufficient_data",
            "detail": f"detail_fetch_status={product.detail_fetch_status!r} — no reliable nutrition/ingredient data (likely a bundle/assortment product)",
            "score": None,
        }

    # 1. Allergen match (also covers "may_contain" once/if that data exists —
    #    currently always empty per the scraper's documented limitation).
    if profile.allergies:
        product_allergens_lower = {a.lower() for a in product.allergens}
        for allergy in profile.allergies:
            if allergy.lower() in product_allergens_lower:
                return {"reason": "allergen", "detail": f"Contains {allergy}", "score": 0}

    # 2. Vegan user + whey-based product
    if profile.is_vegan and product.is_whey_based:
        return {"reason": "vegan_whey_conflict", "detail": "Vegan user, whey-based product", "score": 0}

    # 3. CKD -> doctor-consult flag instead of a score
    if profile.has_ckd:
        return {
            "reason": "ckd_requires_doctor_consult",
            "detail": "KDIGO guidance recommends protein restriction for CKD; this is a clinical decision, not a recommendation-engine one.",
            "score": None,  # explicitly no score, not 0 — see framework doc
        }

    return None


# ---------------------------------------------------------------------------
# Component 1: Protein Quality (0-40), DIAAS-based
# ---------------------------------------------------------------------------

def protein_quality_score(product: Product) -> tuple[float, dict]:
    breakdown_json = json.dumps(product.protein_type_breakdown) if product.protein_type_breakdown else None
    diaas = resolve_diaas(breakdown_json, product.protein_type, DIAAS_REFERENCE, default_diaas=NEUTRAL_DIAAS)

    span = PROTEIN_QUALITY_DIAAS_MAX - PROTEIN_QUALITY_DIAAS_MIN
    normalized = (diaas - PROTEIN_QUALITY_DIAAS_MIN) / span
    score = max(0.0, min(1.0, normalized)) * 40.0

    return score, {"diaas": diaas, "protein_type": product.protein_type}


# ---------------------------------------------------------------------------
# Component 2: Goal/Age Alignment (0-30)
# ---------------------------------------------------------------------------

def goal_age_alignment_score(product: Product, profile: UserProfile) -> tuple[float, dict]:
    if product.protein_g is None:
        # "goal component skipped" when protein_g missing, per framework's
        # documented graceful degradation.
        return 0.0, {"skipped": True, "reason": "missing protein_g"}

    if profile.age is not None and profile.age >= 55:
        target_lo, target_hi = 30, 40
    else:
        target_lo, target_hi = 20, 25

    if profile.goal == "muscle_gain":
        target_lo += 5
        target_hi += 5

    p = product.protein_g
    if target_lo <= p <= target_hi:
        score = 30.0
    else:
        # Distance-based falloff outside the target band, floored at 0.
        # (Not specified exactly by the framework — a reasonable smooth
        # penalty rather than a hard cliff.)
        distance = (target_lo - p) if p < target_lo else (p - target_hi)
        score = max(0.0, 30.0 - distance * 2.0)

    return score, {"target_range_g": (target_lo, target_hi), "product_protein_g": p}


# ---------------------------------------------------------------------------
# Component 3: Safety (0-20)
# ---------------------------------------------------------------------------

def safety_score(product: Product) -> tuple[float, dict]:
    score = 15.0  # neutral start
    notes = []

    if product.flavor_tag == "chocolate":
        score -= 3
        notes.append("chocolate/cocoa flavor penalty (-3): Clean Label Project 2025 study finding")

    if product.certifications_claims and any(
        kw in product.certifications_claims.lower()
        for kw in ("third-party", "third party", "nabl", "lab tested")
    ):
        score += 5
        notes.append("independent lab testing bonus (+5)")

    # Note: organic is documented as a NEGATIVE safety signal per the study
    # cited in scoring_framework.md, not currently checked here since no
    # structured "organic" attribute is populated by the scraper yet.

    return max(0.0, min(20.0, score)), {"notes": notes}


# ---------------------------------------------------------------------------
# Component 4: Medical Condition Adjustment (-15 to +18)
# ---------------------------------------------------------------------------

def medical_adjustment_score(product: Product, profile: UserProfile) -> tuple[float, dict]:
    adjustment = 0.0
    notes = []

    if profile.has_diabetes and product.is_whey_based:
        adjustment += 8
        notes.append("diabetes + whey (+8)")

    if profile.has_pcos and product.is_whey_based:
        adjustment += 6
        notes.append("PCOS + whey (+6)")

    if profile.is_lactose_intolerant:
        if product.is_concentrate_or_casein:
            adjustment -= 15
            notes.append("lactose intolerant + concentrate/casein (-15)")
        elif product.is_isolate_or_plant:
            adjustment += 10
            notes.append("lactose intolerant + isolate/plant (+10)")

    adjustment = max(-15.0, min(18.0, adjustment))
    return adjustment, {"notes": notes}


# ---------------------------------------------------------------------------
# Taste preference (section 5) — kept separate from the 0-100 health score
# ---------------------------------------------------------------------------

def preference_label(product: Product, profile: UserProfile) -> str:
    tag = product.flavor_tag
    if tag is None:
        return "unknown"
    if tag in profile.preferred_flavor_tags:
        return "preferred"
    if tag in profile.disliked_flavor_tags:
        return "disliked"
    return "neutral"


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def score_product(product: Product, profile: UserProfile) -> dict:
    exclusion = check_hard_exclusions(product, profile)
    if exclusion:
        return {
            "sku_id": product.sku_id,
            "name": product.name,
            "excluded": True,
            "exclusion": exclusion,
            "score": exclusion["score"],  # None for CKD, 0 for allergen/vegan-whey
            "preference": preference_label(product, profile),
        }

    pq_score, pq_detail = protein_quality_score(product)
    ga_score, ga_detail = goal_age_alignment_score(product, profile)
    sf_score, sf_detail = safety_score(product)
    md_score, md_detail = medical_adjustment_score(product, profile)

    total = max(0.0, min(100.0, pq_score + ga_score + sf_score + md_score))

    return {
        "sku_id": product.sku_id,
        "name": product.name,
        "excluded": False,
        "score": round(total, 1),
        "breakdown": {
            "protein_quality": {"score": round(pq_score, 1), **pq_detail},
            "goal_age_alignment": {"score": round(ga_score, 1), **ga_detail},
            "safety": {"score": round(sf_score, 1), **sf_detail},
            "medical_adjustment": {"score": round(md_score, 1), **md_detail},
        },
        "preference": preference_label(product, profile),
    }


def rank_products(products: list[Product], profile: UserProfile,
                   include_out_of_stock: bool = False) -> list[dict]:
    """Sort by health score first (per section 5: 'sort by health score first,
    use preference as a tiebreaker/booster within that eligible set'),
    excluded products last. Out-of-stock products are filtered out by
    default — recommending something nobody can buy isn't useful; pass
    include_out_of_stock=True to see them anyway (e.g. for an internal
    catalog-quality view rather than an end-user recommendation list)."""
    eligible = products if include_out_of_stock else [
        p for p in products if (p.availability or "").lower() != "outofstock"
    ]
    results = [score_product(p, profile) for p in eligible]

    preference_rank = {"preferred": 0, "neutral": 1, "unknown": 1, "disliked": 2}

    def sort_key(r):
        if r["excluded"]:
            return (1, 0, 0)  # excluded products sink to the bottom
        return (0, -r["score"], preference_rank.get(r["preference"], 1))

    return sorted(results, key=sort_key)


def dedupe_by_base_product(ranked: list[dict], products_by_sku: dict[str, Product]) -> list[dict]:
    """Product names carry a ' — <Pack Option>' suffix per pack-size variant
    (e.g. 'Cold Coffee 24 g Protein Powder — Pack of 1 KG'), so the same
    flavor can occupy several consecutive slots in `ranked` at an identical
    score. For a display list, collapse to one entry per base product name —
    keeping its best-scoring variant — with the other pack options attached
    as `other_packs` rather than shown as separate ranked rows."""
    best_by_base: dict[str, dict] = {}
    order: list[str] = []

    for r in ranked:
        base_name = r["name"].split(" — ")[0].strip()
        product = products_by_sku.get(r["sku_id"])
        pack_label = r["name"].split(" — ", 1)[1].strip() if " — " in r["name"] else None
        pack_entry = {
            "pack": pack_label,
            "price": product.price if product else None,
            "availability": product.availability if product else None,
            "sku_id": r["sku_id"],
        }

        if base_name not in best_by_base:
            merged = dict(r)
            merged["name"] = base_name
            merged["other_packs"] = [pack_entry]
            best_by_base[base_name] = merged
            order.append(base_name)
        else:
            best_by_base[base_name]["other_packs"].append(pack_entry)
            # ranked is already sorted best-first, so the first occurrence of
            # a base_name is always its best-scoring variant — nothing to
            # update on the merged record itself beyond appending the pack.

    return [best_by_base[name] for name in order]


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="twt.db")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--profile", default="muscle_gain_lactose",
                     choices=["muscle_gain_lactose", "no_conditions", "diabetic", "vegan", "senior"],
                     help="which demo profile to run, to sanity-check that scoring components move independently across different users")
    args = ap.parse_args()

    demo_profiles = {
        # Original demo profile — lactose intolerance drives the medical_adj swings we've been checking.
        "muscle_gain_lactose": UserProfile(
            age=28, goal="muscle_gain", allergies=["Peanut"], is_vegan=False,
            has_diabetes=False, is_lactose_intolerant=True,
            preferred_flavor_tags=["chocolate", "coffee"], disliked_flavor_tags=["coconut"],
        ),
        # No medical conditions at all — every medical_adj should be exactly 0.0,
        # so ranking is driven purely by protein_quality + goal_align + safety.
        "no_conditions": UserProfile(
            age=30, goal="general", allergies=[], is_vegan=False,
        ),
        # Diabetic, not lactose intolerant — should favor whey (+8) regardless of
        # isolate/concentrate, unlike the lactose profile which cares about that split.
        "diabetic": UserProfile(
            age=45, goal="general", allergies=[], is_vegan=False, has_diabetes=True,
        ),
        # Vegan — every whey/casein product should hard-exclude via check_hard_exclusions,
        # leaving only plant-protein products scoreable at all.
        "vegan": UserProfile(
            age=25, goal="general", allergies=[], is_vegan=True,
        ),
        # 55+ — goal_age_alignment's target band shifts to 30-40g instead of 20-25g,
        # so the SAME products should score differently on goal_align than the 28-year-old profile above.
        "senior": UserProfile(
            age=60, goal="general", allergies=[], is_vegan=False,
        ),
    }
    demo_profile = demo_profiles[args.profile]

    products = load_products(args.db, only_relevant=True)
    print(f"Loaded {len(products)} protein-relevant products from {args.db}")
    print(f"Profile: {args.profile} -> {demo_profile}\n")
    products_by_sku = {p.sku_id: p for p in products}

    ranked = rank_products(products, demo_profile)  # out-of-stock filtered out by default
    deduped = dedupe_by_base_product(ranked, products_by_sku)

    excluded_count = sum(1 for r in deduped if r["excluded"])
    scored_count = len(deduped) - excluded_count
    print(f"{scored_count} scoreable, {excluded_count} excluded (bundles/insufficient-data/allergen/vegan-whey/etc.)\n")

    print(f"=== Top {args.top} ===")
    print("(one row per flavor; other pack sizes/prices summarized, out-of-stock variants excluded)\n")
    for r in deduped[: args.top]:
        if r["excluded"]:
            print(f"[EXCLUDED: {r['exclusion']['reason']}] {r['name']}")
            continue
        packs = ", ".join(
            (f"{p['pack']} (₹{p['price']:.0f})" if p["price"] else p["pack"])
            for p in r["other_packs"]
            if p["pack"] not in (None, "", "Default Option / Default Value")
        ) or (f"₹{r['other_packs'][0]['price']:.0f}" if r["other_packs"] and r["other_packs"][0]["price"] else "")
        print(f"{r['score']:>5.1f}  ({r['preference']:<9})  {r['name']}")
        bd = r["breakdown"]
        print(f"        protein_quality={bd['protein_quality']['score']} "
              f"goal_align={bd['goal_age_alignment']['score']} "
              f"safety={bd['safety']['score']} "
              f"medical_adj={bd['medical_adjustment']['score']}")
        print(f"        packs: {packs}")