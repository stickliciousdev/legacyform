"""
Event & Staff Tracker — Streamlit + Supabase (Postgres)

Compact paper-form digitizer for fast entry of many legacy event reports.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime
from io import BytesIO

import pandas as pd
import streamlit as st
from sqlalchemy import text

st.set_page_config(page_title="Event & Staff Tracker", page_icon="🚚", layout="wide")

SETUP_OPTIONS = ["Food Truck", "Tent", "Ice Cream Van"]
DATE_FMT = "DD/MM/YYYY"  # Streamlit date_input display
DATE_PY = "%d/%m/%Y"
DATETIME_PY = "%d/%m/%Y %H:%M"

# Tighten vertical spacing for faster scanning / less scrolling
st.markdown(
    """
    <style>
      .block-container { padding-top: 1rem; padding-bottom: 1rem; max-width: 1100px; }
      div[data-testid="stVerticalBlock"] > div { gap: 0.35rem; }
      div[data-testid="stForm"] {
        border: 1px solid #e5e7eb; border-radius: 10px;
        padding: 0.75rem 1rem 0.5rem;
        background: #fafafa;
      }
      div[data-testid="stForm"] label { font-size: 0.85rem; }
      textarea { min-height: 56px !important; }
      div[data-baseweb="input"] input { font-size: 0.95rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

conn = st.connection("supabase_db", type="sql")

if "events_saved_session" not in st.session_state:
    st.session_state.events_saved_session = 0
if "last_event_defaults" not in st.session_state:
    st.session_state.last_event_defaults = None
if "last_event_id" not in st.session_state:
    st.session_state.last_event_id = None
if "shifts_saved_for_event" not in st.session_state:
    st.session_state.shifts_saved_for_event = 0


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


def format_date_au(value) -> str:
    """Display a date/datetime as day/month/year."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.strftime(DATETIME_PY)
    if isinstance(value, date):
        return value.strftime(DATE_PY)
    # string fallback
    try:
        parsed = pd.to_datetime(value)
        if pd.isna(parsed):
            return str(value)
        if getattr(parsed, "hour", 0) or getattr(parsed, "minute", 0):
            return parsed.strftime(DATETIME_PY)
        return parsed.strftime(DATE_PY)
    except Exception:
        return str(value)


def format_df_dates_au(df: pd.DataFrame) -> pd.DataFrame:
    """Copy a dataframe with date/datetime columns shown as DD/MM/YYYY for tables."""
    out = df.copy()
    for col in out.columns:
        series = out[col]
        if pd.api.types.is_datetime64_any_dtype(series):
            # distinguish date-only vs datetime by checking if all times are midnight
            times = series.dropna()
            if times.empty:
                continue
            if (times.dt.normalize() == times).all():
                out[col] = series.dt.strftime(DATE_PY)
            else:
                out[col] = series.dt.strftime(DATETIME_PY)
        elif series.dtype == object:
            # Postgres date sometimes arrives as Python date objects
            sample = series.dropna().head(1)
            if not sample.empty and isinstance(sample.iloc[0], (date, datetime)):
                out[col] = series.map(format_date_au)
    return out


def combine_dt(d: date, t: dtime) -> datetime:
    return datetime.combine(d, t)


def money(v) -> float:
    """Treat empty number inputs as 0."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0.0
    return float(v)


def allocate_sales_categories(eftpos, cash, food, drinks, other):
    """
    Eftpos/cash are the payment total.
    If only food OR only drinks is entered, fill the other with the remainder
    (after subtracting Other if provided). No hard match validation.
    """
    eft = money(eftpos)
    csh = money(cash)
    total = round(eft + csh, 2)
    other_v = money(other) if other is not None else None
    food_v = food
    drinks_v = drinks
    note = None

    other_amt = money(other_v) if other_v is not None else 0.0

    if total > 0:
        if food_v is None and drinks_v is not None:
            food_v = max(round(total - money(drinks_v) - other_amt, 2), 0.0)
            note = f"Auto-filled Food ${food_v:.2f} (remaining after Drinks/Other)."
        elif drinks_v is None and food_v is not None:
            drinks_v = max(round(total - money(food_v) - other_amt, 2), 0.0)
            note = f"Auto-filled Drinks ${drinks_v:.2f} (remaining after Food/Other)."
        elif food_v is None and drinks_v is None:
            # Neither food nor drinks entered — put remainder into Food if Other set,
            # otherwise leave categories at 0 (user may only have payment totals).
            if other_v is not None:
                food_v = max(round(total - other_amt, 2), 0.0)
                drinks_v = 0.0
                note = f"Auto-filled Food ${food_v:.2f} (remaining after Other)."
            else:
                food_v, drinks_v, other_v = 0.0, 0.0, 0.0

    return (
        eft,
        csh,
        money(food_v),
        money(drinks_v),
        money(other_v) if other_v is not None else 0.0,
        note,
    )


def event_label(row) -> str:
    eid = int(row["id"])
    venue = row.get("venue") or "?"
    ed = row.get("event_date") or row.get("start_at") or ""
    return f"EVT-{eid:04d} — {venue} ({format_date_au(ed)})"


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


# Top bar
top_l, top_r = st.columns([3, 1])
with top_l:
    st.title("Event & Staff Tracker")
with top_r:
    st.metric("Saved this session", st.session_state.events_saved_session)

tab_event, tab_roster, tab_browse, tab_export = st.tabs(
    [
        "New Event Report",
        "Staff Roster",
        "Browse / Fix",
        "Export to Excel",
    ]
)

# === TAB 1 — New Event Report ================================================
with tab_event:
    defaults = st.session_state.last_event_defaults or {}

    # Clear identity fields on the run after a successful save (before widgets exist)
    if st.session_state.pop("clear_evt_identity", False):
        st.session_state.evt_venue = ""
        st.session_state.evt_location = ""
        st.session_state.evt_contact = ""

    head_l, head_r = st.columns([3, 1])
    with head_l:
        st.caption("Fill top → bottom from the paper form. Submit clears for the next one.")
    with head_r:
        copy_clicked = st.button("Copy previous", use_container_width=True)
    if copy_clicked:
        if not st.session_state.last_event_defaults:
            st.warning("Nothing to copy yet — submit one event first.")
        else:
            d = st.session_state.last_event_defaults
            st.session_state.evt_venue = d.get("venue", "")
            st.session_state.evt_location = d.get("location", "")
            st.session_state.evt_contact = d.get("contact_info", "")
            st.session_state.evt_start_d = d.get("start_d", date.today())
            st.session_state.evt_end_d = d.get(
                "end_d", st.session_state.evt_start_d
            )
            st.session_state.evt_prev_start_d = st.session_state.evt_start_d
            st.session_state.evt_start_t = d.get("start_t", dtime(9, 0))
            st.session_state.evt_end_t = d.get("end_t", dtime(17, 0))
            st.session_state.evt_rent = float(d.get("rent", 0.0))
            st.toast("Previous defaults loaded")

    # Identity + dates sit outside the form so end date can follow start live
    if "evt_venue" not in st.session_state:
        st.session_state.evt_venue = defaults.get("venue", "")
    if "evt_location" not in st.session_state:
        st.session_state.evt_location = defaults.get("location", "")
    if "evt_contact" not in st.session_state:
        st.session_state.evt_contact = defaults.get("contact_info", "")
    if "evt_start_d" not in st.session_state:
        st.session_state.evt_start_d = defaults.get("start_d", date.today())
    if "evt_end_d" not in st.session_state:
        st.session_state.evt_end_d = defaults.get(
            "end_d", st.session_state.evt_start_d
        )
    if "evt_prev_start_d" not in st.session_state:
        st.session_state.evt_prev_start_d = st.session_state.evt_start_d
    if "evt_start_t" not in st.session_state:
        st.session_state.evt_start_t = defaults.get("start_t", dtime(9, 0))
    if "evt_end_t" not in st.session_state:
        st.session_state.evt_end_t = defaults.get("end_t", dtime(17, 0))
    if "evt_rent" not in st.session_state:
        st.session_state.evt_rent = float(defaults.get("rent", 0.0))

    # If start date changed and end was still matching the old start, keep them in sync
    if st.session_state.evt_start_d != st.session_state.evt_prev_start_d:
        if st.session_state.evt_end_d == st.session_state.evt_prev_start_d:
            st.session_state.evt_end_d = st.session_state.evt_start_d
        st.session_state.evt_prev_start_d = st.session_state.evt_start_d

    v1, v2, v3 = st.columns([2, 2, 1.4])
    with v1:
        venue = st.text_input(
            "Event name *",
            key="evt_venue",
            placeholder="e.g. Marayong Public School fete",
            help="Show / event name — what you call this booking.",
        )
    with v2:
        location = st.text_input(
            "Address",
            key="evt_location",
            placeholder="Paste full address from Google Maps",
            help="Full street address — paste from Google so it can plot on a map later.",
        )
    with v3:
        contact_info = st.text_input(
            "Contact",
            key="evt_contact",
            placeholder="Name / phone",
        )

    t1, t2, t3, t4, t5 = st.columns([1.1, 1, 1.1, 1, 1])
    with t1:
        start_d = st.date_input(
            "Start date *",
            key="evt_start_d",
            format=DATE_FMT,
        )
    with t2:
        start_t = st.time_input("Start *", key="evt_start_t")
    with t3:
        end_d = st.date_input(
            "End date *",
            key="evt_end_d",
            format=DATE_FMT,
            help="Matches start date until you change it (multi-day / past midnight).",
        )
    with t4:
        end_t = st.time_input("End *", key="evt_end_t")
    with t5:
        rent = st.number_input(
            "Rent $",
            min_value=0.0,
            step=1.0,
            format="%.2f",
            key="evt_rent",
        )

    with st.form("event_form", clear_on_submit=True, border=False):
        # Setup + utilities
        s1, s2 = st.columns([2.2, 1.2])
        with s1:
            setup_types = st.multiselect(
                "Setup type *",
                SETUP_OPTIONS,
                default=defaults.get("setup_types", []),
            )
        with s2:
            setup_other = st.text_input(
                "Setup other",
                value=defaults.get("setup_other", ""),
                placeholder="If not listed",
            )

        u1, u2, u3, u4, u5 = st.columns(5)
        with u1:
            power_provided = st.radio(
                "Power *",
                ["Yes", "No"],
                horizontal=True,
                index=0 if defaults.get("power_provided", True) else 1,
            )
        with u2:
            water_access = st.radio(
                "Water *",
                ["Yes", "No"],
                horizontal=True,
                index=0 if defaults.get("water_access", False) else 1,
            )
        with u3:
            competition_present = st.radio(
                "Competition *",
                ["Yes", "No"],
                horizontal=True,
                index=0 if defaults.get("competition_present", False) else 1,
            )
        with u4:
            competition_count = st.number_input(
                "How many",
                min_value=0,
                step=1,
                value=int(defaults.get("competition_count", 0) or 0),
            )
        with u5:
            weather = st.text_input(
                "Weather",
                value=defaults.get("weather", ""),
                placeholder="Fine / rain",
            )

        competition_notes = st.text_input(
            "Competition notes",
            value=defaults.get("competition_notes", ""),
            placeholder="Who / what they sold",
        )

        # Sales — empty until typed (avoids 0.00 sticking in front of digits)
        st.caption(
            "Sales — leave Food or Drinks blank to auto-fill the other from Eftpos+Cash"
        )
        m1, m2, m3, m4, m5 = st.columns(5)
        with m1:
            eftpos = st.number_input(
                "Eftpos $",
                min_value=0.0,
                step=1.0,
                format="%.2f",
                value=None,
                placeholder="0.00",
            )
        with m2:
            cash = st.number_input(
                "Cash $",
                min_value=0.0,
                step=1.0,
                format="%.2f",
                value=None,
                placeholder="0.00",
            )
        with m3:
            food = st.number_input(
                "Food $",
                min_value=0.0,
                step=1.0,
                format="%.2f",
                value=None,
                placeholder="auto / type",
            )
        with m4:
            drinks = st.number_input(
                "Drinks $",
                min_value=0.0,
                step=1.0,
                format="%.2f",
                value=None,
                placeholder="auto / type",
            )
        with m5:
            other_sales = st.number_input(
                "Other $",
                min_value=0.0,
                step=1.0,
                format="%.2f",
                value=None,
                placeholder="0.00",
            )

        n1, n2 = st.columns([1, 2])
        with n1:
            square_note = st.text_input("Square note", placeholder="Optional")
        with n2:
            notes = st.text_input("Notes / comments", placeholder="Anything else")

        submitted = st.form_submit_button(
            "Submit event report", type="primary", use_container_width=True
        )

    if submitted:
        start_at = combine_dt(start_d, start_t)
        end_at = combine_dt(end_d, end_t)
        errors = []
        if not venue.strip():
            errors.append("Event name is required.")
        if not setup_types and not setup_other.strip():
            errors.append("Select at least one setup type (or describe under Setup other).")
        if end_at <= start_at:
            errors.append("End must be after start.")
        if competition_present == "Yes" and int(competition_count) < 1:
            errors.append("Competition is Yes — enter how many (≥ 1).")

        eftpos, cash, food, drinks, other_sales, sales_note = allocate_sales_categories(
            eftpos, cash, food, drinks, other_sales
        )

        if errors:
            for e in errors:
                st.error(e)
        else:
            if sales_note:
                st.info(sales_note)
            extra_bits = []
            if location.strip():
                extra_bits.append(f"Location: {location.strip()}")
            if contact_info.strip():
                extra_bits.append(f"Contact: {contact_info.strip()}")
            notes_out = notes.strip()
            if extra_bits:
                prefix = " | ".join(extra_bits)
                notes_out = f"{prefix}\n{notes_out}".strip() if notes_out else prefix

            result = run_write(
                """
                insert into events (
                    venue, event_date, start_at, end_at, rent,
                    setup_types, setup_other, power_provided, water_access,
                    competition_present, competition_count, competition_notes,
                    eftpos, cash, food, drinks, other_sales, notes,
                    weather_summary, square_note
                ) values (
                    :venue, :event_date, :start_at, :end_at, :rent,
                    :setup_types, :setup_other, :power_provided, :water_access,
                    :competition_present, :competition_count, :competition_notes,
                    :eftpos, :cash, :food, :drinks, :other_sales, :notes,
                    :weather_summary, :square_note
                ) returning id
                """,
                {
                    "venue": venue.strip(),
                    "event_date": start_d,
                    "start_at": start_at,
                    "end_at": end_at,
                    "rent": rent,
                    "setup_types": ", ".join(setup_types),
                    "setup_other": setup_other.strip(),
                    "power_provided": power_provided == "Yes",
                    "water_access": water_access == "Yes",
                    "competition_present": competition_present == "Yes",
                    "competition_count": (
                        int(competition_count) if competition_present == "Yes" else None
                    ),
                    "competition_notes": competition_notes.strip(),
                    "eftpos": eftpos,
                    "cash": cash,
                    "food": food,
                    "drinks": drinks,
                    "other_sales": other_sales,
                    "notes": notes_out,
                    "weather_summary": weather.strip(),
                    "square_note": square_note.strip(),
                },
            )
            new_id = result.scalar()
            st.session_state.events_saved_session += 1
            st.session_state.last_event_id = int(new_id)
            st.session_state.shifts_saved_for_event = 0
            st.session_state.last_event_defaults = {
                "venue": venue.strip(),
                "location": location.strip(),
                "contact_info": contact_info.strip(),
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
                "weather": weather.strip(),
            }
            # Clear identity for next form; keep dates/times for same-day batching
            st.session_state.clear_evt_identity = True
            st.session_state.flash_event_saved = (
                f"Saved EVT-{new_id:04d}. Add staff below, then start the next form."
            )
            st.rerun()

    flash = st.session_state.pop("flash_event_saved", None)
    if flash:
        st.success(flash)

    # ── Staff for this event (same tab) ─────────────────────────────────────
    st.markdown("##### Staff on this event")
    events_df = load_events_for_picker()
    roster_df = load_staff_roster()

    if events_df.empty:
        st.info("Submit an event above first, then add staff here.")
    else:
        # Default to the event just saved
        event_ids = list(events_df["id"])
        default_ix = 0
        if st.session_state.last_event_id in event_ids:
            default_ix = event_ids.index(st.session_state.last_event_id)

        pick_col, hint_col = st.columns([3, 2])
        with pick_col:
            event_choice = st.selectbox(
                "Event",
                options=event_ids,
                index=default_ix,
                format_func=lambda eid: event_label(
                    events_df[events_df["id"] == eid].iloc[0]
                ),
                key="inline_event_pick",
            )
        with hint_col:
            st.caption(
                f"Shifts added for current pick this round: "
                f"**{st.session_state.shifts_saved_for_event}**"
            )

        # Quick-add someone to roster without leaving the tab
        with st.expander("Person not on roster? Quick-add name", expanded=False):
            with st.form("quick_roster_form", clear_on_submit=True, border=False):
                q1, q2, q3 = st.columns([2, 1, 1])
                with q1:
                    q_name = st.text_input("Name *")
                with q2:
                    q_rate = st.number_input("Rate $/hr", min_value=0.0, step=0.5, format="%.2f")
                with q3:
                    st.write("")
                    st.write("")
                    q_go = st.form_submit_button("Add to roster", use_container_width=True)
            if q_go:
                if not q_name.strip():
                    st.error("Name required.")
                else:
                    try:
                        run_write(
                            "insert into staff (name, default_pay_rate, phone) "
                            "values (:name, :rate, :phone)",
                            {"name": q_name.strip(), "rate": q_rate or None, "phone": ""},
                        )
                        st.success(f"Added {q_name.strip()} — they appear in the list below.")
                        st.rerun()
                    except Exception as e:
                        if "unique" in str(e).lower():
                            st.error("Already on the roster.")
                        else:
                            st.error(str(e))

        roster_df = load_staff_roster()
        if roster_df.empty:
            st.warning("Roster is empty — use quick-add above, or the Staff Roster tab.")
        else:
            # Prefill times from last event defaults when available
            dflt = st.session_state.last_event_defaults or {}
            with st.form("inline_staff_shift_form", clear_on_submit=True, border=False):
                r1, r2 = st.columns([1.4, 1.6])
                with r1:
                    staff_choice = st.selectbox(
                        "Staff *",
                        options=list(roster_df["id"]),
                        format_func=lambda sid: roster_df[roster_df["id"] == sid].iloc[0][
                            "name"
                        ],
                    )
                with r2:
                    selected_rate = roster_df[roster_df["id"] == staff_choice].iloc[0][
                        "default_pay_rate"
                    ]
                    st.caption(
                        f"Default rate: "
                        f"${float(selected_rate):.2f}/hr"
                        if selected_rate
                        else "No default rate"
                    )

                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    shift_start_d = st.date_input(
                        "Start date *",
                        value=dflt.get("start_d", date.today()),
                        format=DATE_FMT,
                        key="inline_shift_start_d",
                    )
                with c2:
                    shift_start_t = st.time_input(
                        "Start *",
                        value=dflt.get("start_t", dtime(9, 0)),
                        key="inline_shift_start_t",
                    )
                with c3:
                    shift_end_d = st.date_input(
                        "End date *",
                        value=dflt.get("end_d", dflt.get("start_d", date.today())),
                        format=DATE_FMT,
                        key="inline_shift_end_d",
                        help="Defaults to the event start/end; change only if needed.",
                    )
                with c4:
                    shift_end_t = st.time_input(
                        "End *",
                        value=dflt.get("end_t", dtime(17, 0)),
                        key="inline_shift_end_t",
                    )

                shift_start = combine_dt(shift_start_d, shift_start_t)
                shift_end = combine_dt(shift_end_d, shift_end_t)
                hours_preview = max(
                    (shift_end - shift_start).total_seconds() / 3600.0, 0
                )
                if selected_rate and hours_preview > 0:
                    st.caption(
                        f"Suggested ${hours_preview * float(selected_rate):.2f} "
                        f"({hours_preview:.1f}h)"
                    )

                p1, p2 = st.columns(2)
                with p1:
                    amount_paid = st.number_input(
                        "Amount paid $ *", min_value=0.0, step=1.0, format="%.2f"
                    )
                with p2:
                    paid = st.radio("Paid?", ["Yes", "No"], horizontal=True)

                submitted_shift = st.form_submit_button(
                    "Add staff shift", type="primary", use_container_width=True
                )

            if submitted_shift:
                if shift_end <= shift_start:
                    st.error("End must be after start.")
                else:
                    run_write(
                        """
                        insert into staff_shifts
                            (event_id, staff_id, start_at, end_at, amount_paid, paid)
                        values
                            (:event_id, :staff_id, :start_at, :end_at, :amount_paid, :paid)
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
                    if int(event_choice) == st.session_state.last_event_id:
                        st.session_state.shifts_saved_for_event += 1
                    else:
                        st.session_state.last_event_id = int(event_choice)
                        st.session_state.shifts_saved_for_event = 1
                    staff_name = roster_df[roster_df["id"] == staff_choice].iloc[0]["name"]
                    st.success(
                        f"Added {staff_name} to EVT-{int(event_choice):04d}. "
                        "Add another, or fill the next event above."
                    )

# === TAB — Staff Roster =====================================================
with tab_roster:
    st.caption("Add once — then pick from the shift dropdown.")
    with st.form("roster_form", clear_on_submit=True, border=False):
        rc1, rc2, rc3, rc4 = st.columns([2, 1.2, 1.4, 1])
        with rc1:
            staff_name = st.text_input("Staff name *")
        with rc2:
            default_rate = st.number_input(
                "Rate $/hr", min_value=0.0, step=0.5, format="%.2f"
            )
        with rc3:
            phone = st.text_input("Phone")
        with rc4:
            st.write("")
            st.write("")
            roster_submitted = st.form_submit_button(
                "Add", type="primary", use_container_width=True
            )

    if roster_submitted:
        if not staff_name.strip():
            st.error("Name is required.")
        else:
            try:
                run_write(
                    "insert into staff (name, default_pay_rate, phone) "
                    "values (:name, :rate, :phone)",
                    {
                        "name": staff_name.strip(),
                        "rate": default_rate or None,
                        "phone": phone.strip(),
                    },
                )
                st.success(f"Added {staff_name.strip()}.")
            except Exception as e:
                if "unique" in str(e).lower():
                    st.error(f"'{staff_name.strip()}' is already on the roster.")
                else:
                    st.error(f"Couldn't save: {e}")

    st.dataframe(load_staff_roster(), use_container_width=True, hide_index=True)

# === TAB 4 — Browse / Fix =====================================================
with tab_browse:
    st.caption("Scan recent rows and fix mistakes.")

    events_view = fetch_df(
        """
        select id, venue, event_date, start_at, end_at, rent,
               setup_types, power_provided, water_access,
               competition_present, competition_count,
               eftpos, cash, food, drinks, other_sales,
               weather_summary, square_note, notes
        from events
        order by coalesce(start_at, event_date::timestamptz) desc, id desc
        limit 500
        """
    )
    st.dataframe(events_view.pipe(format_df_dates_au), use_container_width=True, hide_index=True, height=260)

    if not events_view.empty:
        with st.expander("Fix an event", expanded=False):
            fix_id = st.selectbox(
                "Event",
                options=list(events_view["id"]),
                format_func=lambda eid: event_label(
                    events_view[events_view["id"] == eid].iloc[0]
                ),
                key="fix_event_id",
            )
            row = events_view[events_view["id"] == fix_id].iloc[0]
            with st.form("fix_event_form", border=False):
                fx1, fx2, fx3 = st.columns(3)
                with fx1:
                    f_venue = st.text_input("Venue", value=str(row["venue"] or ""))
                    f_rent = st.number_input(
                        "Rent $",
                        min_value=0.0,
                        step=1.0,
                        format="%.2f",
                        value=float(row["rent"] or 0),
                    )
                with fx2:
                    f_eftpos = st.number_input(
                        "Eftpos $",
                        min_value=0.0,
                        step=1.0,
                        format="%.2f",
                        value=float(row["eftpos"] or 0),
                    )
                    f_cash = st.number_input(
                        "Cash $",
                        min_value=0.0,
                        step=1.0,
                        format="%.2f",
                        value=float(row["cash"] or 0),
                    )
                    f_food = st.number_input(
                        "Food $",
                        min_value=0.0,
                        step=1.0,
                        format="%.2f",
                        value=float(row["food"] or 0),
                    )
                with fx3:
                    f_drinks = st.number_input(
                        "Drinks $",
                        min_value=0.0,
                        step=1.0,
                        format="%.2f",
                        value=float(row["drinks"] or 0),
                    )
                    f_other = st.number_input(
                        "Other $",
                        min_value=0.0,
                        step=1.0,
                        format="%.2f",
                        value=float(row["other_sales"] or 0),
                    )
                    f_weather = st.text_input(
                        "Weather", value=str(row["weather_summary"] or "")
                    )
                f_square = st.text_input(
                    "Square note", value=str(row["square_note"] or "")
                )
                f_notes = st.text_input("Notes", value=str(row["notes"] or ""))
                fix_submit = st.form_submit_button(
                    "Save corrections", type="primary", use_container_width=True
                )

            if fix_submit:
                run_write(
                    """
                    update events set
                        venue = :venue, rent = :rent,
                        eftpos = :eftpos, cash = :cash, food = :food,
                        drinks = :drinks, other_sales = :other_sales,
                        notes = :notes, square_note = :square_note,
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
                st.success(f"Updated EVT-{int(fix_id):04d}.")

        with st.expander("Delete an event (trial / mistake)", expanded=False):
            st.caption(
                "Permanently removes the event and any staff shifts linked to it."
            )
            del_id = st.selectbox(
                "Event to delete",
                options=list(events_view["id"]),
                format_func=lambda eid: event_label(
                    events_view[events_view["id"] == eid].iloc[0]
                ),
                key="delete_event_id",
            )
            shift_count = fetch_df(
                "select count(*) as n from staff_shifts where event_id = :id",
                {"id": int(del_id)},
            ).iloc[0]["n"]
            st.warning(
                f"This will delete **EVT-{int(del_id):04d}**"
                + (f" and **{int(shift_count)}** staff shift(s)." if int(shift_count) else ".")
            )
            confirm = st.checkbox(
                f"I understand — permanently delete EVT-{int(del_id):04d}",
                key="delete_event_confirm",
            )
            if st.button(
                "Delete event",
                type="primary",
                disabled=not confirm,
                use_container_width=True,
                key="delete_event_btn",
            ):
                run_write("delete from events where id = :id", {"id": int(del_id)})
                if st.session_state.last_event_id == int(del_id):
                    st.session_state.last_event_id = None
                    st.session_state.shifts_saved_for_event = 0
                st.success(f"Deleted EVT-{int(del_id):04d}.")
                st.rerun()

    st.caption("Staff shifts")
    shifts_view = fetch_df(
        "select * from staff_shifts_view "
        "order by event_date desc nulls last, id desc limit 500"
    )
    st.dataframe(
        shifts_view.pipe(format_df_dates_au),
        use_container_width=True,
        hide_index=True,
        height=220,
    )

# === TAB 5 — Export ===========================================================
with tab_export:
    st.caption("Download Events + Staff Shifts as Excel.")
    if st.button("Build Excel file", type="primary"):
        events_export = fetch_df(
            """
            select *
            from events
            order by coalesce(start_at, event_date::timestamptz), id
            """
        )
        shifts_export = fetch_df(
            "select * from staff_shifts_view order by event_date nulls last, id"
        )
        events_export = strip_tz_for_excel(events_export)
        shifts_export = strip_tz_for_excel(shifts_export)
        events_export = format_df_dates_au(events_export)
        shifts_export = format_df_dates_au(shifts_export)

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
            use_container_width=True,
        )
        st.success(
            f"{len(events_export)} events · {len(shifts_export)} staff shifts"
        )
