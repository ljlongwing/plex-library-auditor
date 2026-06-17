import sqlite3
import streamlit as st
import pandas as pd
import plex_logic as plex
from datetime import datetime, timedelta
import time
import urllib.parse
import os

st.set_page_config(page_title="Plex Library Auditor", layout="wide")

st.markdown("""
    <style>
    /* === NAVIGATION: style horizontal radio as pill tabs === */
    div[data-testid="stRadio"] > div[role="radiogroup"] {
        background: rgba(255,255,255,0.05);
        border-radius: 10px;
        padding: 3px;
        display: flex;
        gap: 2px;
        border: 1px solid rgba(255,255,255,0.09);
        width: fit-content;
    }
    div[data-testid="stRadio"] input[type="radio"] {
        position: absolute !important;
        opacity: 0 !important;
        width: 0 !important;
        height: 0 !important;
    }
    div[data-testid="stRadio"] > div[role="radiogroup"] > label {
        padding: 7px 22px !important;
        border-radius: 8px !important;
        cursor: pointer !important;
        transition: all 0.15s ease !important;
        margin: 0 !important;
        border: 1px solid transparent !important;
    }
    div[data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked) {
        background: rgba(229, 160, 13, 0.14) !important;
        border: 1px solid rgba(229, 160, 13, 0.35) !important;
    }
    div[data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked) p {
        color: #e5a00d !important;
        font-weight: 600 !important;
    }

    /* === METRIC CARDS === */
    div[data-testid="stMetric"] {
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.09);
        border-radius: 10px;
        padding: 14px 18px !important;
    }
    div[data-testid="metric-container"] label {
        font-size: 11px !important;
        text-transform: uppercase !important;
        letter-spacing: 0.07em !important;
        color: rgba(255,255,255,0.45) !important;
    }
    div[data-testid="metric-container"] div[data-testid="stMetricValue"] {
        font-size: 1.6rem !important;
        font-weight: 700 !important;
    }

    /* === EXPANDERS === */
    div[data-testid="stExpander"] {
        border: 1px solid rgba(255,255,255,0.1) !important;
        border-radius: 8px !important;
        margin-bottom: 4px !important;
    }

    /* === STATUS PILLS === */
    .stat-pill {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 4px 13px;
        border-radius: 20px;
        font-size: 13px;
        font-weight: 500;
    }
    .stat-pill.ok {
        background: rgba(40, 167, 69, 0.15);
        border: 1px solid rgba(40, 167, 69, 0.3);
        color: #4caf50;
    }
    .stat-pill.err {
        background: rgba(220, 53, 69, 0.15);
        border: 1px solid rgba(220, 53, 69, 0.3);
        color: #ef5350;
    }

    /* === HEADER BUTTONS (minimal/transparent) === */
    div.stButton > button[kind="secondary"] {
        border: none !important;
        background-color: transparent !important;
        background: transparent !important;
        box-shadow: none !important;
        outline: none !important;
        padding: 0 !important;
        margin: 0 !important;
        min-height: 0 !important;
        height: 38px !important;
        line-height: 38px !important;
        color: inherit !important;
        display: flex !important;
        align-items: center !important;
    }
    div.stButton > button[kind="secondary"]:hover {
        color: #e5a00d !important;
    }
    div[data-testid="column"] p {
        margin: 0 !important;
        padding: 0 !important;
        line-height: 38px !important;
    }

    .progress-text {
        font-family: monospace;
        white-space: nowrap;
    }
    </style>
    """, unsafe_allow_html=True)

# Initialize session state
if 'plex_token' not in st.session_state:
    st.session_state.plex_token = plex.get_setting('plex_token')
if 'plex_url' not in st.session_state:
    st.session_state.plex_url = plex.get_setting('plex_url')
if 'tmdb_api_key' not in st.session_state:
    st.session_state.tmdb_api_key = plex.get_setting('tmdb_api_key')
if 'pin_login' not in st.session_state:
    st.session_state.pin_login = None
if 'pin_id' not in st.session_state:
    st.session_state.pin_id = None
if 'filter_rules' not in st.session_state:
    st.session_state.filter_rules = []

# --- Rule builder constants ---
_RULE_FIELDS = ["Last Watched", "Play Count", "File Size (GB)", "Date Added", "Resolution"]
_RULE_OPS = {
    "Last Watched":    ["is never", "older than (days)", "within last (days)"],
    "Play Count":      ["less than", "greater than", "equals"],
    "File Size (GB)":  ["greater than", "less than"],
    "Date Added":      ["older than (days)", "within last (days)"],
    "Resolution":      ["is", "is not"],
    "Watched by User": ["has watched", "has not watched"],
}
_DEFAULT_VALUES = {
    "Last Watched": "180",
    "Play Count": "1",
    "File Size (GB)": "10",
    "Date Added": "90",
    "Resolution": "",
    "Watched by User": "",
}

@st.cache_data(ttl=300)
def get_cached_tautulli_users():
    return plex.get_tautulli_users()

@st.cache_data(ttl=300)
def get_cached_tautulli_watched(user_id: str):
    return plex.get_tautulli_watched_keys(user_id)

def _apply_rule(df, rule, all_resolutions):
    """Apply a single rule condition to a DataFrame. Returns filtered DataFrame."""
    field, op, value = rule['field'], rule['op'], rule.get('value', '')
    now = datetime.now()

    if field == "Last Watched":
        if op == "is never":
            return df[df['last_watched_at'].isna() & (df['viewed_leaf_count'] == 0)]
        try:
            days = int(value)
        except (ValueError, TypeError):
            return df
        threshold = now - timedelta(days=days)
        if op == "older than (days)":
            return df[df['last_watched_at'].isna() | (df['last_watched_at'] < threshold)]
        elif op == "within last (days)":
            return df[df['last_watched_at'].notna() & (df['last_watched_at'] >= threshold)]

    elif field == "Play Count":
        try:
            v = int(value)
        except (ValueError, TypeError):
            return df
        count = df['viewed_leaf_count'].fillna(0)
        if op == "less than":    return df[count < v]
        elif op == "greater than": return df[count > v]
        elif op == "equals":     return df[count == v]

    elif field == "File Size (GB)":
        try:
            v = float(value)
        except (ValueError, TypeError):
            return df
        size_gb = df['size_bytes'].fillna(0) / (1024 ** 3)
        if op == "greater than": return df[size_gb > v]
        elif op == "less than":  return df[size_gb < v]

    elif field == "Date Added":
        try:
            days = int(value)
        except (ValueError, TypeError):
            return df
        threshold = now - timedelta(days=days)
        if op == "older than (days)":    return df[df['added_at'] < threshold]
        elif op == "within last (days)": return df[df['added_at'] >= threshold]

    elif field == "Resolution":
        res_val = '' if value == 'Unknown' else value
        if op == "is":     return df[df['resolution'].fillna('') == res_val]
        elif op == "is not": return df[df['resolution'].fillna('') != res_val]

    elif field == "Watched by User":
        if not value:
            return df
        watched_keys = get_cached_tautulli_watched(value)
        if op == "has watched":     return df[df['rating_key'].isin(watched_keys)]
        elif op == "has not watched": return df[~df['rating_key'].isin(watched_keys)]

    return df

def render_rule_builder(all_resolutions):
    """Renders the rule builder UI and returns the current list of rule dicts."""
    tautulli_ok = bool(plex.get_setting("tautulli_url") and plex.get_setting("tautulli_api_key"))
    field_options = _RULE_FIELDS + (["Watched by User"] if tautulli_ok else [])

    rules = st.session_state.filter_rules
    to_delete = []

    if rules:
        st.caption("All conditions are combined with AND logic.")
        for i, rule in enumerate(rules):
            col_f, col_o, col_v, col_r = st.columns([2, 2, 2, 0.5])

            with col_f:
                curr_field = rule['field'] if rule['field'] in field_options else field_options[0]
                field = st.selectbox("Field", field_options, index=field_options.index(curr_field),
                                     key=f"rf_{i}", label_visibility="collapsed")

            ops = _RULE_OPS.get(field, [])
            with col_o:
                curr_op = rule['op'] if rule['op'] in ops else ops[0]
                op = st.selectbox("Op", ops, index=ops.index(curr_op),
                                  key=f"ro_{i}", label_visibility="collapsed")

            with col_v:
                if op == "is never":
                    st.caption("(no value needed)")
                    value = ""
                elif field == "Resolution":
                    res_opts = all_resolutions
                    curr_val = rule.get('value', res_opts[0] if res_opts else '')
                    if curr_val not in res_opts:
                        curr_val = res_opts[0] if res_opts else ''
                    value = st.selectbox("Value", res_opts, index=res_opts.index(curr_val),
                                         key=f"rv_{i}", label_visibility="collapsed")
                elif field == "Watched by User":
                    users = get_cached_tautulli_users()
                    user_opts = [u['user_id'] for u in users]
                    user_labels = {u['user_id']: u['username'] for u in users}
                    if user_opts:
                        curr_val = rule.get('value', user_opts[0])
                        if curr_val not in user_opts:
                            curr_val = user_opts[0]
                        value = st.selectbox("User", user_opts,
                                             index=user_opts.index(curr_val),
                                             format_func=lambda x: user_labels.get(x, x),
                                             key=f"rv_{i}", label_visibility="collapsed")
                    else:
                        st.caption("No Tautulli users found")
                        value = rule.get('value', '')
                else:
                    value = st.text_input("Value", value=rule.get('value', _DEFAULT_VALUES.get(field, '')),
                                          key=f"rv_{i}", label_visibility="collapsed")

            with col_r:
                if st.button("✕", key=f"rm_{i}", help="Remove condition"):
                    to_delete.append(i)

            rules[i] = {"field": field, "op": op, "value": value}

    if to_delete:
        for idx in reversed(to_delete):
            rules.pop(idx)
        st.rerun()

    if st.button("＋ Add Condition"):
        rules.append({"field": "Last Watched", "op": "older than (days)", "value": "180"})
        st.rerun()

    return rules

def _smart_multiselect(label, options, default, key):
    """Multiselect shown as a compact popover button.
    Displays 'All' when everything is selected, otherwise 'N of M selected ✏'.
    Inside the popover: Select All / Clear All buttons + the full multiselect.
    """
    val_key = f"_sms_val_{key}"
    inner_key = f"_sms_inner_{key}"

    if val_key not in st.session_state:
        st.session_state[val_key] = list(default)

    # Sync changes made by the inner multiselect widget on previous render
    if inner_key in st.session_state:
        st.session_state[val_key] = list(st.session_state[inner_key])

    current = st.session_state[val_key]
    # Ensure inner_key is initialized so we never pass both default= and session state
    if inner_key not in st.session_state:
        st.session_state[inner_key] = current

    all_selected = set(options) and set(current) >= set(options)

    if all_selected or not options:
        btn_label = f"{label} — All"
    elif not current:
        btn_label = f"{label} — None selected ✏"
    elif len(current) <= 2:
        btn_label = f"{label} — {', '.join(str(x) for x in current)} ✏"
    else:
        btn_label = f"{label} — {len(current)} of {len(options)} selected ✏"

    with st.popover(btn_label, use_container_width=True):
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Select All", key=f"_sms_all_{key}", use_container_width=True):
                st.session_state[val_key] = list(options)
                st.session_state[inner_key] = list(options)
                st.rerun()
        with c2:
            if st.button("Clear All", key=f"_sms_clr_{key}", use_container_width=True):
                st.session_state[val_key] = []
                st.session_state[inner_key] = []
                st.rerun()
        st.multiselect(
            label, options=options,
            key=inner_key,
            label_visibility="collapsed",
        )

    return current


def _smart_select(label, options, default, key):
    """Single-select shown as a compact popover button, matching _smart_multiselect style."""
    val_key = f"_ss_val_{key}"

    if val_key not in st.session_state:
        st.session_state[val_key] = default

    current = st.session_state[val_key]
    if current not in options:
        current = options[0]

    is_default = current == options[0]
    btn_label = f"{label} — {current}" if is_default else f"{label} — {current} ✏"

    with st.popover(btn_label, use_container_width=True):
        choice = st.radio(label, options=options, index=options.index(current),
                          key=f"_ss_inner_{key}", label_visibility="collapsed")
        if choice != current:
            st.session_state[val_key] = choice
            st.rerun()

    return current


def main_dashboard():
    # Header Area - Row 1: Title and Status
    col_t1, col_t2 = st.columns([5, 1], vertical_alignment="center")
    with col_t1:
        st.markdown("# 🎬 Plex Library Auditor")
    with col_t2:
        pass

    # Header Area - Row 2: Refresh and Progress
    col_r1, col_r2, col_r3 = st.columns([1.5, 8, 1], vertical_alignment="center")

    refresh_state = plex.get_refresh_state()

    with col_r2:
        progress_placeholder = st.empty()
        if refresh_state["running"]:
            overall = refresh_state["overall_msg"]
            item = refresh_state["item_msg"]
            msg = f"⏳ {overall} | {item}" if item else f"⏳ {overall}"
            progress_placeholder.markdown(f"<span class='progress-text'>{msg}</span>", unsafe_allow_html=True)
        elif refresh_state["done"]:
            if refresh_state["error"]:
                progress_placeholder.markdown(f"<span class='progress-text'>❌ Refresh failed: {refresh_state['error']}</span>", unsafe_allow_html=True)
            else:
                progress_placeholder.markdown(f"<span class='progress-text'>✅ Refresh Complete!</span>", unsafe_allow_html=True)
        else:
            last_refresh = plex.get_setting('last_refreshed_at')
            if last_refresh:
                try:
                    refresh_dt = datetime.fromisoformat(last_refresh)
                    progress_placeholder.caption(f"Last refreshed: {refresh_dt.strftime('%b %d, %Y %I:%M %p')}")
                except ValueError:
                    pass

    with col_r1:
        if st.session_state.plex_token and st.session_state.plex_url:
            if st.button("🔄 Refresh Data", key="header_refresh_minimal", disabled=refresh_state["running"]):
                plex.start_background_refresh(st.session_state.plex_url, st.session_state.plex_token)
                st.rerun()

    with col_r3:
        if st.button("🧹 Reset UI", key="reset_ui_minimal", help="Clear stuck elements"):
            plex.clear_refresh_done()
            st.rerun()

    st.divider()
    
    # Tab Navigation with URL Persistence
    tab_options = ["Library Audit", "Series Auditor", "📋 Audit Log", "⚙️ Settings"]
    
    # Default to Settings tab if Plex isn't configured yet
    not_configured = not st.session_state.plex_token or not st.session_state.plex_url
    default_tab = "⚙️ Settings" if not_configured else "Library Audit"
    query_tab = st.query_params.get("tab", default_tab)
    if query_tab not in tab_options:
        query_tab = default_tab
    
    # Use index to set the default value based on URL
    selected_tab = st.radio(
        "Navigation", 
        options=tab_options, 
        index=tab_options.index(query_tab), 
        horizontal=True, 
        label_visibility="collapsed",
        key="main_navigation"
    )
    
    # Update query params whenever the tab changes
    st.query_params["tab"] = selected_tab
    
    # Render Content based on selection
    if selected_tab == "Library Audit":
        if not st.session_state.plex_token or not st.session_state.plex_url:
            st.warning("### Welcome! \nPlease head over to the **⚙️ Settings** tab to connect your Plex server and begin.")
        else:
            render_library_audit()
    
    elif selected_tab == "Series Auditor":
        if not st.session_state.plex_token or not st.session_state.plex_url:
            st.warning("Please connect to your Plex server in the **⚙️ Settings** tab to use the Series Auditor.")
        else:
            render_series_auditor()
        
    elif selected_tab == "📋 Audit Log":
        render_audit_log()

    elif selected_tab == "⚙️ Settings":
        render_settings()

    # Polling: keep re-running while a background refresh is active
    if refresh_state["running"]:
        time.sleep(0.5)
        st.rerun()
    elif refresh_state["done"] and not refresh_state["error"]:
        # Brief pause so the user sees the completion message, then reload with fresh data
        time.sleep(1.5)
        plex.clear_refresh_done()
        st.rerun()

def render_settings():
    st.header("⚙️ App Settings")
    
    col_conn, col_tmdb = st.columns(2)
    
    with col_conn:
        st.subheader("Plex Connection")
        if st.session_state.plex_token and st.session_state.plex_url:
            st.caption(f"🟢 Connected — `{st.session_state.plex_url}`")
        else:
            st.caption("🔴 Not connected")

        # Connection Mode selection
        login_mode = st.radio("Connection Mode", ["OAuth (Plex.tv)", "Manual (Direct IP)"], horizontal=True)

        if login_mode == "OAuth (Plex.tv)":
            if not st.session_state.plex_token:
                if st.session_state.pin_login is None:
                    st.info("Authenticate with your Plex.tv account to automatically discover servers.")
                    if st.button("Login with Plex", key="settings_login_btn"):
                        try:
                            pin_login, pin_id = plex.start_plex_auth()
                            st.session_state.pin_login = pin_login
                            st.session_state.pin_id = pin_id
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to start Plex login: {e}")
                else:
                    st.markdown(f"### [Click here to authorize on Plex.tv]({st.session_state.pin_login.oauthUrl()})")
                    st.caption("After authorizing, this page will detect it automatically.")
                    if st.button("Cancel", key="cancel_login_btn"):
                        st.session_state.pin_login = None
                        st.session_state.pin_id = None
                        st.rerun()

                    # Auto-poll for authorization every 3 seconds
                    try:
                        import time
                        time.sleep(3)
                        token = plex.check_plex_pin(st.session_state.pin_id)
                        if token:
                            st.session_state.plex_token = token
                            plex.save_setting('plex_token', token)
                            st.session_state.pin_login = None
                            st.session_state.pin_id = None
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error checking authorization: {e}")
            else:
                st.success("Authenticated with Plex.tv")
                if not st.session_state.plex_url:
                    try:
                        with st.spinner("Discovering servers..."):
                            account = plex.get_plex_account(st.session_state.plex_token)
                            servers = [r for r in account.resources() if 'server' in r.provides and r.owned]

                        if not servers:
                            st.error("No owned Plex servers found on this account.")
                        elif len(servers) == 1:
                            with st.spinner(f"Connecting to {servers[0].name}..."):
                                server = servers[0].connect()
                                st.session_state.plex_url = server._baseurl
                                plex.save_setting('plex_url', st.session_state.plex_url)
                            st.rerun()
                        else:
                            server_names = [s.name for s in servers]
                            selected_server = st.selectbox("Select Plex Server", server_names)
                            if st.button("Connect"):
                                server = account.resource(selected_server).connect()
                                st.session_state.plex_url = server._baseurl
                                plex.save_setting('plex_url', st.session_state.plex_url)
                                st.rerun()
                    except Exception as e:
                        st.error(f"Error discovering servers: {e}")
                else:
                    st.caption(f"🟢 Connected — `{st.session_state.plex_url}`")
                    if st.button("Change Server / Re-discover"):
                        st.session_state.plex_url = None
                        plex.save_setting('plex_url', None)
                        st.rerun()
            
            # Show logout button regardless of OAuth/Manual if connected
            if st.session_state.plex_token:
                st.divider()
                if st.button("🛑 Logout / Disconnect", use_container_width=True):
                    st.session_state.plex_token = None
                    st.session_state.plex_url = None
                    plex.save_setting('plex_token', None)
                    plex.save_setting('plex_url', None)
                    st.rerun()
        else:
            # Manual Mode
            manual_url = st.text_input("Server URL", value=st.session_state.plex_url or "http://192.168.1.X:32400")
            manual_token = st.text_input("Plex Token", value=st.session_state.plex_token or "", type="password")
            if st.button("Save Manual Connection"):
                if not manual_url.startswith(("http://", "https://")):
                    st.error("Server URL must start with http:// or https://")
                elif not manual_token.strip():
                    st.error("Plex Token cannot be empty.")
                else:
                    st.session_state.plex_url = manual_url
                    st.session_state.plex_token = manual_token.strip()
                    plex.save_setting('plex_url', manual_url)
                    plex.save_setting('plex_token', manual_token.strip())
                    st.success("Connection saved!")
                    st.rerun()
            
            if st.session_state.plex_token:
                st.divider()
                if st.button("🛑 Logout / Disconnect", key="manual_logout", use_container_width=True):
                    st.session_state.plex_token = None
                    st.session_state.plex_url = None
                    plex.save_setting('plex_token', None)
                    plex.save_setting('plex_url', None)
                    st.rerun()

    with col_tmdb:
        st.subheader("TMDB Integration")
        st.write("Required for Series Auditor to identify collections and missing items.")
        curr_tmdb = st.text_input("TMDB API Key", value=st.session_state.tmdb_api_key or "", type="password")
        col_test_tmdb, col_save_tmdb = st.columns(2)
        with col_test_tmdb:
            if st.button("🧪 Test TMDB Key", use_container_width=True):
                if not curr_tmdb:
                    st.warning("Enter an API key first.")
                else:
                    with st.spinner("Testing..."):
                        ok, msg = plex.test_tmdb_connection(curr_tmdb)
                    if ok:
                        st.success(f"Connected — {msg}")
                    else:
                        st.error(msg)
        with col_save_tmdb:
            if st.button("💾 Save TMDB Key", use_container_width=True):
                st.session_state.tmdb_api_key = curr_tmdb
                plex.save_setting('tmdb_api_key', curr_tmdb)
                st.success("TMDB Key saved!")
        if st.session_state.tmdb_api_key:
            ok, msg = plex.test_tmdb_connection(st.session_state.tmdb_api_key)
            if ok:
                st.caption("🟢 TMDB connected")
            else:
                st.caption(f"🔴 TMDB not connected — {msg}")

    st.divider()
    st.subheader("Media Automation (Radarr / Sonarr)")
    st.write("Enable direct monitoring and requests from the Series Auditor.")
    
    col_radarr, col_sonarr = st.columns(2)
    
    # Helper to render automation section
    def render_auto_config(service_name, configs):
        st.markdown(f"#### {service_name.capitalize()}")
        url = st.text_input(f"{service_name.capitalize()} URL", value=plex.get_setting(f"{service_name}_url") or "", placeholder="http://192.168.1.50:7878")
        key = st.text_input(f"{service_name.capitalize()} API Key", value=plex.get_setting(f"{service_name}_api_key") or "", type="password")

        col_test, col_save = st.columns(2)

        with col_test:
            if st.button(f"🧪 Test {service_name.capitalize()}", use_container_width=True):
                if not url or not key:
                    st.warning("Enter URL and Key first.")
                else:
                    with st.spinner("Testing..."):
                        items, err = plex.test_automation_connection(service_name, url=url, api_key=key)
                        if err:
                            st.error(err)
                        elif items:
                            st.success(f"Connected! Found {len(items)} items.")
                            if service_name == "radarr": get_cached_radarr_items.clear()
                            else: get_cached_sonarr_items.clear()
                        else:
                            st.warning("Connected but library appears empty.")

        with col_save:
            if st.button(f"💾 Save {service_name.capitalize()}", use_container_width=True):
                plex.save_setting(f"{service_name}_url", url)
                plex.save_setting(f"{service_name}_api_key", key)
                get_cached_radarr_configs.clear()
                get_cached_sonarr_configs.clear()
                st.success("Core settings saved!")

        saved_url = plex.get_setting(f"{service_name}_url")
        saved_key = plex.get_setting(f"{service_name}_api_key")
        if saved_url and saved_key:
            items = service_name == "radarr" and get_cached_radarr_items() or get_cached_sonarr_items()
            if items:
                st.caption(f"🟢 Connected — {len(items)} items in library")
            else:
                st.caption(f"🔴 Not connected or library is empty — use Test to diagnose")
        else:
            st.caption("⚪ Not configured")

        if configs["profiles"] and configs["folders"]:
            st.divider()
            profiles = configs["profiles"]
            folders = configs["folders"]
            
            curr_prof = plex.get_setting(f"{service_name}_profile")
            curr_fold = plex.get_setting(f"{service_name}_folder")
            
            prof_names = [p["name"] for p in profiles]
            fold_paths = [f["path"] for f in folders]
            
            p_idx = prof_names.index(next((p["name"] for p in profiles if str(p["id"]) == str(curr_prof)), prof_names[0])) if curr_prof else 0
            f_idx = fold_paths.index(curr_fold) if curr_fold in fold_paths else 0
            
            sel_prof = st.selectbox(f"Default Profile", options=prof_names, index=p_idx, key=f"{service_name}_prof_sel")
            sel_fold = st.selectbox(f"Default Root Path", options=fold_paths, index=f_idx, key=f"{service_name}_fold_sel")
            
            if st.button(f"Update Defaults", key=f"{service_name}_save_defaults"):
                prof_id = next(p["id"] for p in profiles if p["name"] == sel_prof)
                plex.save_setting(f"{service_name}_profile", str(prof_id))
                plex.save_setting(f"{service_name}_folder", sel_fold)
                st.success("Defaults updated!")

    with col_radarr:
        render_auto_config("radarr", get_cached_radarr_configs())

    with col_sonarr:
        render_auto_config("sonarr", get_cached_sonarr_configs())

    st.divider()
    st.subheader("Tautulli Integration")
    st.write("Enables per-user watch history filtering in the Library Audit rule builder.")
    col_taut1, col_taut2 = st.columns(2)
    with col_taut1:
        tautulli_url = st.text_input("Tautulli URL", value=plex.get_setting("tautulli_url") or "",
                                     placeholder="http://192.168.1.50:8181")
    with col_taut2:
        tautulli_key = st.text_input("Tautulli API Key", value=plex.get_setting("tautulli_api_key") or "",
                                     type="password")
    col_tt, col_ts = st.columns(2)
    with col_tt:
        if st.button("🧪 Test Tautulli", use_container_width=True):
            if not tautulli_url or not tautulli_key:
                st.warning("Enter URL and API key first.")
            else:
                with st.spinner("Testing..."):
                    if plex.test_tautulli(tautulli_url, tautulli_key):
                        users = plex.get_tautulli_users(tautulli_url, tautulli_key)
                        st.success(f"Connected! Found {len(users)} users.")
                    else:
                        st.error("Connection failed. Check URL and API key.")
    with col_ts:
        if st.button("💾 Save Tautulli", use_container_width=True):
            plex.save_setting("tautulli_url", tautulli_url)
            plex.save_setting("tautulli_api_key", tautulli_key)
            get_cached_tautulli_users.clear()
            st.success("Tautulli settings saved!")

    saved_taut_url = plex.get_setting("tautulli_url")
    saved_taut_key = plex.get_setting("tautulli_api_key")
    if saved_taut_url and saved_taut_key:
        users = get_cached_tautulli_users()
        if users:
            st.caption(f"🟢 Connected — {len(users)} users available")
        else:
            st.caption("🔴 Not connected or no users found — use Test to diagnose")
    else:
        st.caption("⚪ Not configured")

    st.divider()
    st.subheader("Data & Persistence")
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        st.write(f"**Current Database:** `{os.path.abspath(plex.DB_PATH)}`")
        new_db_path = st.text_input("Change Database Path", value=plex.DB_PATH, placeholder="cache.sqlite",
                                    help="Saved to .env as CACHE_DB_PATH. Restart required to take effect.")
        if st.button("💾 Save Database Path"):
            plex.save_setting('cache_db_path', new_db_path.strip())
            st.warning(f"Path saved. Restart the app to switch to `{new_db_path.strip()}`.")

    with col_d2:
        st.write("**Wipe Cached Data**")
        st.caption("Clears the library and series caches. Settings are preserved.")
        if st.button("🗑️ Wipe Local Cache", type="secondary", use_container_width=True):
            confirm_wipe_cache_dialog()

def render_library_audit():
    _cache_key = plex.get_setting('last_refreshed_at') or 'none'
    df = _cached_load_library(_cache_key)
    if df.empty:
        st.info("No data cached. Please click 'Refresh Data from Server' in the sidebar.")
        return

    # Quick stats row (full unfiltered library)
    total_gb = df['size_bytes'].sum() / (1024**3)
    lib_size = f"{total_gb:.1f} GB" if total_gb < 1000 else f"{total_gb/1024:.2f} TB"
    never_watched = len(df[df['last_watched_at'].isna() & (df['viewed_leaf_count'] == 0)])
    mc1, mc2, mc3, mc4 = st.columns(4)
    with mc1:
        st.metric("Movies", f"{len(df[df['type'] == 'movie']):,}")
    with mc2:
        st.metric("TV Shows", f"{len(df[df['type'] == 'show']):,}")
    with mc3:
        st.metric("Total Size", lib_size)
    with mc4:
        st.metric("Never Watched", f"{never_watched:,}")

    st.divider()

    # Filters Initialization from Query Params
    params = st.query_params

    # Handle list-based params (Type and Library)
    if "type" in params:
        q_types = params.get_all("type")
        default_lib_type = [t for t in q_types if t in ["movie", "show"]]
    else:
        default_lib_type = ["movie", "show"]

    all_libs = df['library'].unique().tolist()
    if "lib" in params:
        q_libs = params.get_all("lib")
        default_libraries = [l for l in q_libs if l in all_libs]
    else:
        default_libraries = all_libs

    default_search = params.get("q", "")
    default_coll = params.get("coll", "")

    # Resolution options
    _res_priority = {'4k': 4, '1080': 3, '720': 2, 'sd': 1}
    all_resolutions = sorted(
        df['resolution'].fillna('').replace('', 'Unknown').unique().tolist(),
        key=lambda x: _res_priority.get(x.lower(), 0),
        reverse=True
    )
    if "res" in params:
        q_res = params.get_all("res")
        default_res = [r for r in q_res if r in all_resolutions] or all_resolutions
    else:
        default_res = all_resolutions

    all_ratings = sorted(df['content_rating'].fillna('NR').replace('', 'NR').unique().tolist())
    if "rating" in params:
        default_rating = [r for r in params.get_all("rating") if r in all_ratings] or all_ratings
    else:
        default_rating = all_ratings

    # Row 1: content type / library
    col1, col2 = st.columns(2)
    with col1:
        lib_type = _smart_multiselect("Content Type", options=["movie", "show"], default=default_lib_type, key="lib_type")
    with col2:
        libraries = _smart_multiselect("Libraries", options=all_libs, default=default_libraries, key="libraries")

    # Row 2: resolution / content rating
    col_s1, col_s2 = st.columns(2)
    with col_s1:
        res_filter = _smart_multiselect("Resolution", options=all_resolutions, default=default_res, key="resolution")
    with col_s2:
        rating_filter = _smart_multiselect("Content Rating", options=all_ratings, default=default_rating, key="content_rating")

    # Row 3: text searches + res dedup + upgrade filter
    col_t1, col_t2, col_t3, col_t4 = st.columns([2, 2, 1, 1])
    with col_t1:
        title_search = st.text_input("Filter by Title", value=default_search, placeholder="Filter by name...")
    with col_t2:
        collection_search = st.text_input("Filter by Collection", value=default_coll, placeholder="e.g. Marvel, Star Wars…")
    with col_t3:
        res_dupes_only = st.checkbox("Lower-res dupes only", help="Show items where you own the same content in a higher resolution")
    with col_t4:
        _radarr_configured = bool(plex.get_setting("radarr_url") and plex.get_setting("radarr_api_key") and plex.get_setting("radarr_profile"))
        _sonarr_configured = bool(plex.get_setting("sonarr_url") and plex.get_setting("sonarr_api_key") and plex.get_setting("sonarr_profile"))
        upgradeable_only = st.checkbox(
            "Upgradeable only",
            help="Items where your Radarr/Sonarr profile allows a higher resolution than what you own",
            disabled=not (_radarr_configured or _sonarr_configured)
        )

    # Update Query Params for Persistence
    st.query_params["type"] = lib_type
    st.query_params["lib"] = libraries
    st.query_params["q"] = title_search
    st.query_params["res"] = res_filter
    st.query_params["rating"] = rating_filter
    st.query_params["coll"] = collection_search

    # --- Preset + Rule Builder ---
    st.divider()
    preset_col, rule_col = st.columns([1, 3])
    with preset_col:
        preset_names = plex.list_presets()
        if preset_names:
            selected_preset = st.selectbox("Load Preset", ["—"] + preset_names, key="preset_load_sel")
            p_load, p_del = st.columns(2)
            with p_load:
                if st.button("Load", use_container_width=True, key="preset_load_btn"):
                    if selected_preset != "—":
                        st.session_state.filter_rules = plex.load_preset(selected_preset)
                        st.rerun()
            with p_del:
                if st.button("Delete", use_container_width=True, key="preset_del_btn"):
                    if selected_preset != "—":
                        plex.delete_preset(selected_preset)
                        st.rerun()
        new_preset_name = st.text_input("Save current rules as…", placeholder="Preset name", key="preset_name_input")
        if st.button("💾 Save Preset", use_container_width=True, key="preset_save_btn"):
            if new_preset_name.strip():
                plex.save_preset(new_preset_name.strip(), st.session_state.filter_rules)
                st.success(f'Saved \"{new_preset_name.strip()}\"')

    with rule_col:
        st.markdown("**Filter Rules**")
        active_rules = render_rule_builder(all_resolutions)

    # Apply Filters
    filtered_df = df[df['type'].isin(lib_type) & df['library'].isin(libraries)]

    if title_search:
        filtered_df = filtered_df[filtered_df['title'].str.contains(title_search, case=False, na=False)]

    # Resolution filter
    res_filter_vals = ['' if r == 'Unknown' else r for r in res_filter]
    filtered_df = filtered_df[filtered_df['resolution'].fillna('').isin(res_filter_vals)]

    # Content rating filter
    if set(rating_filter) != set(all_ratings):
        rating_vals = ['' if r == 'NR' else r for r in rating_filter]
        filtered_df = filtered_df[filtered_df['content_rating'].fillna('').isin(rating_vals)]

    # Collection text search
    if collection_search:
        filtered_df = filtered_df[filtered_df['collections'].fillna('').str.contains(collection_search, case=False, na=False)]

    # Resolution duplicates filter
    if res_dupes_only:
        dupe_df = plex.get_resolution_duplicates()
        if not dupe_df.empty:
            filtered_df = filtered_df[filtered_df['rating_key'].isin(dupe_df['rating_key'])]

    # Upgradeable filter: items whose resolution is below the Radarr/Sonarr profile max
    _PLEX_RES_PX = {'sd': 480, '720': 720, '1080': 1080, '4k': 2160}
    if upgradeable_only and (_radarr_configured or _sonarr_configured):
        _upgrade_mask = pd.Series(False, index=filtered_df.index)
        _res_px = filtered_df['resolution'].fillna('').str.lower().map(lambda r: _PLEX_RES_PX.get(r, 0))
        if _radarr_configured:
            _radarr_max = get_radarr_profile_max_res()
            if _radarr_max > 0:
                _upgrade_mask |= (filtered_df['type'] == 'movie') & (_res_px < _radarr_max)
        if _sonarr_configured:
            _sonarr_max = get_sonarr_profile_max_res()
            if _sonarr_max > 0:
                _upgrade_mask |= (filtered_df['type'] == 'show') & (_res_px < _sonarr_max)
        filtered_df = filtered_df[_upgrade_mask]

    # Apply rule builder conditions (AND logic)
    for rule in active_rules:
        filtered_df = _apply_rule(filtered_df, rule, all_resolutions)

    # Calculate Total Size
    total_size_bytes = filtered_df['size_bytes'].sum()
    total_size_gb = total_size_bytes / (1024**3)
    size_str = f"{total_size_gb:.2f} GB" if total_size_gb < 1000 else f"{total_size_gb/1024:.2f} TB"

    col_hdr, col_export = st.columns([4, 1], vertical_alignment="bottom")
    with col_hdr:
        st.subheader(f"Found {len(filtered_df)} items ({size_str})")
    with col_export:
        csv_data = filtered_df[['title', 'type', 'library', 'release_date', 'added_at',
                                 'last_watched_at', 'viewed_leaf_count', 'leaf_count',
                                 'size_bytes', 'resolution', 'content_rating', 'collections']].copy()
        csv_data['release_date'] = csv_data['release_date'].dt.strftime('%Y-%m-%d')
        csv_data['added_at'] = csv_data['added_at'].dt.strftime('%Y-%m-%d')
        csv_data['last_watched_at'] = csv_data['last_watched_at'].dt.strftime('%Y-%m-%d')
        st.download_button(
            label="📥 Export CSV",
            data=csv_data.to_csv(index=False),
            file_name=f"plex_library_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True
        )
    st.caption("💡 Select one or more rows below to delete them. Click a column header to sort.")

    # Prepare DataFrame for Display
    display_df = filtered_df.copy()
    
    # Format dates to string for cleaner display in table
    display_df['First Added'] = display_df['added_at'].dt.strftime('%Y-%m-%d').fillna('')
    display_df['Latest Added'] = display_df.apply(
        lambda x: x['latest_added_at'].strftime('%Y-%m-%d') if (x['type'] == 'show' and pd.notnull(x['latest_added_at'])) else "", 
        axis=1
    )
    display_df['Last Watched'] = display_df['last_watched_at'].dt.strftime('%Y-%m-%d').fillna('')
    display_df['Released'] = display_df['release_date'].dt.strftime('%Y-%m-%d').fillna('')
    
    # Create the x / y progress column (only for shows)
    display_df['Watched'] = display_df.apply(
        lambda x: f"{int(x['viewed_leaf_count'])} / {int(x['leaf_count'])}" if x['type'] == 'show' else "", 
        axis=1
    )
    
    # Format Size to GB
    display_df['Size (GB)'] = (display_df['size_bytes'] / (1024**3)).round(2)
    
    # Create the Plex Web Link
    # https://app.plex.tv/desktop/#!/server/{machineIdentifier}/details?key=%2Flibrary%2Fmetadata%2F{ratingKey}
    display_df['Plex Link'] = display_df.apply(
        lambda x: f"https://app.plex.tv/desktop/#!/server/{x['server_id']}/details?key=%2Flibrary%2Fmetadata%2F{x['rating_key']}", 
        axis=1
    )

    # Rename and select columns for user display
    display_df = display_df.rename(columns={
        'title': 'Title',
        'type': 'Type',
        'library': 'Library',
        'collections': 'Collections',
        'resolution': 'Res',
        'content_rating': 'Rating',
        'series_status': 'Status'
    })

    # Final column selection
    cols_to_show = ['Plex Link', 'Title', 'Released', 'Rating', 'Res', 'Type', 'Status', 'Library', 'Collections', 'First Added', 'Latest Added', 'Last Watched', 'Watched', 'Size (GB)']

    # Display as an interactive table with multi-row selection
    selection = st.dataframe(
        display_df[cols_to_show + ['rating_key']],
        width="stretch",
        hide_index=True,
        column_config={
            "Plex Link": st.column_config.LinkColumn("View in Plex", display_text="Open ↗", width="small"),
            "Title": st.column_config.TextColumn("Title", width="large"),
            "Released": st.column_config.TextColumn("Released"),
            "Res": st.column_config.TextColumn("Res", width="small"),
            "Collections": st.column_config.TextColumn("Collections", width="medium"),
            "Status": st.column_config.TextColumn("Status", width="small"),
            "First Added": st.column_config.TextColumn("First Added"),
            "Latest Added": st.column_config.TextColumn("Latest Added"),
            "Last Watched": st.column_config.TextColumn("Last Watched"),
            "Watched": st.column_config.TextColumn("Progress (Ep/Total)"),
            "Size (GB)": st.column_config.NumberColumn("Size (GB)", format="%.2f GB"),
            "rating_key": None
        },
        on_select="rerun",
        selection_mode="multi-row",
        key="audit_table"
    )

    # Handle selection trigger
    if selection.selection.rows:
        try:
            # Filter indices to ensure they are within the current display_df bounds
            # This prevents IndexError if the dataframe size changes between reruns
            selected_indices = [i for i in selection.selection.rows if i < len(display_df)]

            if selected_indices:
                # Get selected items
                selected_items = display_df.iloc[selected_indices]

                # Determine which selected items can be upgraded via Radarr (movies) or Sonarr (shows)
                _radarr_max = get_radarr_profile_max_res() if _radarr_configured else 0
                _sonarr_max = get_sonarr_profile_max_res() if _sonarr_configured else 0
                upgradeable_movies = []
                upgradeable_shows = []
                for _, row in selected_items.iterrows():
                    res = str(row.get('Res', '') or '').lower()
                    res_px = _PLEX_RES_PX.get(res, 0)
                    if row.get('Type') == 'movie' and _radarr_configured and _radarr_max > 0:
                        tmdb_id = row.get('tmdb_id')
                        if res_px < _radarr_max and tmdb_id and not pd.isna(tmdb_id):
                            upgradeable_movies.append({'title': row['Title'], 'tmdb_id': int(tmdb_id)})
                    elif row.get('Type') == 'show' and _sonarr_configured and _sonarr_max > 0:
                        tvdb_id = row.get('tvdb_id')
                        if res_px < _sonarr_max and tvdb_id and not pd.isna(tvdb_id):
                            upgradeable_shows.append({'title': row['Title'], 'tvdb_id': int(tvdb_id)})
                upgradeable_items = upgradeable_movies + upgradeable_shows

                st.divider()
                action_cols = st.columns([1, 1, 2] if upgradeable_items else [1, 3])
                with action_cols[0]:
                    if st.button(f"🗑️ Delete {len(selected_items)} Selected Items", type="primary", use_container_width=True):
                        st.session_state.pending_deletes = [
                            {"rk": row['rating_key'], "title": row['Title'],
                             "type": row['Type'], "library": row['Library'],
                             "size_bytes": int(filtered_df.loc[filtered_df['rating_key'] == row['rating_key'], 'size_bytes'].iloc[0] or 0)}
                            for _, row in selected_items.iterrows()
                        ]
                        st.rerun()
                if upgradeable_items:
                    svc_label = "Radarr/Sonarr" if (upgradeable_movies and upgradeable_shows) else ("Radarr" if upgradeable_movies else "Sonarr")
                    with action_cols[1]:
                        if st.button(f"⬆️ Upgrade {len(upgradeable_items)} via {svc_label}", use_container_width=True):
                            _radarr_items = get_cached_radarr_items()
                            _sonarr_items = get_cached_sonarr_items()
                            ok_count = 0
                            for item in upgradeable_movies:
                                auto_status = plex.check_automation_status(item['tmdb_id'], "radarr", _radarr_items)
                                if auto_status in ("Downloaded", "Monitored", "Tracked (Not Monitored)"):
                                    ok, _ = plex.trigger_radarr_search(item['tmdb_id'], _radarr_items)
                                else:
                                    _r_profile = plex.get_setting("radarr_profile")
                                    _r_folder = plex.get_setting("radarr_folder")
                                    ok, _ = plex.add_to_automation(item['tmdb_id'], "radarr", _r_profile, _r_folder, title=item['title']) if (_r_profile and _r_folder) else (False, "")
                                if ok:
                                    ok_count += 1
                            for item in upgradeable_shows:
                                auto_status = plex.check_automation_status(item['tvdb_id'], "sonarr", _sonarr_items)
                                if auto_status in ("Downloaded", "Monitored", "Tracked (Not Monitored)"):
                                    ok, _ = plex.trigger_sonarr_search(item['tvdb_id'], _sonarr_items)
                                else:
                                    _s_profile = plex.get_setting("sonarr_profile")
                                    _s_folder = plex.get_setting("sonarr_folder")
                                    ok, _ = plex.add_to_automation(item['tvdb_id'], "sonarr", _s_profile, _s_folder, title=item['title']) if (_s_profile and _s_folder) else (False, "")
                                if ok:
                                    ok_count += 1
                            if ok_count:
                                get_cached_radarr_items.clear()
                                get_cached_sonarr_items.clear()
                                st.success(f"Upgrade initiated for {ok_count}/{len(upgradeable_items)} item(s)")
                                time.sleep(1)
                                st.rerun()
                            else:
                                st.error(f"No upgrades initiated — set {svc_label} defaults in Settings")
        except Exception:
            # Silently handle indexing errors due to stale widget state
            pass

@st.cache_data(ttl=300)
def get_cached_radarr_items():
    return plex.get_automation_items("radarr")

@st.cache_data(ttl=300)
def get_cached_sonarr_items():
    return plex.get_automation_items("sonarr")

@st.cache_data(ttl=300)
def get_cached_radarr_configs():
    return plex.get_automation_configs("radarr")

@st.cache_data(ttl=300)
def get_cached_sonarr_configs():
    return plex.get_automation_configs("sonarr")

@st.cache_data(ttl=300)
def get_radarr_profile_max_res():
    profile_id = plex.get_setting("radarr_profile")
    return plex.get_radarr_profile_max_resolution(profile_id)

@st.cache_data(ttl=300)
def get_sonarr_profile_max_res():
    profile_id = plex.get_setting("sonarr_profile")
    return plex.get_sonarr_profile_max_resolution(profile_id)

@st.cache_data
def _cached_load_library(cache_key: str):
    """Cache library data keyed on last_refreshed_at so it auto-invalidates after each refresh."""
    return plex.load_cached_data()

def render_series_auditor():
    if not st.session_state.tmdb_api_key:
        st.warning("Please enter a TMDB API Key in the Settings tab to use the Series Auditor.")
        return

    radarr_items = get_cached_radarr_items()
    sonarr_items = get_cached_sonarr_items()

    with st.container(border=True):
        st.markdown("**Scan Library**")
        st.caption("Enrich metadata from TMDB to identify collection gaps.")
        scan_clicked = st.button("🔍 Scan for Missing Series Items", use_container_width=True)

    if scan_clicked:
        progress_bar = st.progress(0)
        status_text = st.empty()

        def update_progress(msg, progress):
            status_text.text(msg)
            progress_bar.progress(progress)

        with st.spinner("Enriching library with TMDB data..."):
            plex.enrich_series_data(st.session_state.tmdb_api_key, progress_callback=update_progress)
            _cached_load_library.clear()
            st.success("Series data updated!")
            time.sleep(1)
            st.rerun()

    audit_data = plex.get_series_audit_data()
    
    if not audit_data:
        st.info("No series identified yet. Click 'Scan for Missing Series Items' to begin.")
        return

    # Filter for incomplete only
    col_f1, col_f2, col_f3, col_f4 = st.columns(4)
    with col_f1:
        incomplete_only = st.checkbox("Show Incomplete Series Only", value=True)
    with col_f2:
        exclude_future = st.checkbox("Exclude Future Releases", value=True)
    with col_f3:
        show_hidden = st.checkbox("Show Hidden/Ignored Series", value=False)
    with col_f4:
        exclude_monitored = st.checkbox("Exclude Fully Monitored", value=True, help="Hide series where all missing parts are already Monitored or Downloaded in Radarr/Sonarr")

    col_s1, col_s2 = st.columns([2, 1])
    with col_s1:
        series_search = st.text_input("Search Series", placeholder="Filter by name…", label_visibility="collapsed")
    with col_s2:
        series_sort_options = ["Name (A-Z)", "Name (Z-A)", "Most Missing", "Least Missing"]
        series_sort_by = st.selectbox("Sort Series By", options=series_sort_options)
    
    today = datetime.now().strftime('%Y-%m-%d')

    processed_audit_data = []
    for series in audit_data:
        # Hide logic
        if not show_hidden and series['is_hidden']:
            continue
        if show_hidden and not series['is_hidden']:
            continue
            
        parts = series['parts']
        if exclude_future:
            filtered_parts = []
            for p in parts:
                if p['is_owned']:
                    filtered_parts.append(p)
                    continue
                
                rd = p.get('release_date', '')
                status = p.get('status', 'Released')
                
                if not rd: continue
                
                is_future_date = False
                if len(rd) == 4:
                    if rd > today[:4]: is_future_date = True
                elif rd > today:
                    is_future_date = True
                
                if is_future_date: continue
                
                unreleased_statuses = ['Planned', 'In Production', 'Post Production', 'Rumored']
                if status in unreleased_statuses: continue
                
                filtered_parts.append(p)
            parts = filtered_parts
        
        owned_count = len([p for p in parts if p['is_owned']])
        total_relevant = len(parts)
        has_missing = any(not p['is_owned'] for p in parts)
        
        if incomplete_only and not has_missing:
            continue

        if exclude_monitored and has_missing:
            is_movie_series = series['collection_id'].startswith("movie_")
            service = "radarr" if is_movie_series else "sonarr"
            all_auto_items = radarr_items if is_movie_series else sonarr_items
            missing_parts = [p for p in parts if not p['is_owned']]
            fully_covered = all(
                plex.check_automation_status(
                    p['tmdb_id'] if is_movie_series else series.get('tvdb_id'),
                    service,
                    all_auto_items
                ) in ("Monitored", "Downloaded")
                for p in missing_parts
                if (p['tmdb_id'] if is_movie_series else series.get('tvdb_id'))
            )
            unchecked = any(
                not (p['tmdb_id'] if is_movie_series else series.get('tvdb_id'))
                for p in missing_parts
            )
            if fully_covered and not unchecked:
                continue

        processed_audit_data.append({
            **series,
            'parts': parts,
            'owned_count': owned_count,
            'total_relevant': total_relevant,
            'missing_count': total_relevant - owned_count
        })

    # Apply series name search
    if series_search:
        processed_audit_data = [s for s in processed_audit_data if series_search.lower() in s['name'].lower()]

    # Apply Sorting
    if series_sort_by == "Name (A-Z)":
        processed_audit_data.sort(key=lambda x: x['name'])
    elif series_sort_by == "Name (Z-A)":
        processed_audit_data.sort(key=lambda x: x['name'], reverse=True)
    elif series_sort_by == "Most Missing":
        processed_audit_data.sort(key=lambda x: (-x['missing_count'], x['name']))
    elif series_sort_by == "Least Missing":
        processed_audit_data.sort(key=lambda x: (x['missing_count'], x['name']))

    for series in processed_audit_data:
        # Handle potential NaN status from pandas
        s_val = series.get('status', 'Unknown')
        if pd.isna(s_val): s_val = 'Unknown'
        status_label = f" ({s_val})" if s_val != 'Unknown' else ""
        
        hidden_tag = " [HIDDEN]" if series['is_hidden'] else ""
        
        with st.expander(f"**{series['name']}**{status_label}{hidden_tag} ({series['owned_count']}/{series['total_relevant']})"):
            st.progress(series['owned_count'] / series['total_relevant'] if series['total_relevant'] > 0 else 1)
            
            # Action Buttons
            col_act1, col_act2, col_act3 = st.columns([1, 1, 1])
            with col_act1:
                if series['is_hidden']:
                    if st.button(f"👁️ Show/Track Series", key=f"unhide_{series['collection_id']}"):
                        plex.toggle_series_visibility(series['collection_id'], hide=False)
                        st.rerun()
                else:
                    if st.button(f"🚫 Hide/Don't Track", key=f"hide_{series['collection_id']}"):
                        plex.toggle_series_visibility(series['collection_id'], hide=True)
                        st.rerun()
            with col_act2:
                if st.button("🔄 Refresh Series Info", key=f"ref_{series['collection_id']}", help="Force update TMDB info for this series"):
                    with st.spinner("Refreshing..."):
                        plex.enrich_series_data(st.session_state.tmdb_api_key, target_collection_id=series['collection_id'])
                        st.rerun()
            with col_act3:
                is_movie_series = series['collection_id'].startswith("movie_")
                _svc = "radarr" if is_movie_series else "sonarr"
                _missing = [p for p in series['parts'] if not p['is_owned'] and p.get('tmdb_id')]
                _svc_ok = plex.get_setting(f"{_svc}_url") and plex.get_setting(f"{_svc}_api_key")
                if _missing and _svc_ok:
                    if st.button(f"➕ Add All ({len(_missing)})", key=f"add_all_{series['collection_id']}",
                                 help=f"Add all missing items to {_svc.capitalize()}"):
                        _profile = plex.get_setting(f"{_svc}_profile")
                        _folder = plex.get_setting(f"{_svc}_folder")
                        if not _profile or not _folder:
                            st.error(f"Set {_svc.capitalize()} defaults in Settings first.")
                        else:
                            _ok_count = 0
                            for _part in _missing:
                                _lid = _part['tmdb_id'] if is_movie_series else series.get('tvdb_id')
                                if _lid and not pd.isna(_lid):
                                    _ok, _ = plex.add_to_automation(_lid, _svc, _profile, _folder, title=_part['title'])
                                    if _ok:
                                        _ok_count += 1
                            if _ok_count:
                                get_cached_radarr_items.clear()
                                get_cached_sonarr_items.clear()
                                st.success(f"Added {_ok_count}/{len(_missing)} to {_svc.capitalize()}")
                                time.sleep(1)
                                st.rerun()
                            else:
                                st.error("No items added — check Settings or Radarr/Sonarr logs.")
            
            st.divider()
            for item in series['parts']:
                status_icon = "✅" if item['is_owned'] else "❌"
                year = item['release_date'][:4] if item['release_date'] else 'N/A'
                
                # Column Split: [Title/Links, Automation Status/Add, Delete]
                col_item_text, col_item_auto, col_item_del = st.columns([5, 2, 1], vertical_alignment="center")
                
                with col_item_text:
                    status = item.get('status', 'Released')
                    future_statuses = ['Planned', 'In Production', 'Post Production', 'Rumored']
                    status_text = f" [{status}]" if status in future_statuses else ""
                    
                    watch_status = ""
                    if item['is_owned']:
                        if item.get('type') == 'show':
                            watch_status = f" 👁️ {int(item.get('viewed_leaf_count', 0))}/{int(item.get('leaf_count', 1))}"
                        else:
                            is_watched = pd.notnull(item.get('last_watched_at')) or (item.get('viewed_leaf_count', 0) > 0)
                            watch_status = " 👁️ Watched" if is_watched else " ⚪ Unwatched"
                    
                    title_line = f"{status_icon} **{item['title']}** ({year}){status_text}{watch_status}"
                    
                    if item['is_owned']:
                        plex_link = f"https://app.plex.tv/desktop/#!/server/{item['server_id']}/details?key=%2Flibrary%2Fmetadata%2F{item['rating_key']}"
                        title_line += f" [Plex ↗]({plex_link})"
                    else:
                        tmdb_link = f"https://www.themoviedb.org/movie/{item['tmdb_id']}"
                        title_line += f" [TMDB ↗]({tmdb_link})"
                    st.markdown(title_line)

                with col_item_auto:
                    if not item['is_owned']:
                        is_movie = series['collection_id'].startswith("movie_") or (not series['collection_id'].startswith("tv_") and not series.get('tvdb_id'))
                        service = "radarr" if is_movie else "sonarr"
                        
                        # Lookup ID: Movie uses item TMDB ID, Show uses series TVDB ID
                        lookup_id = item['tmdb_id'] if is_movie else series.get('tvdb_id')
                        all_items = radarr_items if is_movie else sonarr_items
                        
                        service_configured = plex.get_setting(f"{service}_url") and plex.get_setting(f"{service}_api_key")
                        if not service_configured:
                            st.caption(f"📡 {service.capitalize()} Not Set")
                        elif not lookup_id or pd.isna(lookup_id):
                            id_type = "TMDB" if is_movie else "TVDB"
                            st.caption(f"📡 {id_type} Missing")
                        else:
                            auto_status = plex.check_automation_status(lookup_id, service, all_items)
                            if auto_status == "Not Tracked":
                                if st.button(f"➕ Add to {service.capitalize()}", key=f"auto_add_{item['tmdb_id']}_{series['collection_id']}_{service}"):
                                    profile = plex.get_setting(f"{service}_profile")
                                    folder = plex.get_setting(f"{service}_folder")
                                    if profile and folder:
                                        success, msg = plex.add_to_automation(lookup_id, service, profile, folder, title=item['title'])
                                        if success: 
                                            st.success(msg)
                                            if is_movie:
                                                _ = get_cached_radarr_items.clear()
                                            else:
                                                _ = get_cached_sonarr_items.clear()
                                            time.sleep(1)
                                            st.rerun()
                                        else: st.error(msg)
                                    else:
                                        st.error(f"Set {service.capitalize()} defaults in Settings.")
                            else:
                                st.caption(f"📡 {auto_status}")
                
                with col_item_del:
                    if item['is_owned']:
                        if st.button("🗑️", key=f"del_part_{series['collection_id']}_{item['tmdb_id']}", help=f"Delete from Plex"):
                            st.session_state.pending_deletes = [{"rk": item['rating_key'], "title": item['title'],
                                                                  "type": item.get('type', 'movie'),
                                                                  "library": item.get('library', ''),
                                                                  "size_bytes": item.get('size_bytes', 0)}]
                            st.rerun()

                if item['overview']:
                    st.caption(item['overview'])
            st.divider()
@st.dialog("Confirm Cache Wipe")
def confirm_wipe_cache_dialog():
    st.warning("This will permanently delete all cached library and series data. Your Settings (Plex connection, API keys, etc.) will be preserved.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Yes, Wipe Cache", type="primary", use_container_width=True):
            conn = sqlite3.connect(plex.DB_PATH)
            c = conn.cursor()
            c.execute("DROP TABLE IF EXISTS library_cache")
            c.execute("DROP TABLE IF EXISTS series_cache")
            conn.commit()
            conn.close()
            plex.init_db()
            _cached_load_library.clear()
            st.rerun()
    with col2:
        if st.button("Cancel", use_container_width=True):
            st.rerun()

@st.dialog("Confirm Bulk Deletion")
def confirm_delete_dialog(items):
    num_items = len(items)
    st.error(f"### ⚠️ CAUTION\nYou are about to delete **{num_items}** items from Plex!")
    
    # List first 10 items
    item_list = "\n".join([f"- {item['title']}" for item in items[:10]])
    if num_items > 10:
        item_list += f"\n- ...and {num_items - 10} more"
    st.markdown(item_list)
    
    st.warning("This will permanently remove the files from your server. This action cannot be undone.")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("❌ YES, DELETE ALL", use_container_width=True):
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            success_count = 0
            for idx, item in enumerate(items):
                status_text.text(f"Deleting ({idx+1}/{num_items}): {item['title']}...")
                try:
                    plex.delete_item(st.session_state.plex_url, st.session_state.plex_token, item['rk'])
                    plex.log_deletion(
                        title=item['title'],
                        type_=item.get('type', ''),
                        library=item.get('library', ''),
                        size_bytes=item.get('size_bytes', 0),
                        rating_key=item['rk']
                    )
                    success_count += 1
                except Exception as e:
                    st.error(f"Failed to delete {item['title']}: {e}")
                progress_bar.progress((idx + 1) / num_items)
            
            if "pending_deletes" in st.session_state:
                del st.session_state.pending_deletes
            if "audit_table" in st.session_state:
                st.session_state["audit_table"] = {"selection": {"rows": [], "columns": [], "cells": []}}
            _cached_load_library.clear()
            st.toast(f"✅ Deleted {success_count} of {num_items} items from Plex")
            st.rerun()
    with col2:
        if st.button("Cancel", use_container_width=True):
            if "pending_deletes" in st.session_state:
                del st.session_state.pending_deletes
            # Clear the selection on cancel too
            if "audit_table" in st.session_state:
                st.session_state["audit_table"] = {"selection": {"rows": [], "columns": [], "cells": []}}
            st.rerun()

def render_audit_log():
    st.header("📋 Deletion Audit Log")
    log_df = plex.get_deletion_log()

    if log_df.empty:
        st.info("No deletions recorded yet. Items deleted through this app will appear here.")
        return

    total_freed = log_df['size_bytes'].fillna(0).sum()
    total_gb = total_freed / (1024 ** 3)
    size_str = f"{total_gb:.2f} GB" if total_gb < 1000 else f"{total_gb / 1024:.2f} TB"

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Total Deletions", f"{len(log_df):,}")
    with m2:
        st.metric("Space Freed", size_str)
    with m3:
        if not log_df.empty:
            latest = log_df['deleted_at'].iloc[0][:10]
            st.metric("Most Recent", latest)

    col_exp, col_clr = st.columns([1, 1])
    with col_exp:
        csv = log_df.drop(columns=['id']).to_csv(index=False)
        st.download_button("📥 Export CSV", data=csv,
                           file_name=f"deletion_log_{datetime.now().strftime('%Y%m%d')}.csv",
                           mime="text/csv", use_container_width=True)
    with col_clr:
        if st.button("🗑️ Clear Log", use_container_width=True):
            plex.clear_deletion_log()
            st.rerun()

    display = log_df.copy()
    display['Size (GB)'] = (display['size_bytes'].fillna(0) / (1024 ** 3)).round(2)
    display['Deleted At'] = display['deleted_at'].str[:19].str.replace('T', ' ')
    display = display.rename(columns={'title': 'Title', 'type': 'Type', 'library': 'Library'})
    st.dataframe(
        display[['Deleted At', 'Title', 'Type', 'Library', 'Size (GB)']],
        hide_index=True,
        use_container_width=True,
        column_config={
            "Size (GB)": st.column_config.NumberColumn("Size (GB)", format="%.2f GB"),
        }
    )

def handle_deletions():
    if "pending_deletes" in st.session_state:
        items = st.session_state.pending_deletes
        confirm_delete_dialog(items)

handle_deletions()
main_dashboard()
