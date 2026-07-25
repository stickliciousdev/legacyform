"""
Event & Staff Tracker — Streamlit + Supabase (Postgres)

Legacy paper-form digitizer: multi-day events, venue search with contacts,
weather lookup, staff shifts across midnight, Excel export for UrbanTree.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from io import BytesIO
from typing import Any

import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text

st.set_page_config(page_title="Legacy Event Entry", page_icon="🚚", layout="wide")

SETUP_OPTIONS = ["Food Truck", "Tent", "Ice Cream Van"]
TZ_LABEL = "Australia/Sydney"

# ── Connection ────────────────────────────────────────────────────────────────
conn = st.connection("supabase_db", type="sql")

if "events_saved_session" not in st.session_state:
    st.session_state.events_saved_session = 0
if "last_event_defaults" not in st.session_state:
    st.session_state.last_event_defaults = None
if "weather_preview" not in st.session_state:
    st.session_state.weather_preview = None


def run_write(query: str, params: dict | None = None):
    with conn.session as s:
        result = s.execute(text(query), params or {})
        s.commit()
        return result


def fetch_df(query: str, params: dict | None = None) -> pd.DataFrame:
    return conn.query(query, params=params, ttl=0)


def strip_tz_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.select_dtypes(include=["datetimetz"]).columns:
        out[col] = out[col].dt.tz_localize(None)
    return out


def combine_dt(d: date, t: dtime) -> datetime:
    return datetime.combine(d, t)


def event_label(row) -> str:
    eid = int(row["id"])
    venue = row.get("venue") or "?"
    ed = row.get("event_date") or row.get("start_at") or ""
    return f"EVT-{eid:04d} — {venue} ({ed})"


def load_events_for_picker() -> pd.DataFrame:
    return fetch_df(
        """
        select id, venue, event_date, start_at, end_at
        from events
        order by coalesce(start_at, event_date::timestamptz) desc, id desc
        limit 300
        """
    )


def load_staff_roster() -> pd.DataFrame:
    return fetch_df("select id, name, default_pay_rate from staff order by name")


def load_venues(search: str = "") -> pd.DataFrame:
    if search.strip():
        return fetch_df(
            """
            select id, name, address, contact_name, contact_phone, contact_email, notes, lat, lng
            from venues
            where name ilike :q or address ilike :q
            order by name
            limit 100
            """,
            {"q": f"%{search.strip()}%"},
        )
    return fetch_df(
        """
        select id, name, address, contact_name, contact_phone, contact_email, notes, lat, lng
        from venues
        order by name
        limit 200
        """
    )


def venue_label(row) -> str:
    addr = (row.get("address") or "").strip()
    return f"{row['name']}" + (f" — {addr}" if addr else "")


def geocode_address(address: str) -> tuple[float | None, float | None]:
    if not address.strip():
        return None, None
    try:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": address, "count": 1, "language": "en", "format": "json"},
            timeout=12,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            return None, None
        return float(results[0]["latitude"]), float(results[0]["longitude"])
    except Exception:
        return None, None


def fetch_historical_weather(lat: float, lng: float, day: date) -> dict[str, Any] | None:
    """Pull daily summary from Open-Meteo archive (no API key)."""
    try:
        r = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat,
                "longitude": lng,
                "start_date": day.isoformat(),
                "end_date": day.isoformat(),
                "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum",
                "timezone": TZ_LABEL,
            },
            timeout=15,
        )
        r.raise_for_status()
        daily = r.json().get("daily") or {}
        if not daily.get("time"):
            return None
        code = (daily.get("weathercode") or [None])[0]
        tmax = (daily.get("temperature_2m_max") or [None])[0]
        tmin = (daily.get("temperature_2m_min") or [None])[0]
        precip = (daily.get("precipitation_sum") or [None])[0]
        code_map = {
            0: "Clear",
            1: "Mainly clear",
            2: "Partly cloudy",
            3: "Overcast",
            45: "Fog",
            48: "Depositing rime fog",
            51: "Light drizzle",
            61: "Rain",
            63: "Moderate rain",
            65: "Heavy rain",
            71: "Snow",
            80: "Rain showers",
            95: "Thunderstorm",
        }
        condition = code_map.get(int(code) if code is not None else -1, f"Code {code}")
        summary = f"{condition}; high {tmax}°C / low {tmin}°C; precip {precip} mm"
        avg_temp = None
        if tmax is not None and tmin is not None:
            avg_temp = round((float(tmax) + float(tmin)) / 2, 1)
        elif tmax is not None:
            avg_temp = float(tmax)
        return {
            "weather_summary": summary,
            "weather_temp_c": avg_temp,
            "weather_precip_mm": float(precip) if precip is not None else None,
        }
    except Exception:
        return None


# ── Tabs ─────────────────────────────────────────────────────────────────────
tab_event, tab_staff_shift, tab_venues, tab_roster, tab_browse, tab_export = st.tabs(
    [
        "New Event Report",
        "Add Staff Shift",
        "Venues",
        "Staff Roster",
        "Browse / Fix",
        "Export to Excel",
    ]
)

st.caption(f"Events saved this session: **{st.session_state.events_saved_session}**")

# === TAB — Venues =============================================================
with tab_venues:
    st.subheader("Venues directory")
    st.caption(
        "Add each school/market/show once. Search from the Event form to autofill "
        "name, address, and contact details for analytics / marketing."
    )

    with st.form("venue_form", clear_on_submit=True):
        vc1, vc2 = st.columns(2)
        with vc1:
            v_name = st.text_input("Venue / Show name *")
            v_address = st.text_input("Address")
            v_contact = st.text_input("Contact name")
        with vc2:
            v_phone = st.text_input("Contact phone")
            v_email = st.text_input("Contact email")
            v_notes = st.text_area("Notes", height=68)
        geocode_on_save = st.checkbox("Geocode address on save (for weather lookups)", value=True)
        venue_submitted = st.form_submit_button("Add venue", type="primary")

    if venue_submitted:
        if not v_name.strip():
            st.error("Venue name is required.")
        else:
            lat = lng = None
            if geocode_on_save and v_address.strip():
                lat, lng = geocode_address(v_address.strip())
                if lat is None:
                    st.warning("Could not geocode address — venue saved without coordinates.")
            run_write(
                """
                insert into venues (name, address, contact_name, contact_phone, contact_email, notes, lat, lng)
                values (:name, :address, :contact_name, :contact_phone, :contact_email, :notes, :lat, :lng)
                """,
                {
                    "name": v_name.strip(),
                    "address": v_address.strip(),
                    "contact_name": v_contact.strip(),
                    "contact_phone": v_phone.strip(),
                    "contact_email": v_email.strip(),
                    "notes": v_notes.strip(),
                    "lat": lat,
                    "lng": lng,
                },
            )
            st.success(f"Added venue: {v_name.strip()}")

    st.divider()
    search_v = st.text_input("Filter venues", key="venue_filter")
    venues_df = load_venues(search_v)
    st.dataframe(venues_df, use_container_width=True, hide_index=True)

# === TAB — New Event Report ===================================================
with tab_event:
    st.subheader("New Event Report")
    st.caption("One paper form → one record. Use venue search + Copy previous for speed.")

    defaults = st.session_state.last_event_defaults or {}
    if st.button("Copy previous event defaults"):
        if not st.session_state.last_event_defaults:
            st.warning("No previous event in this session yet.")
        else:
            st.info("Defaults loaded below — adjust anything that differs on this paper form.")

    # Venue search (outside form so selection can refresh contact preview)
    st.markdown("**1 — Event details**")
    venue_q = st.text_input("Search venues", placeholder="e.g. Marayong Public School", key="event_venue_q")
    venues_pick = load_venues(venue_q)
    selected_venue_id = None
    selected_venue_row = None

    if venues_pick.empty:
        st.info("No venues match — add one in the **Venues** tab (or use free-text venue name below).")
        free_venue = st.text_input("Show name / Venue *", value=defaults.get("venue", ""))
        venue_snapshot = free_venue
    else:
        options = list(venues_pick["id"])
        selected_venue_id = st.selectbox(
            "Pick venue *",
            options=options,
            format_func=lambda vid: venue_label(venues_pick[venues_pick["id"] == vid].iloc[0]),
            key="event_venue_pick",
        )
        selected_venue_row = venues_pick[venues_pick["id"] == selected_venue_id].iloc[0]
        st.write(
            f"**Address:** {selected_venue_row.get('address') or '—'}  \n"
            f"**Contact:** {selected_venue_row.get('contact_name') or '—'} "
            f"| {selected_venue_row.get('contact_phone') or '—'} "
            f"| {selected_venue_row.get('contact_email') or '—'}"
        )
        venue_snapshot = selected_venue_row["name"]

    with st.form("event_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            rent = st.number_input(
                "Rent ($)",
                min_value=0.0,
                step=1.0,
                format="%.2f",
                value=float(defaults.get("rent", 0.0)),
            )
            start_d = st.date_input(
                "Start date *",
                value=defaults.get("start_d", date.today()),
            )
            start_t = st.time_input(
                "Start time *",
                value=defaults.get("start_t", dtime(9, 0)),
            )
            end_d = st.date_input(
                "End date *",
                value=defaults.get("end_d", date.today()),
            )
            end_t = st.time_input(
                "End time *",
                value=defaults.get("end_t", dtime(17, 0)),
            )
        with col2:
            setup_types = st.multiselect(
                "Stall type * (truck / tent / van)",
                SETUP_OPTIONS,
                default=defaults.get("setup_types", []),
            )
            setup_other = st.text_input("Stall — Other (optional)", value=defaults.get("setup_other", ""))
            power_provided = st.radio(
                "Power *",
                ["Yes", "No"],
                horizontal=True,
                index=0 if defaults.get("power_provided", True) else 1,
            )
            water_access = st.radio(
                "Water *",
                ["Yes", "No"],
                horizontal=True,
                index=0 if defaults.get("water_access", False) else 1,
            )
            competition_present = st.radio(
                "Any other competition? *",
                ["Yes", "No"],
                horizontal=True,
                index=0 if defaults.get("competition_present", False) else 1,
            )
            competition_count = st.number_input(
                "If yes — how many?",
                min_value=0,
                step=1,
                value=int(defaults.get("competition_count", 0) or 0),
            )
            competition_notes = st.text_area(
                "Competition notes",
                value=defaults.get("competition_notes", ""),
                placeholder="Who / what they sold",
            )

        st.markdown("**Weather** (optional — fetch after you know venue + start date)")
        w1, w2 = st.columns([1, 3])
        with w1:
            do_fetch_weather = st.checkbox("Fetch weather on save", value=True)
        with w2:
            weather_manual = st.text_input(
                "Or type weather note",
                value=(st.session_state.weather_preview or {}).get("weather_summary", ""),
            )

        st.markdown("**4 — Sales summary**")
        s1, s2, s3, s4 = st.columns(4)
        with s1:
            eftpos = st.number_input("Eftpos ($) *", min_value=0.0, step=1.0, format="%.2f")
        with s2:
            cash = st.number_input("Cash ($) *", min_value=0.0, step=1.0, format="%.2f")
        with s3:
            food = st.number_input("Food ($)", min_value=0.0, step=1.0, format="%.2f")
        with s4:
            drinks = st.number_input("Drinks ($)", min_value=0.0, step=1.0, format="%.2f")
        other_sales = st.number_input("Other ($)", min_value=0.0, step=1.0, format="%.2f")

        st.markdown("**Square (optional cross-reference)**")
        square_note = st.text_input(
            "Square note",
            placeholder="e.g. Square day export 2024-03-12 — attach deep sales later",
        )

        st.markdown("**5 — Comments**")
        notes = st.text_area("Final comments")

        submitted = st.form_submit_button("Submit Event Report", type="primary")

    if submitted:
        start_at = combine_dt(start_d, start_t)
        end_at = combine_dt(end_d, end_t)
        errors = []
        if not (venue_snapshot or "").strip():
            errors.append("Venue is required (pick from search or type a name).")
        if not setup_types and not setup_other.strip():
            errors.append("Select at least one stall type (or describe one under Other).")
        if end_at <= start_at:
            errors.append("End must be after start (multi-day / overnight is OK).")
        if competition_present == "Yes" and int(competition_count) < 1:
            errors.append("Competition is Yes — enter how many (≥ 1).")
        total = round(eftpos + cash, 2)
        category_total = round(food + drinks + other_sales, 2)
        if total > 0 and abs(total - category_total) > 0.01:
            errors.append(
                f"Food + Drinks + Other (${category_total:.2f}) must match Eftpos + Cash (${total:.2f})."
            )

        if errors:
            for e in errors:
                st.error(e)
        else:
            weather_summary = weather_manual.strip()
            weather_temp = None
            weather_precip = None
            weather_fetched_at = None

            if do_fetch_weather:
                lat = lng = None
                if selected_venue_row is not None:
                    lat = selected_venue_row.get("lat")
                    lng = selected_venue_row.get("lng")
                    if pd.isna(lat):
                        lat = None
                    if pd.isna(lng):
                        lng = None
                    if lat is None or lng is None:
                        addr = (selected_venue_row.get("address") or venue_snapshot or "").strip()
                        lat, lng = geocode_address(addr)
                        if lat is not None and selected_venue_id is not None:
                            run_write(
                                "update venues set lat = :lat, lng = :lng where id = :id",
                                {"lat": lat, "lng": lng, "id": int(selected_venue_id)},
                            )
                if lat is not None and lng is not None:
                    w = fetch_historical_weather(float(lat), float(lng), start_d)
                    if w:
                        weather_summary = weather_summary or w["weather_summary"]
                        weather_temp = w["weather_temp_c"]
                        weather_precip = w["weather_precip_mm"]
                        weather_fetched_at = datetime.utcnow()
                    else:
                        st.warning("Weather fetch failed — saved without auto weather.")
                else:
                    st.warning("No coordinates for venue — saved without auto weather.")

            result = run_write(
                """
                insert into events (
                    venue, venue_id, event_date, start_at, end_at, rent,
                    setup_types, setup_other, power_provided, water_access,
                    competition_present, competition_count, competition_notes,
                    eftpos, cash, food, drinks, other_sales, notes,
                    weather_summary, weather_temp_c, weather_precip_mm, weather_fetched_at,
                    square_note
                ) values (
                    :venue, :venue_id, :event_date, :start_at, :end_at, :rent,
                    :setup_types, :setup_other, :power_provided, :water_access,
                    :competition_present, :competition_count, :competition_notes,
                    :eftpos, :cash, :food, :drinks, :other_sales, :notes,
                    :weather_summary, :weather_temp_c, :weather_precip_mm, :weather_fetched_at,
                    :square_note
                ) returning id
                """,
                {
                    "venue": venue_snapshot.strip(),
                    "venue_id": int(selected_venue_id) if selected_venue_id is not None else None,
                    "event_date": start_d,
                    "start_at": start_at,
                    "end_at": end_at,
                    "rent": rent,
                    "setup_types": ", ".join(setup_types),
                    "setup_other": setup_other.strip(),
                    "power_provided": power_provided == "Yes",
                    "water_access": water_access == "Yes",
                    "competition_present": competition_present == "Yes",
                    "competition_count": int(competition_count) if competition_present == "Yes" else None,
                    "competition_notes": competition_notes.strip(),
                    "eftpos": eftpos,
                    "cash": cash,
                    "food": food,
                    "drinks": drinks,
                    "other_sales": other_sales,
                    "notes": notes.strip(),
                    "weather_summary": weather_summary,
                    "weather_temp_c": weather_temp,
                    "weather_precip_mm": weather_precip,
                    "weather_fetched_at": weather_fetched_at,
                    "square_note": square_note.strip(),
                },
            )
            new_id = result.scalar()
            st.session_state.events_saved_session += 1
            st.session_state.last_event_defaults = {
                "venue": venue_snapshot.strip(),
                "rent": rent,
                "start_d": start_d,
                "start_t": start_t,
                "end_d": end_d,
                "end_t": end_t,
                "setup_types": setup_types,
                "setup_other": setup_other,
                "power_provided": power_provided == "Yes",
                "water_access": water_access == "Yes",
                "competition_present": competition_present == "Yes",
                "competition_count": int(competition_count),
                "competition_notes": competition_notes,
            }
            st.success(
                f"Saved as EVT-{new_id:04d}. "
                f"Session total: {st.session_state.events_saved_session}. Form cleared."
            )
            if weather_summary:
                st.caption(f"Weather: {weather_summary}")

# === TAB — Add Staff Shift ====================================================
with tab_staff_shift:
    st.subheader("Add Staff Shift")
    st.caption("One record per staff member per event. Times may cross midnight / days.")

    events_df = load_events_for_picker()
    roster_df = load_staff_roster()

    if events_df.empty:
        st.info("No events yet — add one in the **New Event Report** tab first.")
    elif roster_df.empty:
        st.info("No staff on the roster yet — add staff in the **Staff Roster** tab first.")
    else:
        with st.form("staff_shift_form", clear_on_submit=True):
            event_choice = st.selectbox(
                "Event *",
                options=events_df["id"],
                format_func=lambda eid: event_label(events_df[events_df["id"] == eid].iloc[0]),
            )
            staff_choice = st.selectbox(
                "Staff Member *",
                options=roster_df["id"],
                format_func=lambda sid: roster_df[roster_df["id"] == sid].iloc[0]["name"],
            )
            c1, c2 = st.columns(2)
            with c1:
                shift_start_d = st.date_input("Start date *", value=date.today(), key="shift_start_d")
                shift_start_t = st.time_input("Start time *", value=dtime(9, 0), key="shift_start_t")
            with c2:
                shift_end_d = st.date_input("End date *", value=date.today(), key="shift_end_d")
                shift_end_t = st.time_input("End time *", value=dtime(17, 0), key="shift_end_t")

            shift_start = combine_dt(shift_start_d, shift_start_t)
            shift_end = combine_dt(shift_end_d, shift_end_t)
            hours_preview = max((shift_end - shift_start).total_seconds() / 3600.0, 0)

            selected_rate = roster_df[roster_df["id"] == staff_choice].iloc[0]["default_pay_rate"]
            if selected_rate and hours_preview > 0:
                st.caption(
                    f"Suggested pay at ${float(selected_rate):.2f}/hr × {hours_preview:.2f}h: "
                    f"**${hours_preview * float(selected_rate):.2f}** "
                    "(informational only — enter what was actually paid below)"
                )

            amount_paid = st.number_input("Amount Paid ($) *", min_value=0.0, step=1.0, format="%.2f")
            paid = st.radio("Paid?", ["Yes", "No"], horizontal=True)
            submitted_shift = st.form_submit_button("Submit Staff Shift", type="primary")

        if submitted_shift:
            if shift_end <= shift_start:
                st.error("End must be after start (overnight / multi-day is OK).")
            else:
                run_write(
                    """
                    insert into staff_shifts (event_id, staff_id, start_at, end_at, amount_paid, paid)
                    values (:event_id, :staff_id, :start_at, :end_at, :amount_paid, :paid)
                    """,
                    {
                        "event_id": int(event_choice),
                        "staff_id": int(staff_choice),
                        "start_at": shift_start,
                        "end_at": shift_end,
                        "amount_paid": amount_paid,
                        "paid": paid == "Yes",
                    },
                )
                st.success("Shift saved — form cleared for the next one.")

# === TAB — Staff Roster =======================================================
with tab_roster:
    st.subheader("Staff Roster")
    st.caption("Add each staff member once — they show up in Add Staff Shift.")

    with st.form("roster_form", clear_on_submit=True):
        rc1, rc2, rc3 = st.columns(3)
        with rc1:
            staff_name = st.text_input("Staff Name *")
        with rc2:
            default_rate = st.number_input(
                "Default Pay Rate ($/hr)", min_value=0.0, step=0.5, format="%.2f"
            )
        with rc3:
            phone = st.text_input("Phone (optional)")
        roster_submitted = st.form_submit_button("Add to Roster", type="primary")

    if roster_submitted:
        if not staff_name.strip():
            st.error("Name is required.")
        else:
            try:
                run_write(
                    "insert into staff (name, default_pay_rate, phone) values (:name, :rate, :phone)",
                    {
                        "name": staff_name.strip(),
                        "rate": default_rate or None,
                        "phone": phone.strip(),
                    },
                )
                st.success(f"Added {staff_name.strip()} to the roster.")
            except Exception as e:
                if "unique" in str(e).lower():
                    st.error(f"'{staff_name.strip()}' is already on the roster.")
                else:
                    st.error(f"Couldn't save: {e}")

    st.divider()
    st.dataframe(load_staff_roster(), use_container_width=True, hide_index=True)

# === TAB — Browse / Fix =======================================================
with tab_browse:
    st.subheader("Browse / Fix")
    st.caption("Scan recent records and correct mistakes without leaving the app.")

    st.markdown("**Events**")
    events_view = fetch_df(
        """
        select e.id, e.venue, e.event_date, e.start_at, e.end_at, e.rent,
               e.setup_types, e.power_provided, e.water_access,
               e.competition_present, e.competition_count,
               e.eftpos, e.cash, e.food, e.drinks, e.other_sales,
               e.weather_summary, e.square_note, e.notes,
               v.address as venue_address, v.contact_name, v.contact_phone
        from events e
        left join venues v on v.id = e.venue_id
        order by coalesce(e.start_at, e.event_date::timestamptz) desc, e.id desc
        limit 500
        """
    )
    st.dataframe(events_view, use_container_width=True, hide_index=True)

    st.markdown("**Fix an event**")
    if events_view.empty:
        st.info("No events to edit yet.")
    else:
        fix_id = st.selectbox(
            "Event to edit",
            options=list(events_view["id"]),
            format_func=lambda eid: event_label(events_view[events_view["id"] == eid].iloc[0]),
            key="fix_event_id",
        )
        row = events_view[events_view["id"] == fix_id].iloc[0]
        with st.form("fix_event_form"):
            fx1, fx2 = st.columns(2)
            with fx1:
                f_venue = st.text_input("Venue", value=str(row["venue"] or ""))
                f_rent = st.number_input(
                    "Rent ($)",
                    min_value=0.0,
                    step=1.0,
                    format="%.2f",
                    value=float(row["rent"] or 0),
                )
                f_notes = st.text_area("Notes", value=str(row["notes"] or ""))
            with fx2:
                f_eftpos = st.number_input(
                    "Eftpos ($)", min_value=0.0, step=1.0, format="%.2f",
                    value=float(row["eftpos"] or 0),
                )
                f_cash = st.number_input(
                    "Cash ($)", min_value=0.0, step=1.0, format="%.2f",
                    value=float(row["cash"] or 0),
                )
                f_food = st.number_input(
                    "Food ($)", min_value=0.0, step=1.0, format="%.2f",
                    value=float(row["food"] or 0),
                )
                f_drinks = st.number_input(
                    "Drinks ($)", min_value=0.0, step=1.0, format="%.2f",
                    value=float(row["drinks"] or 0),
                )
                f_other = st.number_input(
                    "Other ($)", min_value=0.0, step=1.0, format="%.2f",
                    value=float(row["other_sales"] or 0),
                )
                f_square = st.text_input("Square note", value=str(row["square_note"] or ""))
                f_weather = st.text_input("Weather summary", value=str(row["weather_summary"] or ""))
            fix_submit = st.form_submit_button("Save corrections", type="primary")

        if fix_submit:
            total = round(f_eftpos + f_cash, 2)
            cats = round(f_food + f_drinks + f_other, 2)
            if total > 0 and abs(total - cats) > 0.01:
                st.error(
                    f"Food + Drinks + Other (${cats:.2f}) must match Eftpos + Cash (${total:.2f})."
                )
            else:
                run_write(
                    """
                    update events set
                        venue = :venue,
                        rent = :rent,
                        eftpos = :eftpos,
                        cash = :cash,
                        food = :food,
                        drinks = :drinks,
                        other_sales = :other_sales,
                        notes = :notes,
                        square_note = :square_note,
                        weather_summary = :weather_summary
                    where id = :id
                    """,
                    {
                        "id": int(fix_id),
                        "venue": f_venue.strip(),
                        "rent": f_rent,
                        "eftpos": f_eftpos,
                        "cash": f_cash,
                        "food": f_food,
                        "drinks": f_drinks,
                        "other_sales": f_other,
                        "notes": f_notes.strip(),
                        "square_note": f_square.strip(),
                        "weather_summary": f_weather.strip(),
                    },
                )
                st.success(f"Updated EVT-{int(fix_id):04d}. Refresh the table above.")

    st.markdown("**Staff Shifts**")
    shifts_view = fetch_df(
        "select * from staff_shifts_view order by event_date desc nulls last, id desc limit 500"
    )
    st.dataframe(shifts_view, use_container_width=True, hide_index=True)

# === TAB — Export =============================================================
with tab_export:
    st.subheader("Export to Excel")
    st.caption("Events + Staff Shifts + Venues — bridge into UrbanTree / analytics.")

    if st.button("Build Excel file"):
        events_export = fetch_df(
            """
            select e.*, v.address as venue_address, v.contact_name as venue_contact_name,
                   v.contact_phone as venue_contact_phone, v.contact_email as venue_contact_email
            from events e
            left join venues v on v.id = e.venue_id
            order by coalesce(e.start_at, e.event_date::timestamptz), e.id
            """
        )
        shifts_export = fetch_df(
            "select * from staff_shifts_view order by event_date nulls last, id"
        )
        venues_export = fetch_df("select * from venues order by name")

        events_export = strip_tz_for_excel(events_export)
        shifts_export = strip_tz_for_excel(shifts_export)
        venues_export = strip_tz_for_excel(venues_export)

        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            events_export.to_excel(writer, sheet_name="Events", index=False)
            shifts_export.to_excel(writer, sheet_name="Staff Shifts", index=False)
            venues_export.to_excel(writer, sheet_name="Venues", index=False)
        buffer.seek(0)

        st.download_button(
            "Download Event_Staff_Export.xlsx",
            data=buffer,
            file_name="Event_Staff_Export.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        st.success(
            f"Ready — {len(events_export)} events, {len(shifts_export)} staff shifts, "
            f"{len(venues_export)} venues."
        )
