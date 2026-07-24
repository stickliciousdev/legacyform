-- Event & Staff Tracker — Supabase schema
-- Run this once in your Supabase project: SQL Editor > New query > paste > Run.

create table if not exists staff (
    id                serial primary key,
    name              text not null unique,
    default_pay_rate  numeric(10,2),
    phone             text,
    created_at        timestamptz not null default now()
);

create table if not exists events (
    id                    serial primary key,
    venue                 text not null,
    event_date            date not null,
    start_time            time,
    end_time              time,
    rent                  numeric(10,2) not null default 0,
    setup_types           text not null default '',   -- comma-separated: "Food Truck, Tent"
    setup_other           text not null default '',
    power_provided        boolean not null default false,
    water_access          boolean not null default false,
    competition_present   boolean not null default false,
    competition_notes     text not null default '',
    eftpos                numeric(10,2) not null default 0,
    cash                  numeric(10,2) not null default 0,
    food                  numeric(10,2) not null default 0,
    drinks                numeric(10,2) not null default 0,
    other_sales           numeric(10,2) not null default 0,
    notes                 text not null default '',
    created_at            timestamptz not null default now()
);

create table if not exists staff_shifts (
    id            serial primary key,
    event_id      integer not null references events(id) on delete cascade,
    staff_id      integer references staff(id) on delete set null,
    start_time    time,
    end_time      time,
    amount_paid   numeric(10,2) not null default 0,
    paid          boolean not null default false,
    created_at    timestamptz not null default now()
);

create index if not exists idx_staff_shifts_event on staff_shifts(event_id);
create index if not exists idx_staff_shifts_staff on staff_shifts(staff_id);

-- Handy view: staff shifts with the staff member's name and the event's venue/date
-- joined in, so exports don't need to do the join in Python.
create or replace view staff_shifts_view as
select
    ss.id,
    ss.event_id,
    e.venue,
    e.event_date,
    st.name as staff_name,
    ss.start_time,
    ss.end_time,
    round(
        case when ss.end_time > ss.start_time
             then extract(epoch from (ss.end_time - ss.start_time)) / 3600.0
             else null end,
        2
    ) as hours,
    ss.amount_paid,
    ss.paid,
    ss.created_at
from staff_shifts ss
join events e on e.id = ss.event_id
left join staff st on st.id = ss.staff_id;
