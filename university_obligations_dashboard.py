import streamlit as st
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import time
import json
import os

# ── Config ──────────────────────────────────────────────────────────────────
st.set_page_config(page_title="University Federal Obligations", layout="wide")

API_BASE = "https://api.usaspending.gov/api/v2"

AGENCIES = {
    "Department of Defense": "DoD",
    "Department of Energy": "DOE",
    "Department of Health and Human Services": "HHS",
    "Department of Homeland Security": "DHS",
    "National Science Foundation": "NSF",
}

RECIPIENT_TYPES = [
    "public_institution_of_higher_education",
    "private_institution_of_higher_education",
    "minority_serving_institution_of_higher_education",
]

FISCAL_YEARS = list(range(2020, 2027))

US_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","PR","VI","GU","AS","MP",
]

GEOCODE_CACHE_FILE = "geocode_cache.json"


# ── Geocoding ───────────────────────────────────────────────────────────────
def load_geocode_cache() -> dict:
    if os.path.exists(GEOCODE_CACHE_FILE):
        with open(GEOCODE_CACHE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_geocode_cache(cache: dict):
    with open(GEOCODE_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


@st.cache_data(ttl=86400, show_spinner=False)
def geocode_universities(names: tuple) -> dict:
    """Geocode university names to lat/lon using Nominatim with persistent cache."""
    cache = load_geocode_cache()
    geolocator = Nominatim(user_agent="usaspending_univ_dashboard_v1", timeout=10)
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.1)

    missing = [n for n in names if n not in cache]
    if missing:
        progress = st.progress(0, text="Geocoding universities...")
        for i, name in enumerate(missing):
            progress.progress(i / len(missing), text=f"Geocoding: {name[:50]}...")
            try:
                location = geocode(name, country_codes="us")
                if location:
                    cache[name] = {"lat": location.latitude, "lon": location.longitude}
                else:
                    alt = name if "university" in name.lower() or "college" in name.lower() else name + " University"
                    location = geocode(alt, country_codes="us")
                    cache[name] = {"lat": location.latitude, "lon": location.longitude} if location else None
            except Exception:
                cache[name] = None
        progress.empty()
        save_geocode_cache(cache)

    return {n: cache.get(n) for n in names}


# ── API Fetch ───────────────────────────────────────────────────────────────
def fy_to_dates(fy: int) -> dict:
    return {"start_date": f"{fy - 1}-10-01", "end_date": f"{fy}-09-30"}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_recipients_for_fy(agency_name: str, fy: int, state_code: str = None, limit: int = 100) -> list:
    """Fetch higher-ed recipients for one agency + one FY with pagination."""
    dates = fy_to_dates(fy)
    filters = {
        "time_period": [dates],
        "agencies": [{"type": "funding", "tier": "toptier", "name": agency_name}],
        "recipient_type_names": RECIPIENT_TYPES,
    }
    if state_code:
        filters["place_of_performance_locations"] = [{"country": "USA", "state": state_code}]

    all_results = []
    page = 1
    while True:
        payload = {"filters": filters, "category": "recipient", "limit": limit, "page": page}
        try:
            resp = requests.post(f"{API_BASE}/search/spending_by_category/recipient", json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            st.error(f"API error (FY{fy}, {agency_name}, page {page}): {e}")
            break

        results = data.get("results", [])
        if not results:
            break
        all_results.extend(results)
        if len(results) < limit:
            break
        page += 1
        time.sleep(0.3)

    return all_results


def build_university_dataframe(agency_names: list, fiscal_years: list, state_code: str = None):
    """Build pivot table aggregated across agencies with 3-yr avg metrics."""
    records = []
    total_steps = len(agency_names) * len(fiscal_years)
    progress = st.progress(0, text="Fetching data...")
    step = 0

    for agency_name in agency_names:
        abbr = AGENCIES[agency_name]
        for fy in fiscal_years:
            progress.progress(step / total_steps, text=f"Fetching {abbr} FY{fy}...")
            results = fetch_recipients_for_fy(agency_name, fy, state_code)
            for r in results:
                records.append({
                    "Recipient": r.get("name", "Unknown"),
                    "Agency": abbr,
                    "FY": f"FY{fy}",
                    "Obligations ($)": r.get("amount", 0),
                })
            step += 1
            progress.progress(step / total_steps, text=f"{abbr} FY{fy} done ({len(results)} recipients)")

    progress.empty()

    if not records:
        return pd.DataFrame(), pd.DataFrame()

    df_raw = pd.DataFrame(records)

    # Pivot aggregated across agencies
    pivot = df_raw.pivot_table(
        index="Recipient", columns="FY", values="Obligations ($)", aggfunc="sum", fill_value=0,
    )
    fy_cols = [f"FY{y}" for y in fiscal_years if f"FY{y}" in pivot.columns]
    pivot = pivot[fy_cols]

    pivot["Total"] = pivot[fy_cols].sum(axis=1)

    # 3-Year Average: most recent 3 FYs
    recent_3 = fy_cols[-3:] if len(fy_cols) >= 3 else fy_cols
    pivot["3-Yr Avg"] = pivot[recent_3].replace(0, pd.NA).mean(axis=1, skipna=True).fillna(0)

    # Annual average across all FYs with data
    pivot["Annual Avg"] = pivot[fy_cols].replace(0, pd.NA).mean(axis=1, skipna=True).fillna(0)

    pivot = pivot.sort_values("Total", ascending=False)

    # Per-agency breakdown
    agency_pivot = df_raw.pivot_table(
        index="Recipient", columns="Agency", values="Obligations ($)", aggfunc="sum", fill_value=0,
    )

    return pivot, agency_pivot


def format_dollars(val):
    if pd.isna(val) or val == 0:
        return "—"
    if abs(val) >= 1e9:
        return f"${val / 1e9:,.2f}B"
    if abs(val) >= 1e6:
        return f"${val / 1e6:,.2f}M"
    if abs(val) >= 1e3:
        return f"${val / 1e3:,.1f}K"
    return f"${val:,.0f}"


# ── Sidebar ─────────────────────────────────────────────────────────────────
st.sidebar.title("Filters")

agency_mode = st.sidebar.radio("Agency Selection", ["Single Agency", "All Agencies (Aggregated)"])

if agency_mode == "Single Agency":
    selected_agency = st.sidebar.selectbox("Select Agency", options=list(AGENCIES.keys()), index=4)
    agency_list = [selected_agency]
    display_label = AGENCIES[selected_agency]
else:
    agency_list = list(AGENCIES.keys())
    display_label = "All Agencies"

state_filter = st.sidebar.selectbox("Filter by State (optional)", options=["All States"] + US_STATES, index=0)
state_code = None if state_filter == "All States" else state_filter

top_n = st.sidebar.slider("Top N universities to display", 10, 100, 25, 5)

fetch_button = st.sidebar.button("Fetch Data", type="primary", use_container_width=True)


# ── Main ────────────────────────────────────────────────────────────────────
st.title(f"University Federal Obligations — {display_label}")
st.caption("FY2020–FY2026 | Higher Education Recipients | Source: USAspending.gov API")

if state_code:
    st.info(f"Filtered to state: **{state_code}**")

if fetch_button:
    with st.spinner("Querying USAspending API..."):
        df_pivot, df_agency = build_university_dataframe(agency_list, FISCAL_YEARS, state_code)

    if df_pivot.empty:
        st.warning("No data returned. Try adjusting filters.")
    else:
        st.session_state["df_pivot"] = df_pivot
        st.session_state["df_agency"] = df_agency
        st.session_state["display_label"] = display_label


# ── Display ─────────────────────────────────────────────────────────────────
if "df_pivot" in st.session_state:
    df_pivot = st.session_state["df_pivot"]
    df_agency = st.session_state["df_agency"]
    label = st.session_state.get("display_label", display_label)

    fy_cols = [c for c in df_pivot.columns if c.startswith("FY") and c not in ("Total", "3-Yr Avg", "Annual Avg")]

    # ── KPI Row ─────────────────────────────────────────────────────────
    total_obligations = df_pivot["Total"].sum()
    num_universities = len(df_pivot)
    top_university = df_pivot.index[0] if len(df_pivot) > 0 else "N/A"
    top_amount = df_pivot["Total"].iloc[0] if len(df_pivot) > 0 else 0
    avg_3yr = df_pivot["3-Yr Avg"].mean() if len(df_pivot) > 0 else 0

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Obligations", format_dollars(total_obligations))
    k2.metric("Universities", f"{num_universities:,}")
    k3.metric("Top Recipient", top_university, format_dollars(top_amount))
    k4.metric("Avg 3-Yr Obligation", format_dollars(avg_3yr), help="Mean of the 3-year average across all universities")

    st.divider()

    # ── MAP ──────────────────────────────────────────────────────────────
    st.subheader("University Locations — Federal Obligations on US Map")

    university_names = df_pivot.head(top_n).index.tolist()
    geo_results = geocode_universities(tuple(university_names))

    map_records = []
    for name in university_names:
        coords = geo_results.get(name)
        if coords:
            row = df_pivot.loc[name]
            map_records.append({
                "University": name,
                "lat": coords["lat"],
                "lon": coords["lon"],
                "Total": row["Total"],
                "3-Yr Avg": row["3-Yr Avg"],
                "Annual Avg": row["Annual Avg"],
            })

    if map_records:
        map_df = pd.DataFrame(map_records)

        fig_map = px.scatter_geo(
            map_df,
            lat="lat",
            lon="lon",
            size="Total",
            color="3-Yr Avg",
            hover_name="University",
            hover_data={
                "Total": ":$,.0f",
                "3-Yr Avg": ":$,.0f",
                "Annual Avg": ":$,.0f",
                "lat": False,
                "lon": False,
            },
            color_continuous_scale="Blues",
            size_max=40,
            scope="usa",
        )
        fig_map.update_layout(
            height=550,
            geo=dict(
                showland=True,
                landcolor="rgb(243, 243, 243)",
                showlakes=True,
                lakecolor="rgb(204, 224, 245)",
                subunitcolor="rgb(200, 200, 200)",
                countrycolor="rgb(180, 180, 180)",
                showsubunits=True,
            ),
            coloraxis_colorbar=dict(title="3-Yr Avg ($)"),
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig_map, use_container_width=True)

        geocoded_count = len(map_records)
        failed = len(university_names) - geocoded_count
        if failed > 0:
            st.caption(f"Mapped {geocoded_count}/{len(university_names)} universities. {failed} could not be geocoded.")
    else:
        st.warning("Could not geocode any university names for the map.")

    st.divider()

    # ── Table ───────────────────────────────────────────────────────────
    st.subheader(f"Top {min(top_n, len(df_pivot))} Universities by Total Obligations")

    display_df = df_pivot.head(top_n).copy()
    display_df.index.name = "University"

    formatted = display_df.copy()
    for col in formatted.columns:
        formatted[col] = formatted[col].apply(format_dollars)

    st.dataframe(formatted, use_container_width=True, height=600)

    # ── Agency Breakdown (all-agency mode) ──────────────────────────────
    if len(agency_list) > 1 and not df_agency.empty:
        st.subheader("Obligation Breakdown by Agency")
        agency_display = df_agency.loc[df_pivot.head(top_n).index].copy()
        agency_display["Total"] = agency_display.sum(axis=1)
        agency_display = agency_display.sort_values("Total", ascending=False)

        formatted_agency = agency_display.copy()
        for col in formatted_agency.columns:
            formatted_agency[col] = formatted_agency[col].apply(format_dollars)

        st.dataframe(formatted_agency, use_container_width=True, height=400)

    # ── Download ────────────────────────────────────────────────────────
    csv = df_pivot.reset_index().to_csv(index=False)
    st.download_button(
        label="Download Full Data (CSV)",
        data=csv,
        file_name=f"university_obligations_{label.replace(' ', '_')}_FY2020_FY2026.csv",
        mime="text/csv",
    )

    st.divider()

    # ── Bar Chart: Top N Total ──────────────────────────────────────────
    st.subheader(f"Top {min(top_n, len(df_pivot))} Universities — Total Obligations")
    chart_df = df_pivot.head(top_n).reset_index()

    fig_bar = px.bar(
        chart_df, x="Total", y="Recipient", orientation="h",
        labels={"Total": "Total Obligations ($)", "Recipient": ""},
        color="3-Yr Avg", color_continuous_scale="Blues",
    )
    fig_bar.update_layout(
        yaxis=dict(autorange="reversed"),
        height=max(400, top_n * 28),
        coloraxis_colorbar=dict(title="3-Yr Avg ($)"),
    )
    fig_bar.update_traces(hovertemplate="<b>%{y}</b><br>Total: $%{x:,.0f}<extra></extra>")
    st.plotly_chart(fig_bar, use_container_width=True)

    # ── 3-Year Average Bar ──────────────────────────────────────────────
    st.subheader("3-Year Average Obligations (Most Recent 3 FYs)")
    avg_df = df_pivot.head(top_n)[["3-Yr Avg"]].reset_index()

    fig_avg = px.bar(
        avg_df, x="3-Yr Avg", y="Recipient", orientation="h",
        labels={"3-Yr Avg": "3-Year Average ($)", "Recipient": ""},
        color="3-Yr Avg", color_continuous_scale="Greens",
    )
    fig_avg.update_layout(
        yaxis=dict(autorange="reversed"),
        height=max(400, top_n * 28),
        coloraxis_showscale=False,
    )
    fig_avg.update_traces(hovertemplate="<b>%{y}</b><br>3-Yr Avg: $%{x:,.0f}<extra></extra>")
    st.plotly_chart(fig_avg, use_container_width=True)

    # ── Year-over-Year Trend: Top 10 ───────────────────────────────────
    st.subheader("Year-over-Year Trend — Top 10 Universities")
    trend_df = df_pivot.head(10)[fy_cols].reset_index().melt(
        id_vars="Recipient", var_name="Fiscal Year", value_name="Obligations ($)"
    )

    fig_line = px.line(trend_df, x="Fiscal Year", y="Obligations ($)", color="Recipient", markers=True)
    fig_line.update_layout(
        height=500,
        legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="center", x=0.5),
    )
    fig_line.update_traces(hovertemplate="<b>%{fullData.name}</b><br>%{x}: $%{y:,.0f}<extra></extra>")
    st.plotly_chart(fig_line, use_container_width=True)

    # ── Heatmap ─────────────────────────────────────────────────────────
    st.subheader(f"Obligation Heatmap — Top {min(top_n, len(df_pivot))} Universities")
    heatmap_data = df_pivot.head(top_n)[fy_cols]

    fig_heat = go.Figure(data=go.Heatmap(
        z=heatmap_data.values,
        x=heatmap_data.columns.tolist(),
        y=heatmap_data.index.tolist(),
        colorscale="Blues",
        hovertemplate="<b>%{y}</b><br>%{x}: $%{z:,.0f}<extra></extra>",
    ))
    fig_heat.update_layout(
        height=max(400, min(top_n, len(df_pivot)) * 28),
        yaxis=dict(autorange="reversed"),
    )
    st.plotly_chart(fig_heat, use_container_width=True)

else:
    st.info("Select agency mode and click **Fetch Data** to begin.")
    st.markdown("""
    **Features:**
    - **Single or All-Agency aggregation** — view one agency or sum across DoD, DOE, HHS, DHS, NSF
    - **University map** — geocoded locations with bubble size = total, color = 3-year average
    - **3-Year Average** — computed from the most recent 3 FYs with non-zero data
    - **Agency breakdown** — when aggregating, see per-agency split per university
    - **Full CSV export** with all metrics
    """)
