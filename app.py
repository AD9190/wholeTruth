"""
Streamlit UI for scoring_engine.py — reads twt.db directly, no manual
export/import step. Run locally with:

    streamlit run app.py

Deploy for free on Streamlit Community Cloud (share.streamlit.io):
    1. Push this whole folder to a GitHub repo, INCLUDING twt.db.
       (twt.db is just SQLite data, not code — fine to commit for a demo.
       If you'd rather not commit real data to GitHub, see the note at the
       bottom of this file about using st.secrets / an external DB instead.)
    2. Go to share.streamlit.io, connect the repo, set main file to app.py.
    3. It builds from requirements.txt automatically. Done — you get a
       public URL you can just send your coworker, no setup on their end.
"""
import os
import sqlite3

import streamlit as st

from scoring_engine import (
    UserProfile, load_products, rank_products, dedupe_by_base_product,
    FLAVOR_TAG_MAP,
)

DB_PATH = os.environ.get("TWT_DB_PATH", "twt.db")

st.set_page_config(page_title="Protein Picks", page_icon="🥤", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;700;900&family=Source+Serif+4:wght@400;500&display=swap');

html, body, [class*="css"]  { font-family: 'Source Serif 4', Georgia, serif; }
h1, h2, h3, .stMarkdown b, [data-testid="stMetricValue"] { font-family: 'Archivo', sans-serif !important; }

:root {
    --amber-deep: #8F5F17;
    --amber: #C2872D;
    --brick: #8C3A2B;
    --brick-bg: #F7ECE8;
    --preferred-bg: #EFE6D3;
}

.product-row { border-top: 1.5px solid rgba(120,110,100,0.25); padding: 0.9rem 0; }
.score-num { font-family: 'Archivo', sans-serif; font-weight: 900; font-size: 1.7rem; }
.product-name { font-family: 'Archivo', sans-serif; font-weight: 600; font-size: 1.02rem; }
.bar { display:flex; height:0.5rem; width:100%; margin:0.4rem 0; border:1px solid rgba(0,0,0,0.6); }
.bar span { display:block; height:100%; }
.seg-pq { background: var(--amber-deep); }
.seg-ga { background: var(--amber); }
.seg-sf { background: #D8D3CC; }
.seg-md-pos { background: #6B8F5A; }
.seg-md-neg { background: var(--brick); }
.meta { font-size:0.78rem; color:#6b6470; }
.packs { font-size:0.85rem; color:#6b6470; margin-top:0.2rem; }
.pref-tag { font-family:'Archivo',sans-serif; font-size:0.68rem; font-weight:700; padding:0.1rem 0.45rem; border:1.5px solid rgba(0,0,0,0.15); border-radius:2px; }
.pref-preferred { background: var(--preferred-bg); border-color: var(--amber-deep); color: var(--amber-deep); }
.pref-disliked { background: var(--brick-bg); border-color: var(--brick); color: var(--brick); }
.excluded-row { display:flex; justify-content:space-between; padding:0.4rem 0; border-top:1px solid rgba(120,110,100,0.2); font-size:0.9rem; }
.excluded-reason { font-family:'Archivo',sans-serif; font-size:0.68rem; font-weight:700; color: var(--brick); background: var(--brick-bg); padding:0.1rem 0.45rem; border-radius:2px; }
</style>
""", unsafe_allow_html=True)


@st.cache_data
def _load(db_path: str, mtime: float):
    """mtime busts the cache automatically whenever twt.db is re-scraped/updated."""
    return load_products(db_path, only_relevant=True)


def get_products():
    if not os.path.exists(DB_PATH):
        st.error(f"Can't find {DB_PATH}. Place your scraped twt.db next to app.py "
                 f"(or set the TWT_DB_PATH environment variable) and reload.")
        st.stop()
    mtime = os.path.getmtime(DB_PATH)
    return _load(DB_PATH, mtime)


def flavor_options(products):
    tags = set()
    for p in products:
        t = p.flavor_tag
        if t:
            tags.add(t)
    return sorted(tags)


def allergy_options(products):
    a = set()
    for p in products:
        a.update(p.allergens)
    return sorted(a)


EXCLUSION_LABELS = {
    "insufficient_data": "No data",
    "allergen": "Allergen",
    "vegan_whey_conflict": "Not vegan",
    "ckd_requires_doctor_consult": "See a doctor",
}


def _md(html: str):
    """st.markdown(unsafe_allow_html=True) renders Markdown first, and
    Markdown treats any line starting with 4+ spaces as a code block —
    which is exactly what our indented f-string HTML produces, so it was
    printing raw HTML as literal text instead of rendering it. Stripping
    each line's leading whitespace avoids tripping that rule."""
    st.markdown("\n".join(line.lstrip() for line in html.split("\n")), unsafe_allow_html=True)


def render_bar(pq, ga, sf, md):
    total = pq + ga + sf + abs(md)
    if total <= 0:
        return ""
    pct = lambda v: v / total * 100
    md_seg = ""
    if md != 0:
        cls = "seg-md-pos" if md > 0 else "seg-md-neg"
        md_seg = f'<span class="{cls}" style="width:{pct(abs(md)):.1f}%"></span>'
    return f"""<div class="bar">
        <span class="seg-pq" style="width:{pct(pq):.1f}%"></span>
        <span class="seg-ga" style="width:{pct(ga):.1f}%"></span>
        <span class="seg-sf" style="width:{pct(sf):.1f}%"></span>
        {md_seg}
    </div>"""


def render_product(rank, r):
    bd = r["breakdown"]
    pq, ga, sf, md = (bd["protein_quality"]["score"], bd["goal_age_alignment"]["score"],
                       bd["safety"]["score"], bd["medical_adjustment"]["score"])

    packs = [p for p in r["other_packs"]
             if p["pack"] not in (None, "", "Default Option / Default Value")
             and (p["availability"] or "").lower() != "outofstock"]
    if packs:
        packs_str = ", ".join(f"{p['pack']} (₹{p['price']:.0f})" if p["price"] else p["pack"] for p in packs)
    elif r["other_packs"] and r["other_packs"][0]["price"]:
        packs_str = f"₹{r['other_packs'][0]['price']:.0f}"
    else:
        packs_str = ""

    pref_html = ""
    if r["preference"] == "preferred":
        pref_html = '<span class="pref-tag pref-preferred">Liked flavor</span>'
    elif r["preference"] == "disliked":
        pref_html = '<span class="pref-tag pref-disliked">Disliked flavor</span>'

    _md(f"""
    <div class="product-row">
        <div style="display:flex; gap:1rem; align-items:baseline;">
            <div style="min-width:2.5rem; color:#6b6470; font-family:'Archivo',sans-serif; font-weight:700;">{rank}</div>
            <div style="flex:1;">
                <div style="display:flex; justify-content:space-between; align-items:baseline; gap:0.75rem; flex-wrap:wrap;">
                    <span class="product-name">{r['name']}</span>
                    {pref_html}
                    <span class="score-num">{r['score']:.1f}</span>
                </div>
                {render_bar(pq, ga, sf, md)}
                <div class="meta">Protein quality {pq:.1f} · Goal match {ga:.1f} · Safety {sf:.1f} · Health adj. {md:+.1f}</div>
                {f'<div class="packs">{packs_str}</div>' if packs_str else ''}
            </div>
        </div>
    </div>
    """)


# --- sidebar: profile inputs ---
products = get_products()

st.sidebar.header("Your profile")
age = st.sidebar.number_input("Age", min_value=1, max_value=110, value=30)
goal = st.sidebar.radio("Goal", options=["general", "muscle_gain"],
                         format_func=lambda g: "General health" if g == "general" else "Muscle gain")

st.sidebar.subheader("Allergies")
allergies = st.sidebar.multiselect("Avoid products containing:", allergy_options(products))

st.sidebar.subheader("Health & diet")
is_vegan = st.sidebar.checkbox("Vegan")
has_diabetes = st.sidebar.checkbox("Diabetes")
has_pcos = st.sidebar.checkbox("PCOS")
is_lactose_intolerant = st.sidebar.checkbox("Lactose intolerant")
has_ckd = st.sidebar.checkbox("Kidney disease (CKD)")

st.sidebar.subheader("Flavors")
flavor_opts = flavor_options(products)
preferred = st.sidebar.multiselect("I like:", flavor_opts)
disliked = st.sidebar.multiselect("I don't like:", [f for f in flavor_opts if f not in preferred])

profile = UserProfile(
    age=age, goal=goal, allergies=allergies, is_vegan=is_vegan,
    has_ckd=has_ckd, has_diabetes=has_diabetes, has_pcos=has_pcos,
    is_lactose_intolerant=is_lactose_intolerant,
    preferred_flavor_tags=preferred, disliked_flavor_tags=disliked,
)

# --- main area ---
st.title("Protein Picks")
st.write("Ranked by protein quality, how well it matches your goals, safety, and your health conditions.")

products_by_sku = {p.sku_id: p for p in products}
ranked = rank_products(products, profile)
deduped = dedupe_by_base_product(ranked, products_by_sku)

scored = [r for r in deduped if not r["excluded"]]
excluded = [r for r in deduped if r["excluded"]]

col1, col2 = st.columns(2)
col1.metric("Products scored", len(scored))
col2.metric("Not suitable for you", len(excluded))

for i, r in enumerate(scored, 1):
    render_product(i, r)

if excluded:
    with st.expander(f"Not suitable for you ({len(excluded)})"):
        for r in excluded:
            reason = EXCLUSION_LABELS.get(r["exclusion"]["reason"], r["exclusion"]["reason"])
            _md(f"""<div class="excluded-row"><span>{r['name']}</span>
                <span class="excluded-reason">{reason}</span></div>""")

# NOTE on data privacy for Community Cloud deployment: this app reads twt.db
# from disk at DB_PATH. Streamlit Community Cloud apps are public by default
# (anyone with the link can open the app AND see the repo if it's public) —
# if that matters, either make the GitHub repo private (Community Cloud
# supports deploying from private repos), or swap the sqlite3 file for a
# hosted DB and load DB_PATH/credentials from st.secrets instead.