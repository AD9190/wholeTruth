"""
Quick inspection helper — avoids needing the sqlite3 CLI (not installed by
default on Windows). Uses Python's built-in sqlite3 module instead.

Usage:
    python inspect_db.py --db twt.db
    python inspect_db.py --db twt.db --sample 5   # also print sample rows
    python inspect_db.py --db twt.db --sample 0 --dump-raw-sku <sku_id>
"""
import argparse
import json
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="twt.db")
    ap.add_argument("--sample", type=int, default=0, help="print N sample rows per status")
    ap.add_argument("--dump-raw-sku", default=None, help="pretty-print the raw_api_capture JSON for one sku_id")
    ap.add_argument("--list-names", action="store_true",
                     help="print every distinct protein-relevant product name (base name, pack-size suffix stripped) — useful for curating scoring_engine.py's FLAVOR_TAG_MAP")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print("=== detail_fetch_status breakdown ===")
    statuses_present = []
    for row in conn.execute(
        "SELECT detail_fetch_status, COUNT(*) as n FROM product_details GROUP BY detail_fetch_status ORDER BY n DESC"
    ):
        print(f"  {row['detail_fetch_status']:<15} {row['n']}")
        statuses_present.append(row["detail_fetch_status"])

    print(f"\n=== products table ===")
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    relevant = conn.execute("SELECT COUNT(*) FROM products WHERE is_protein_relevant=1").fetchone()[0]
    print(f"  total rows: {total}, protein-relevant: {relevant}")

    if args.sample:
        print(f"\n=== sample rows (up to {args.sample} per status) ===")
        for status in statuses_present:  # driven by what's actually in the DB, not a hardcoded guess
            rows = conn.execute(
                """
                SELECT p.name, p.canonical_url, d.protein_type, d.protein_g,
                       d.serving_size_g, d.calories_per_serving, d.allergens,
                       d.certifications_claims, d.detail_fetch_error
                FROM product_details d
                JOIN products p ON p.sku_id = d.sku_id
                WHERE d.detail_fetch_status = ?
                LIMIT ?
                """,
                (status, args.sample),
            ).fetchall()
            if not rows:
                continue
            print(f"\n--- {status} ---")
            for r in rows:
                print(f"  {r['name']}")
                print(f"    url: {r['canonical_url']}")
                print(f"    protein_type={r['protein_type']!r}  protein_g={r['protein_g']!r}  "
                      f"serving_size_g={r['serving_size_g']!r}  calories={r['calories_per_serving']!r}")
                print(f"    allergens={r['allergens']!r}")
                print(f"    certifications={r['certifications_claims']!r}")
                if r["detail_fetch_error"]:
                    print(f"    ERROR: {r['detail_fetch_error']}")

    if args.dump_raw_sku:
        row = conn.execute(
            """SELECT protein_type, protein_type_breakdown, protein_g, serving_size_g,
                      serving_label_raw, allergens, detail_fetch_status, raw_api_capture
               FROM product_details WHERE sku_id = ?""",
            (args.dump_raw_sku,),
        ).fetchone()
        if not row:
            print(f"\nNo product_details row for sku_id={args.dump_raw_sku}")
        else:
            print(f"\n=== computed DB fields for sku_id={args.dump_raw_sku} ===")
            print(f"  status: {row['detail_fetch_status']}")
            print(f"  protein_type: {row['protein_type']!r}")
            print(f"  protein_type_breakdown: {row['protein_type_breakdown']!r}")
            print(f"  protein_g: {row['protein_g']!r}")
            print(f"  serving_size_g: {row['serving_size_g']!r}")
            print(f"  serving_label_raw: {row['serving_label_raw']!r}")
            print(f"  allergens: {row['allergens']!r}")
            if row["raw_api_capture"]:
                print(f"\n=== raw_api_capture for sku_id={args.dump_raw_sku} ===")
                payload = json.loads(row["raw_api_capture"])
                print(json.dumps(payload, indent=2)[:5000])
            else:
                print("\n(no raw_api_capture stored)")

    if args.list_names:
        rows = conn.execute(
            "SELECT DISTINCT name FROM products WHERE is_protein_relevant = 1 ORDER BY name"
        ).fetchall()
        base_names = sorted({r["name"].split(" — ")[0].strip() for r in rows})
        print(f"\n=== {len(base_names)} distinct base product names ===")
        for n in base_names:
            print(f"  {n}")

    conn.close()


if __name__ == "__main__":
    main()