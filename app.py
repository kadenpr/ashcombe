"""
app.py — Ashcombe Advisers Company Tracker admin portal.

Run locally:
    streamlit run app.py

Deploy: push to GitHub, connect repo on share.streamlit.io, set
APP_PASSWORD in Streamlit Secrets.
"""

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import streamlit as st

COMPANIES_FILE = Path(__file__).parent / "companies.csv"
PARTNERS_FILE = Path(__file__).parent / "partners.csv"

PARTNERS = ["Simon", "Marc", "Jack", "Diyar", "Murs", "Andreas", "Josh", "George"]
CATEGORIES = ["Clients", "Targets", "Investors", "PTT", "Other"]

CATEGORY_COLOURS = {
    "Clients":   ("#dbeafe", "#1e40af"),
    "Targets":   ("#dcfce7", "#166534"),
    "Investors": ("#fef9c3", "#854d0e"),
    "PTT":       ("#ede9fe", "#5b21b6"),
    "Other":     ("#f1f5f9", "#475569"),
}


# ── Auth ────────────────────────────────────────────────────────────────────

def _get_password() -> str:
    try:
        return st.secrets["APP_PASSWORD"]
    except Exception:
        return os.environ.get("APP_PASSWORD", "ashcombe2024")


def require_auth() -> None:
    if st.session_state.get("authenticated"):
        return

    st.markdown(
        """
        <div style='max-width:420px; margin:80px auto 0; text-align:center;'>
          <div style='font-size:13px; letter-spacing:2px; text-transform:uppercase;
                      color:#1a6dc5; margin-bottom:6px;'>Ashcombe Advisers</div>
          <div style='font-size:28px; font-weight:700; color:#0d1b3e;
                      margin-bottom:32px;'>Company Tracker</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col = st.columns([1, 2, 1])[1]
    with col:
        pwd = st.text_input("Password", type="password", label_visibility="collapsed",
                            placeholder="Enter password")
        if st.button("Sign in", type="primary", use_container_width=True):
            if pwd == _get_password():
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    st.stop()


# ── CSV helpers ──────────────────────────────────────────────────────────────

def load() -> pd.DataFrame:
    df = pd.read_csv(COMPANIES_FILE, keep_default_na=False)
    if "partner" not in df.columns:
        df["partner"] = "Jack"
    if "search_name" not in df.columns:
        df["search_name"] = ""
    return df


def _push_to_github(csv_content: str, path: str) -> None:
    """Commit an updated CSV to GitHub so changes survive Streamlit restarts."""
    try:
        token = st.secrets.get("GITHUB_TOKEN", "")
        repo = st.secrets.get("GITHUB_REPO", "kadenpr/ashcombe")
        if not token:
            return
        api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            sha = json.loads(resp.read())["sha"]
        payload = json.dumps({
            "message": f"chore: update {path} via admin UI",
            "content": base64.b64encode(csv_content.encode()).decode(),
            "sha": sha,
        }).encode()
        req = urllib.request.Request(api_url, data=payload, headers=headers, method="PUT")
        urllib.request.urlopen(req)
    except Exception as exc:
        st.warning(f"Changes saved locally but GitHub sync failed: {exc}")


def save(df: pd.DataFrame) -> None:
    df.to_csv(COMPANIES_FILE, index=False)
    _push_to_github(df.to_csv(index=False), "companies.csv")


def load_partners() -> pd.DataFrame:
    if PARTNERS_FILE.exists():
        df = pd.read_csv(PARTNERS_FILE, keep_default_na=False)
    else:
        df = pd.DataFrame({"name": PARTNERS, "email": [""] * len(PARTNERS)})
    # Ensure all current partners are present
    existing = set(df["name"].tolist())
    new_rows = [{"name": p, "email": ""} for p in PARTNERS if p not in existing]
    if new_rows:
        df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
    return df


def save_partners(df: pd.DataFrame) -> None:
    df.to_csv(PARTNERS_FILE, index=False)
    _push_to_github(df.to_csv(index=False), "partners.csv")


# ── UI helpers ───────────────────────────────────────────────────────────────

def _badge(label: str) -> str:
    bg, fg = CATEGORY_COLOURS.get(label, ("#f1f5f9", "#475569"))
    return (
        f"<span style='background:{bg}; color:{fg}; font-size:11px; font-weight:600;"
        f" padding:2px 9px; border-radius:12px; margin-left:6px;'>{label}</span>"
    )


def _render_partner_tab(partner: str, df: pd.DataFrame) -> pd.DataFrame:
    partner_df = df[df["partner"] == partner].copy()

    # ── Company list ────────────────────────────────────────────────────────
    if partner_df.empty:
        st.info(f"No companies assigned to {partner} yet — add one below.")
    else:
        for category in CATEGORIES:
            cat_rows = partner_df[partner_df["owner"] == category]
            if cat_rows.empty:
                continue

            bg, _ = CATEGORY_COLOURS.get(category, ("#f1f5f9", "#475569"))
            st.markdown(
                f"<div style='background:{bg}; border-radius:6px; padding:6px 14px;"
                f" font-size:11px; font-weight:700; text-transform:uppercase;"
                f" letter-spacing:1px; color:#374151; margin:18px 0 8px;'>"
                f"{category} &nbsp;·&nbsp; {len(cat_rows)}</div>",
                unsafe_allow_html=True,
            )

            for orig_idx, row in cat_rows.iterrows():
                c1, c2, c3, c4 = st.columns([3, 3, 3, 1])

                display = row["name"]
                if row.get("search_name"):
                    display += f"  *(searches as: {row['search_name']})*"
                c1.markdown(f"**{display}**")

                if row.get("url"):
                    c2.markdown(f"[{row['url']}]({row['url']})")
                else:
                    c2.markdown("—")

                if row.get("linkedin_url"):
                    c3.markdown(f"[LinkedIn ↗]({row['linkedin_url']})")
                else:
                    c3.markdown("—")

                if c4.button("Remove", key=f"rm_{orig_idx}", help=f"Remove {row['name']}"):
                    df = df.drop(orig_idx).reset_index(drop=True)
                    save(df)
                    st.toast(f"{row['name']} removed.", icon="🗑️")
                    st.rerun()

    # ── Add company form ─────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander(f"➕  Add a company to {partner}", expanded=partner_df.empty):
        mode = st.radio(
            "mode",
            ["New company", "From existing list"],
            horizontal=True,
            label_visibility="collapsed",
            key=f"mode_{partner}",
        )

        if mode == "From existing list":
            already_here = set(partner_df["name"].tolist())
            candidates = (
                df[~df["name"].isin(already_here)]
                .drop_duplicates(subset="name")
                .sort_values("name")
            )
            if candidates.empty:
                st.info("All tracked companies are already in this list.")
            else:
                c1, c2 = st.columns([3, 1])
                selected_name = c1.selectbox(
                    "Company", candidates["name"].tolist(), key=f"pick_{partner}"
                )
                pick_cat = c2.selectbox("Category", CATEGORIES, key=f"pickcat_{partner}")

                source = candidates[candidates["name"] == selected_name].iloc[0]
                st.caption(
                    f"Website: {source['url'] or '—'}   ·   "
                    f"LinkedIn: {source['linkedin_url'] or '—'}"
                )

                if st.button("Add to list", type="primary", use_container_width=True, key=f"addexisting_{partner}"):
                    new_row = {
                        "name": source["name"],
                        "url": source["url"],
                        "owner": pick_cat,
                        "linkedin_url": source["linkedin_url"],
                        "search_name": source["search_name"],
                        "partner": partner,
                    }
                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                    save(df)
                    st.toast(f"{selected_name} added to {partner}.", icon="✅")
                    st.rerun()

        else:
            with st.form(key=f"add_{partner}", clear_on_submit=True):
                c1, c2 = st.columns(2)
                with c1:
                    new_name = st.text_input("Company name *", placeholder="e.g. Acme Corp")
                    new_url = st.text_input("Website", placeholder="https://acmecorp.com")
                    new_cat = st.selectbox("Category", CATEGORIES)
                with c2:
                    new_linkedin = st.text_input(
                        "LinkedIn URL", placeholder="https://linkedin.com/company/acme/"
                    )
                    new_search = st.text_input(
                        "Search name",
                        placeholder="Leave blank if same as company name",
                        help=(
                            "Only needed if the company name is ambiguous. "
                            "E.g. 'GCP' → 'Growth Capital Partners'. "
                            "This is what gets sent to Google News."
                        ),
                    )

                submitted = st.form_submit_button(
                    "Add company", type="primary", use_container_width=True
                )

                if submitted:
                    name = new_name.strip()
                    if not name:
                        st.error("Company name is required.")
                    elif name in partner_df["name"].values:
                        st.error(f"'{name}' is already in {partner}'s list.")
                    else:
                        new_row = {
                            "name": name,
                            "url": new_url.strip(),
                            "owner": new_cat,
                            "linkedin_url": new_linkedin.strip(),
                            "search_name": new_search.strip(),
                            "partner": partner,
                        }
                        df = pd.concat(
                            [df, pd.DataFrame([new_row])], ignore_index=True
                        )
                        save(df)
                        st.toast(f"{name} added to {partner}.", icon="✅")
                        st.rerun()

    return df


# ── Settings tab ─────────────────────────────────────────────────────────────

def _render_settings_tab() -> None:
    st.markdown(
        "<div style='font-size:16px; font-weight:700; color:#0d1b3e;"
        " margin-bottom:4px;'>Partner Email Addresses</div>"
        "<div style='font-size:13px; color:#6b7280; margin-bottom:20px;'>"
        "Set the email address each partner's digest is sent to.</div>",
        unsafe_allow_html=True,
    )

    partners_df = load_partners()

    with st.form("partner_emails_form"):
        updated_emails: dict[str, str] = {}
        for _, row in partners_df.iterrows():
            col1, col2 = st.columns([1, 3])
            col1.markdown(
                f"<div style='padding-top:8px; font-weight:600;'>{row['name']}</div>",
                unsafe_allow_html=True,
            )
            updated_emails[row["name"]] = col2.text_input(
                row["name"],
                value=row["email"],
                placeholder=f"{row['name'].lower()}@example.com",
                label_visibility="collapsed",
                key=f"email_{row['name']}",
            )

        if st.form_submit_button("Save email addresses", type="primary", use_container_width=True):
            partners_df["email"] = partners_df["name"].map(updated_emails)
            save_partners(partners_df)
            st.toast("Email addresses saved.", icon="✅")
            st.rerun()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(
        page_title="Ashcombe — Company Tracker",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    # Suppress Streamlit's default top padding
    st.markdown(
        "<style>div[data-testid='stAppViewContainer'] { padding-top: 1rem; }"
        " #MainMenu, footer { visibility: hidden; }</style>",
        unsafe_allow_html=True,
    )

    require_auth()

    # ── Header ───────────────────────────────────────────────────────────────
    hc1, hc2 = st.columns([3, 1])
    with hc1:
        st.markdown(
            "<div style='font-size:11px; letter-spacing:2.5px; text-transform:uppercase;"
            " color:#1a6dc5; margin-bottom:2px;'>Ashcombe Advisers</div>"
            "<div style='font-size:26px; font-weight:700; color:#0d1b3e;'>Company Tracker</div>"
            "<div style='font-size:13px; color:#6b7280; margin-top:2px;'>"
            "Manage which companies appear in the daily newsletter.</div>",
            unsafe_allow_html=True,
        )
    with hc2:
        if st.button("Sign out", use_container_width=True):
            st.session_state.authenticated = False
            st.rerun()

    st.markdown("<hr style='border:none;border-top:2px solid #1a6dc5;margin:14px 0 20px;'>",
                unsafe_allow_html=True)

    # ── Metrics row ──────────────────────────────────────────────────────────
    df = load()
    mc = st.columns(len(PARTNERS) + 1)
    mc[0].metric("Total companies", len(df))
    for i, p in enumerate(PARTNERS):
        mc[i + 1].metric(p, len(df[df["partner"] == p]))

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Partner tabs + Settings ───────────────────────────────────────────────
    tab_labels = [f"📋  {p}" for p in PARTNERS] + ["⚙️  Settings"]
    tabs = st.tabs(tab_labels)
    for tab, partner in zip(tabs[:-1], PARTNERS):
        with tab:
            df = _render_partner_tab(partner, df)

    with tabs[-1]:
        _render_settings_tab()


if __name__ == "__main__":
    main()
