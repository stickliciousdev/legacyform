"""
Event & Staff Tracker — Streamlit + Supabase (Postgres)

Digitize paper event-report forms straight into a relational database
(events + staff_shifts, linked by event_id), with an Excel export that
turns it back into a two-sheet workbook (Events / Staff Shifts) on demand.
"""

from datetime import date, time as dtime
from io import BytesIO

import pandas as pd
import streamlit as st
from sqlalchemy import text

st.set_page_config(page_title="Event & Staff Tracker", page_icon="🚚", layout="wide")

SETUP_OPTIONS = ["Food Truck", "Tent", "Ice Cream Van"]

# ── Connection ────────────────────────────────────────────────────────────────
# Reads [connections.supabase_db] from .streamlit/secrets.toml (local) or the
# app's Secrets panel (Streamlit Community Cloud). See README for the exact URL.
conn = st.connection("supabase_db", type="sql")


def run_write(query: str, params: dict | None = None):
    """Execute an INSERT/UPDATE inside a transaction, return the cursor result."""
    with conn.session as s:
        result = s.execute(text(query), params or {})
        s.commit()
        return result


def fetch_df(query: str, params: dict | None = None) -> pd.DataFrame:
    """Read-only query, never cached — always the latest data."""
    return conn.query(query, params=params, ttl=0)


# ── Shared helpers ──────────────────────────────────────────────────────────

def event_label(row) -> str:
    return f"EVT-{row['id']:04d} — {row['venue']} ({row['event_date']})"


def load_events_for_picker() -> pd.DataFrame:
    return fetch_df(
        "select id, venue, event_date from events order by event_date desc, id desc limit 200"
    )


def load_staff_roster() -> pd.DataFrame:
    return fetch_df("select id, name, default_pay_rate from staff order by name")


# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_event, tab_staff_shift, tab_roster, tab_browse, tab_export = st.tabs(
    ["New Event Report", "Add Staff Shift", "Staff Roster", "Browse", "Export to Excel"]
)

# === TAB 1 — New Event Report ================================================
with tab_event:
    st.subheader("New Event Report")
    st.caption("One record per event. Fill this in from the paper form, then Submit.")

    with st.form("event_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            venue = st.text_input("Show Name / Venue *")
            event_date = st.date_input("Date *", value=date.today())
            start_time = st.time_input("Start Time *", value=dtime(9, 0))
            end_time = st.time_input("End Time *", value=dtime(17, 0))
            rent = st.number_input("Rent ($)", min_value=0.0, step=1.0, format="%.2f")
        with col2:
            setup_types = st.multiselect("Setup Type *", SETUP_OPTIONS)
            setup_other = st.text_input("Setup — Other (optional)")
            power_provided = st.radio("Power Provided *", ["Yes", "No"], horizontal=True)
            water_access = st.radio("Water Access *", ["Yes", "No"], horizontal=True)
            competition_present = st.radio("Competition Present *", ["Yes", "No"], horizontal=True)

        competition_notes = st.text_area(
            "Competition Notes", placeholder="Which competitor(s) / what they sold — leave blank if none"
        )

        st.markdown("**Sales**")
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

        notes = st.text_area("Notes")

        submitted = st.form_submit_button("Submit Event Report", type="primary")

    if submitted:
        errors = []
        if not venue.strip():
            errors.append("Venue is required.")
        if not setup_types and not setup_other.strip():
            errors.append("Select at least one setup type (or describe one under Other).")
        if end_time <= start_time:
            errors.append("End time must be after start time.")
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
            result = run_write(
                """
                insert into events (
                    venue, event_date, start_time, end_time, rent,
                    setup_types, setup_other, power_provided, water_access,
                    competition_present, competition_notes,
                    eftpos, cash, food, drinks, other_sales, notes
                ) values (
                    :venue, :event_date, :start_time, :end_time, :rent,
                    :setup_types, :setup_other, :power_provided, :water_access,
                    :competition_present, :competition_notes,
                    :eftpos, :cash, :food, :drinks, :other_sales, :notes
                ) returning id
                """,
                {
                    "venue": venue.strip(),
                    "event_date": event_date,
                    "start_time": start_time,
                    "end_time": end_time,
                    "rent": rent,
                    "setup_types": ", ".join(setup_types),
                    "setup_other": setup_other.strip(),
                    "power_provided": power_provided == "Yes",
                    "water_access": water_access == "Yes",
                    "competition_present": competition_present == "Yes",
                    "competition_notes": competition_notes.strip(),
                    "eftpos": eftpos,
                    "cash": cash,
                    "food": food,
                    "drinks": drinks,
                    "other_sales": other_sales,
                    "notes": notes.strip(),
                },
            )
            new_id = result.scalar()
            st.success(f"Saved as EVT-{new_id:04d}. Form cleared — ready for the next one.")

# === TAB 2 — Add Staff Shift ===================================================
with tab_staff_shift:
    st.subheader("Add Staff Shift")
    st.caption("One record per staff member per event.")

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
                shift_start = st.time_input("Start Time *", value=dtime(9, 0), key="shift_start")
            with c2:
                shift_end = st.time_input("End Time *", value=dtime(17, 0), key="shift_end")

            selected_rate = roster_df[roster_df["id"] == staff_choice].iloc[0]["default_pay_rate"]
            if selected_rate:
                hours_preview = max((shift_end.hour * 60 + shift_end.minute) -
                                     (shift_start.hour * 60 + shift_start.minute), 0) / 60
                st.caption(f"Suggested pay at ${selected_rate:.2f}/hr: **${hours_preview * float(selected_rate):.2f}** "
                           "(informational only — enter what was actually paid below)")

            amount_paid = st.number_input("Amount Paid ($) *", min_value=0.0, step=1.0, format="%.2f")
            paid = st.radio("Paid?", ["Yes", "No"], horizontal=True)

            submitted_shift = st.form_submit_button("Submit Staff Shift", type="primary")

        if submitted_shift:
            if shift_end <= shift_start:
                st.error("End time must be after start time.")
            else:
                run_write(
                    """
                    insert into staff_shifts (event_id, staff_id, start_time, end_time, amount_paid, paid)
                    values (:event_id, :staff_id, :start_time, :end_time, :amount_paid, :paid)
                    """,
                    {
                        "event_id": int(event_choice),
                        "staff_id": int(staff_choice),
                        "start_time": shift_start,
                        "end_time": shift_end,
                        "amount_paid": amount_paid,
                        "paid": paid == "Yes",
                    },
                )
                st.success("Shift saved — form cleared for the next one.")

# === TAB 3 — Staff Roster =====================================================
with tab_roster:
    st.subheader("Staff Roster")
    st.caption("Add each staff member once — they'll then show up in the Add Staff Shift tab.")

    with st.form("roster_form", clear_on_submit=True):
        rc1, rc2, rc3 = st.columns(3)
        with rc1:
            staff_name = st.text_input("Staff Name *")
        with rc2:
            default_rate = st.number_input("Default Pay Rate ($/hr)", min_value=0.0, step=0.5, format="%.2f")
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
                    {"name": staff_name.strip(), "rate": default_rate or None, "phone": phone.strip()},
                )
                st.success(f"Added {staff_name.strip()} to the roster.")
            except Exception as e:
                if "unique" in str(e).lower():
                    st.error(f"'{staff_name.strip()}' is already on the roster.")
                else:
                    st.error(f"Couldn't save: {e}")

    st.divider()
    st.dataframe(load_staff_roster(), use_container_width=True, hide_index=True)

# === TAB 4 — Browse ============================================================
with tab_browse:
    st.subheader("Browse Records")

    st.markdown("**Events**")
    events_view = fetch_df("select * from events order by event_date desc, id desc")
    st.dataframe(events_view, use_container_width=True, hide_index=True)

    st.markdown("**Staff Shifts**")
    shifts_view = fetch_df("select * from staff_shifts_view order by event_date desc, id desc")
    st.dataframe(shifts_view, use_container_width=True, hide_index=True)

# === TAB 5 — Export to Excel ===================================================
with tab_export:
    st.subheader("Export to Excel")
    st.caption("Pulls everything currently in the database into a two-sheet workbook: Events + Staff Shifts.")

    if st.button("Build Excel file"):
        events_export = fetch_df("select * from events order by event_date, id")
        shifts_export = fetch_df("select * from staff_shifts_view order by event_date, id")

        # Excel can't store timezone-aware datetimes (Supabase timestamptz → created_at).
        for df in (events_export, shifts_export):
            for col in df.select_dtypes(include=["datetimetz"]).columns:
                df[col] = df[col].dt.tz_localize(None)

        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            events_export.to_excel(writer, sheet_name="Events", index=False)
            shifts_export.to_excel(writer, sheet_name="Staff Shifts", index=False)
        buffer.seek(0)

        st.download_button(
            "Download Event_Staff_Export.xlsx",
            data=buffer,
            file_name="Event_Staff_Export.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        st.success(f"Ready — {len(events_export)} events, {len(shifts_export)} staff shifts.")
