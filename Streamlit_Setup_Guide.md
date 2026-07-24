# Event & Staff Tracker — Streamlit + Supabase setup

A relational database (Postgres, via a new Supabase project) with a Streamlit form on top, and a one-click "export everything to an Excel workbook" button whenever you want a spreadsheet copy.

## 1. Create a new Supabase project

Go to [supabase.com](https://supabase.com), **New project** (separate from anything else — this is its own small database, just for this tool). Note the database password you set.

## 2. Create the tables

In the Supabase dashboard: **SQL Editor > New query**, paste the contents of `schema.sql`, click **Run**. This creates `events`, `staff`, `staff_shifts`, and a `staff_shifts_view` that joins them together for easy reading/export.

## 3. Get your connection string

**Project Settings > Database > Connection string > Session pooler tab** (this one works from anywhere, including Streamlit Cloud — the direct connection string often doesn't). Copy the URI. It looks like:

```
postgresql://postgres.xxxxxxxx:[YOUR-PASSWORD]@aws-x-xx-xxxx-x.pooler.supabase.com:5432/postgres
```

Replace `[YOUR-PASSWORD]` with your real password, and change `postgresql://` to `postgresql+psycopg2://` at the start (SQLAlchemy needs the driver named explicitly).

## 4. Set up secrets

Copy `secrets.toml.example` to `.streamlit/secrets.toml` (create the `.streamlit` folder if it doesn't exist) and paste your connection string in. **Never commit this file to git** — add `.streamlit/secrets.toml` to `.gitignore`.

## 5. Run it locally to test

```bash
pip install -r requirements.txt
streamlit run app.py
```

Try each tab: add a staff member, submit an event, add a staff shift against it, browse the records, then build an Excel export and check it downloads correctly.

## 6. Publish on Streamlit Community Cloud

1. Push this folder to a GitHub repo (make sure `.streamlit/secrets.toml` is gitignored — you'll paste secrets separately, not commit them).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in, **New app**, point it at your repo, set the main file to `app.py`.
3. Before or after deploying: **App settings > Secrets**, paste the same `[connections.supabase_db]` block from your local `secrets.toml`.
4. Deploy. You'll get a shareable `*.streamlit.app` URL — that's what you hand to whoever's digitizing paper forms.

## What's in each tab

- **New Event Report** — the main paper-form digitizing screen. Validates required fields, that end time is after start time, that at least one setup type is picked, and that Food+Drinks+Other adds up to Eftpos+Cash (same balance check we used in the Excel/Sheets versions).
- **Add Staff Shift** — pick an event and a staff member from dropdowns (typed once, never retyped), enter times and what was actually paid. Shows a "suggested pay" hint based on their roster rate, purely informational.
- **Staff Roster** — add each staff member once; they then show up in the Add Staff Shift dropdown.
- **Browse** — read-only tables of everything entered so far, useful for a quick sanity check without leaving the app.
- **Export to Excel** — pulls every event and every staff shift (joined with venue/date/staff name so it's readable on its own) into a two-sheet `.xlsx` and gives you a download button. This answers the "relational data as an Excel workbook" question directly — Events and Staff Shifts are separate sheets, linked by `event_id`.

## Notes

- Every record gets a stable numeric ID; the app displays events as `EVT-0001` etc. by formatting that ID, so there's nothing to type or that can collide.
- This is intentionally a separate, self-contained tool — it does not touch the UrbanTree BOS database or the mobile app work. If you later want this data inside BOS proper, the Excel export is the bridge: it's the same two-sheet Events/Staff shape as everything we built for Excel earlier, so it could be imported into BOS's existing legacy-import flow.
