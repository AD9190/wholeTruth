"""
Weighted DIAAS calculation for protein blends.

scoring_engine.py's DIAAS_REFERENCE is (per scoring_framework.md) a lookup
table keyed by single protein_type strings — e.g. DIAAS_REFERENCE["whey_isolate"].
That breaks for blend products, where product_details.protein_type is now a
joined string like "whey_isolate+whey_concentrate" (see 02_enrich_details.py).

Use protein_type_breakdown instead — it's the structured version:
    [{"type": "whey_isolate", "pct": 42.0}, {"type": "whey_concentrate", "pct": 37.1}]

The percentages are each ingredient's share of the WHOLE PRODUCT (from the
ingredient list), not renormalized to the protein-only portion — e.g. 42.0 +
37.1 = 79.1, not 100, because ~21% of the product by weight is non-protein
ingredients (dates, coffee, etc.). This module renormalizes before weighting,
so the DIAAS blend reflects each protein source's share of the *protein*
contribution, not its share of total product weight.

Drop resolve_diaas() into scoring_engine.py's Protein Quality component in
place of a direct DIAAS_REFERENCE[protein_type] lookup.
"""
import json


def resolve_diaas(protein_type_breakdown_json: str | None, protein_type: str | None,
                   diaas_reference: dict[str, float], default_diaas: float = 0.75) -> float:
    """
    Args:
        protein_type_breakdown_json: the DB's product_details.protein_type_breakdown
            field (a JSON string), or None if missing.
        protein_type: fallback single-type string (product_details.protein_type),
            used only if breakdown is unavailable.
        diaas_reference: your existing DIAAS_REFERENCE dict, keyed by single
            type strings like "whey_isolate", "whey_concentrate", "pea_protein", etc.
        default_diaas: neutral fallback per scoring_framework.md's documented
            behavior ("protein_type unknown -> neutral DIAAS score") when we
            can't resolve a type at all.

    Returns:
        A single DIAAS float — the percentage-weighted average across all
        identified protein sources in the blend, or a single lookup if
        there's only one type, or default_diaas if nothing is resolvable.
    """
    breakdown = None
    if protein_type_breakdown_json:
        try:
            breakdown = json.loads(protein_type_breakdown_json)
        except (json.JSONDecodeError, TypeError):
            breakdown = None

    if breakdown:
        weighted_sum = 0.0
        weight_total = 0.0
        unresolved_types = []
        for component in breakdown:
            ptype = component.get("type")
            pct = component.get("pct") or 0.0
            diaas = diaas_reference.get(ptype)
            if diaas is None:
                unresolved_types.append(ptype)
                continue
            weighted_sum += diaas * pct
            weight_total += pct

        if weight_total > 0:
            # Renormalized average: each resolved component's DIAAS, weighted
            # by its share of the total resolved-protein percentage — so an
            # unresolved/unknown component in the blend doesn't silently drag
            # the score toward zero, it's just excluded from the average.
            return weighted_sum / weight_total

        # Every component was unresolved (all types missing from diaas_reference) —
        # fall through to the single-type/default path below.

    # No breakdown available, or it didn't resolve to anything — fall back to
    # a plain single-type lookup (handles legacy rows or a genuinely single-type product),
    # splitting on '+' defensively in case protein_type itself is a joined blend string.
    if protein_type:
        first_type = protein_type.split("+")[0]
        if first_type in diaas_reference:
            return diaas_reference[first_type]

    return default_diaas


if __name__ == "__main__":
    # Self-test against the real Cold Coffee blend data from this conversation.
    diaas_reference_example = {
        "whey_isolate": 1.09,
        "whey_concentrate": 1.00,  # placeholder — use your real reference table's values
        "pea_protein": 0.82,
    }
    breakdown_json = json.dumps([
        {"type": "whey_isolate", "pct": 42.0},
        {"type": "whey_concentrate", "pct": 37.1},
    ])
    result = resolve_diaas(breakdown_json, "whey_isolate+whey_concentrate", diaas_reference_example)
    expected = (1.09 * 42.0 + 1.00 * 37.1) / (42.0 + 37.1)
    assert abs(result - expected) < 1e-9, f"{result} != {expected}"
    print(f"Weighted DIAAS for the blend: {result:.4f} (matches hand-computed {expected:.4f})")

    # Single-type product should just return that type's value
    single_result = resolve_diaas(
        json.dumps([{"type": "whey_concentrate", "pct": 17.0}]),
        "whey_concentrate",
        diaas_reference_example,
    )
    assert single_result == 1.00
    print(f"Single-type case: {single_result}")

    # Unknown/missing data should fall back to the neutral default
    unknown_result = resolve_diaas(None, None, diaas_reference_example)
    assert unknown_result == 0.75
    print(f"Unknown case (neutral default): {unknown_result}")

    print("All self-tests passed.")