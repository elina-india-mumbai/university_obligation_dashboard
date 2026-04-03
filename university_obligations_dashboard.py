import streamlit as st
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import time

# ── Config ──────────────────────────────────────────────────────────────────
st.set_page_config(page_title="University Federal Obligations", layout="wide")

API_BASE = "https://api.usaspending.gov/api/v2"

AGENCIES = {
    "Department of Defense": "Department of Defense",
    "Department of Energy": "Department of Energy",
    "Department of Health and Human Services": "Department of Health and Human Services",
    "Department of Homeland Security": "Department of Homeland Security",
    "National Science Foundation": "National Science Foundation",
}

AGENCY_ABBREVIATIONS = {
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

FISCAL_YEARS = list(range(2020, 2027))  # FY2020–FY2026

US_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","PR","VI","GU","AS","MP",
]


def fy_to_dates(fy: int) -> dict:
    """Convert fiscal year to start/end date strings."""
    return {
        "start_date": f"{fy - 1}-10-01",
        "end_date": f"{fy}-09-30",
    }


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_recipients_for_fy(agency_name: str, fy: int, state_code: str = None, limit: int = 100) -> list:
    """
    Fetch higher-ed recipients and their obligations for one agency + one FY.
    Uses /api/v2/search/spending_by_category/recipient
    Paginates through all results.
    """
    dates = fy_to_dates(fy)
    filters = {
        "time_period": [dates],
        "agencies": [
            {
                "type": "funding",
                "tier": "toptier",
                "name": agency_name,
            }
        ],
        "recipient_type_names": RECIPIENT_TYPES,
    }
    if state_code:
        filters["place_of_performance_locations"] = [
            {"country": "USA", "state": state_code}
        ]

    all_results = []
    page = 1
    while True:
        payload = {
            "filters": filters,
            "category": "recipient",
            "limit": limit,
            "page": page,
        }
        try:
            resp = requests.post(
                f"{API_BASE}/search/spending_by_category/recipient",
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            st.error(f"API error (FY{fy}, {agency_name}, page {page}): {e}")
            break

        results = data.get("results", [])
        if not results:
            break
        all_results.extend(results)

        # Stop if we got fewer than limit (last page)
        if len(results) < limit:
            break
        page += 1
        time.sleep(0.3)  # rate-limit courtesy

    return all_results


def build_university_dataframe(agency_name: str, fiscal_years: list, state_code: str = None) -> pd.DataFrame:
    """
    Build a pivot table: rows = universities, columns = FY obligations.
    """
    records = []
    progress = st.progress(0, text="Fetching data...")
    total = len(fiscal_years)

    for i, fy in enumerate(fiscal_years):
        progress.progress((i) / total, text=f"Fetching FY{fy}...")
        results = fetch_recipients_for_fy(agency_name, fy, state_code)
        for r in results:
            records.append({
                "Recipient": r.get("name", "Unknown"),
                "Recipient ID": r.get("id", ""),
                "FY": f"FY{fy}",
                "Obligations ($)": r.get("amount", 0),
            })
        progress.progress((i + 1) / total, text=f"FY{fy} done ({len(results)} recipients)")

    progress.empty()

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # Pivot: one row per university, one column per FY
    pivot = df.pivot_table(
        index="Recipient",
        columns="FY",
        values="Obligations ($)",
        aggfunc="sum",
        fill_value=0,
    )

    # Ensure FY columns are in order
    fy_cols = [f"FY{y}" for y in fiscal_years if f"FY{y}" in pivot.columns]
    pivot = pivot[fy_cols]

    # Add total column
    pivot["Total"] = pivot[fy_cols].sum(axis=1)

    # Sort by total descending
    pivot = pivot.sort_values("Total", ascending=False)

    return pivot


def format_dollars(val):
    """Format dollar amounts for display."""
    if val == 0:
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

selected_agency = st.sidebar.selectbox(
    "Select Agency",
    options=list(AGENCIES.keys()),
    index=4,  # default NSF
)

state_filter = st.sidebar.selectbox(
    "Filter by State (optional)",
    options=["All States"] + US_STATES,
    index=0,
)
state_code = None if state_filter == "All States" else state_filter

top_n = st.sidebar.slider("Top N universities to display", 10, 100, 25, 5)

fetch_button = st.sidebar.button("Fetch Data", type="primary", use_container_width=True)

# ── Main ────────────────────────────────────────────────────────────────────
abbr = AGENCY_ABBREVIATIONS.get(selected_agency, selected_agency)
st.title(f"University Federal Obligations — {abbr}")
st.caption(f"FY2020–FY2026 | Higher Education Recipients | Source: USAspending.gov API")

if state_code:
    st.info(f"Filtered to state: **{state_code}**")

if fetch_button:
    with st.spinner("Querying USAspending API..."):
        df_pivot = build_university_dataframe(selected_agency, FISCAL_YEARS, state_code)

    if df_pivot.empty:
        st.warning("No data returned. Try adjusting filters.")
    else:
        st.session_state["df_pivot"] = df_pivot
        st.session_state["agency_used"] = selected_agency

# Display if data exists in session state
if "df_pivot" in st.session_state:
    df_pivot = st.session_state["df_pivot"]
    agency_used = st.session_state.get("agency_used", selected_agency)
    abbr_used = AGENCY_ABBREVIATIONS.get(agency_used, agency_used)

    # ── KPI Row ─────────────────────────────────────────────────────────
    total_obligations = df_pivot["Total"].sum()
    num_universities = len(df_pivot)
    top_university = df_pivot.index[0] if len(df_pivot) > 0 else "N/A"
    top_amount = df_pivot["Total"].iloc[0] if len(df_pivot) > 0 else 0

    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("Total Obligations", format_dollars(total_obligations))
    kpi2.metric("Universities", f"{num_universities:,}")
    kpi3.metric("Top Recipient", top_university, format_dollars(top_amount))

    st.divider()

    # ── Table ───────────────────────────────────────────────────────────
    st.subheader(f"Top {min(top_n, len(df_pivot))} Universities by Total Obligations")

    display_df = df_pivot.head(top_n).copy()
    display_df.index.name = "University"

    # Format for display
    formatted = display_df.copy()
    for col in formatted.columns:
        formatted[col] = formatted[col].apply(format_dollars)

    st.dataframe(formatted, use_container_width=True, height=600)

    # ── Download ────────────────────────────────────────────────────────
    csv = df_pivot.reset_index().to_csv(index=False)
    st.download_button(
        label="Download Full Data (CSV)",
        data=csv,
        file_name=f"university_obligations_{abbr_used}_FY2020_FY2026.csv",
        mime="text/csv",
    )

    st.divider()

    # ── Bar Chart: Top N by Total ───────────────────────────────────────
    st.subheader(f"Top {min(top_n, len(df_pivot))} Universities — Total Obligations")
    chart_df = df_pivot.head(top_n).copy()
    chart_df = chart_df.reset_index()

    fig_bar = px.bar(
        chart_df,
        x="Total",
        y="Recipient",
        orientation="h",
        labels={"Total": "Total Obligations ($)", "Recipient": ""},
        color="Total",
        color_continuous_scale="Blues",
    )
    fig_bar.update_layout(
        yaxis=dict(autorange="reversed"),
        height=max(400, top_n * 28),
        showlegend=False,
        coloraxis_showscale=False,
    )
    fig_bar.update_traces(
        hovertemplate="<b>%{y}</b><br>Total: $%{x:,.0f}<extra></extra>"
    )
    st.plotly_chart(fig_bar, use_container_width=True)

    # ── Year-over-Year Trend: Top 10 ───────────────────────────────────
    st.subheader("Year-over-Year Trend — Top 10 Universities")
    fy_cols = [c for c in df_pivot.columns if c.startswith("FY") and c != "Total"]
    trend_df = df_pivot.head(10)[fy_cols].copy()
    trend_df = trend_df.reset_index().melt(
        id_vars="Recipient", var_name="Fiscal Year", value_name="Obligations ($)"
    )

    fig_line = px.line(
        trend_df,
        x="Fiscal Year",
        y="Obligations ($)",
        color="Recipient",
        markers=True,
        labels={"Obligations ($)": "Obligations ($)", "Fiscal Year": ""},
    )
    fig_line.update_layout(
        height=500,
        legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="center", x=0.5),
    )
    fig_line.update_traces(
        hovertemplate="<b>%{fullData.name}</b><br>%{x}: $%{y:,.0f}<extra></extra>"
    )
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
    st.info("Select an agency and click **Fetch Data** to begin.")
    st.markdown("""
    **How it works:**
    - Queries USAspending.gov `/search/spending_by_category/recipient` endpoint
    - Filters to higher education recipient types
    - Fetches obligations per fiscal year (FY2020–FY2026) with pagination
    - Aggregates by university name across years
    """)
