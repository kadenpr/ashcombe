"""
app.py — Ashcombe Advisers Company Tracker admin portal.

Run locally:
    streamlit run app.py

Deploy: push to GitHub, connect repo on share.streamlit.io, set
APP_PASSWORD in Streamlit Secrets.
"""

import base64
import hmac
import html
import json
import os
import re
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
            if hmac.compare_digest(pwd, _get_password()):
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
    except urllib.error.HTTPError as exc:
        st.warning(f"GitHub sync failed: HTTP {exc.code} — check your GITHUB_TOKEN secret.")
    except Exception:
        st.warning("GitHub sync failed — changes saved locally only.")


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

def _safe_url(url: str) -> str:
    """Return url only if it uses http/https; otherwise empty string."""
    return url if re.match(r"https?://", url) else ""


def _validate_company(
    name: str,
    url: str,
    linkedin_url: str,
    existing_names: "pd.Series",
    original_name: str = "",
) -> list[str]:
    errors = []
    if not name:
        errors.append("Company name is required.")
    elif name != original_name and name in existing_names.values:
        errors.append(f"'{name}' is already in this list.")
    if url and not re.match(r"https?://.+\..+", url):
        errors.append("Website must start with http:// or https:// and be a valid URL.")
    if linkedin_url and "linkedin.com/company/" not in linkedin_url:
        errors.append("LinkedIn URL must be a LinkedIn company page (linkedin.com/company/…).")
    return errors



def _render_partner_tab(partner: str, df: pd.DataFrame) -> pd.DataFrame:
    partner_df = df[df["partner"] == partner].copy()
    edit_key = f"edit_idx_{partner}"

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
                c1, c2, c3, c4, c5 = st.columns([3, 2.5, 2.5, 1, 1])

                display = row["name"]
                if row.get("search_name"):
                    display += f"  *(searches as: {row['search_name']})*"
                c1.markdown(f"**{display}**")

                safe_web = _safe_url(row.get("url", ""))
                c2.markdown(f"[{safe_web}]({safe_web})" if safe_web else "—")

                safe_li = _safe_url(row.get("linkedin_url", ""))
                c3.markdown(f"[LinkedIn ↗]({safe_li})" if safe_li else "—")

                if c4.button("Edit", key=f"edit_{orig_idx}", help=f"Edit {row['name']}"):
                    st.session_state[edit_key] = orig_idx
                    st.rerun()

                if c5.button("Remove", key=f"rm_{orig_idx}", help=f"Remove {row['name']}"):
                    if st.session_state.get(edit_key) == orig_idx:
                        st.session_state[edit_key] = None
                    df = df.drop(orig_idx).reset_index(drop=True)
                    save(df)
                    st.toast(f"{row['name']} removed.", icon="🗑️")
                    st.rerun()

    # ── Edit form ────────────────────────────────────────────────────────────
    editing_idx = st.session_state.get(edit_key)
    if editing_idx is not None and editing_idx in df.index:
        row = df.loc[editing_idx]
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown(
            f"<div style='background:#fffbeb; border:1px solid #fbbf24; border-radius:8px;"
            f" padding:16px 20px 4px; margin-bottom:8px;'>"
            f"<span style='font-size:13px; font-weight:700; color:#92400e;'>"
            f"✏️  Editing: {html.escape(row['name'])}</span></div>",
            unsafe_allow_html=True,
        )
        with st.form(key=f"edit_form_{partner}"):
            c1, c2 = st.columns(2)
            with c1:
                e_name = st.text_input("Company name *", value=row["name"])
                e_url = st.text_input("Website", value=row.get("url", ""))
                e_cat = st.selectbox(
                    "Category", CATEGORIES,
                    index=CATEGORIES.index(row["owner"]) if row["owner"] in CATEGORIES else 0,
                )
            with c2:
                e_linkedin = st.text_input("LinkedIn URL", value=row.get("linkedin_url", ""))
                e_search = st.text_input(
                    "Search name",
                    value=row.get("search_name", ""),
                    help=(
                        "Only needed if the company name is ambiguous. "
                        "E.g. 'GCP' → 'Growth Capital Partners'."
                    ),
                )

            cs, cc = st.columns(2)
            save_clicked = cs.form_submit_button("Save changes", type="primary", use_container_width=True)
            cancel_clicked = cc.form_submit_button("Cancel", use_container_width=True)

            if save_clicked:
                errors = _validate_company(
                    e_name.strip(), e_url.strip(), e_linkedin.strip(),
                    partner_df["name"], original_name=row["name"],
                )
                if errors:
                    for err in errors:
                        st.error(err)
                else:
                    df.at[editing_idx, "name"] = e_name.strip()
                    df.at[editing_idx, "url"] = e_url.strip()
                    df.at[editing_idx, "owner"] = e_cat
                    df.at[editing_idx, "linkedin_url"] = e_linkedin.strip()
                    df.at[editing_idx, "search_name"] = e_search.strip()
                    save(df)
                    st.session_state[edit_key] = None
                    st.toast(f"{e_name.strip()} updated.", icon="✅")
                    st.rerun()

            if cancel_clicked:
                st.session_state[edit_key] = None
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
                    errors = _validate_company(
                        new_name.strip(), new_url.strip(), new_linkedin.strip(),
                        partner_df["name"],
                    )
                    if errors:
                        for err in errors:
                            st.error(err)
                    else:
                        new_row = {
                            "name": new_name.strip(),
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
                        st.toast(f"{new_name.strip()} added to {partner}.", icon="✅")
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
