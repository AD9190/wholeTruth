"""
Exports twt.db's protein-relevant products (with their scoring-relevant
fields) to a single JSON file, for scoring_ui.html to load via its file
picker. Nothing leaves your machine — the HTML page reads this file
directly in your browser via the File API, no server, no upload.

Usage:
    python export_for_ui.py --db twt.db --out products_for_ui.json
"""
import argparse
import json
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="twt.db")
    ap.add_argument("--out", default="products_for_ui.json")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT p.sku_id, p.name, p.price, p.availability, p.canonical_url,
               d.protein_type, d.protein_type_breakdown, d.protein_g,
               d.serving_size_g, d.calories_per_serving, d.allergens,
               d.certifications_claims, d.ingredients_raw, d.detail_fetch_status
        FROM products p
        LEFT JOIN product_details d ON d.sku_id = p.sku_id
        WHERE p.is_protein_relevant = 1
    """).fetchall()
    conn.close()

    products = []
    for r in rows:
        products.append({
            "sku_id": r["sku_id"],
            "name": r["name"],
            "price": r["price"],
            "availability": r["availability"],
            "protein_type": r["protein_type"],
            "protein_type_breakdown": json.loads(r["protein_type_breakdown"]) if r["protein_type_breakdown"] else None,
            "protein_g": r["protein_g"],
            "serving_size_g": r["serving_size_g"],
            "calories_per_serving": r["calories_per_serving"],
            "allergens": json.loads(r["allergens"]) if r["allergens"] else [],
            "certifications_claims": r["certifications_claims"],
            "detail_fetch_status": r["detail_fetch_status"] or "not_attempted",
        })

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"exported_at": __import__("datetime").datetime.now().isoformat(),
                    "products": products}, f, indent=2)

    print(f"Exported {len(products)} products to {args.out}")
    print("Open scoring_ui.html in a browser and load this file with the file picker.")


if __name__ == "__main__":
    main()