"""A plausible university week for a new demo account, dated relative to the day it is created.

Past occurrences are marked done (one is missed a week ago) and their points are written to the ledger here, because
the midnight points job skips demo accounts — so the Points tab shows real totals and a streak from the first minute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import CompletionMethod, EventInstanceStatus, EventType, Importance
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.models.important_date_range import ImportantDateRange
from app.models.location import Location
from app.models.user import User
from app.schemas.event import NewEvent
from app.services.draft_rules import first_occurrence
from app.services.event_service import build_event
from app.services.points import recalculate_points_since

TERM_NAME = "Fall term"
TERM_STARTS_DAYS_AGO = 7
TERM_LENGTH_DAYS = 70
MISSED_DAYS_AGO = 6


@dataclass(frozen=True)
class _Class:
    title: str
    rule: str
    start: time
    minutes: int
    importance: Importance
    location: str | None = None


CLASSES: tuple[_Class, ...] = (
    _Class("MAT235 Lecture", "FREQ=WEEKLY;BYDAY=MO,WE,FR", time(10), 60, Importance.OFFICIAL, "Bahen Centre"),
    _Class("ECE244 Lecture", "FREQ=WEEKLY;BYDAY=TU,TH", time(13), 90, Importance.OFFICIAL),
    _Class("PHY180 Lecture", "FREQ=WEEKLY;BYDAY=TU,TH", time(9), 60, Importance.OFFICIAL),
    _Class("ECE244 Lab", "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE", time(14), 180, Importance.MUST, "Myhal Centre"),
    _Class("MAT235 Tutorial", "FREQ=WEEKLY;BYDAY=TH", time(16), 60, Importance.OBLIGATION_NO_CHECK),
)

# (title, days from today, due time, importance)
DEADLINES: tuple[tuple[str, int, time, Importance], ...] = (
    ("PHY180 Problem Set 2", -2, time(23, 59), Importance.OFFICIAL),
    ("ECE244 Lab 3 report", 2, time(23, 59), Importance.MUST),
    ("MAT235 Problem Set 4", 4, time(17), Importance.OFFICIAL),
)

LOCATIONS: tuple[tuple[str, int], ...] = (("Bahen Centre", 15), ("Myhal Centre", 10))


def seed_demo_week(db: Session, user: User, today: date) -> None:
    """Adds the demo data to a freshly flushed demo user. Commits (the points recalculation commits per day)."""
    term = ImportantDateRange(
        user_id=user.id,
        name=TERM_NAME,
        start_date=today - timedelta(days=TERM_STARTS_DAYS_AGO),
        end_date=today + timedelta(days=TERM_LENGTH_DAYS - TERM_STARTS_DAYS_AGO),
    )
    places = {name: Location(user_id=user.id, name=name, default_travel_minutes=minutes) for name, minutes in LOCATIONS}
    db.add_all([term, *places.values()])
    db.flush()

    for item in CLASSES:
        first_day = first_occurrence(item.rule, item.start, term.start_date, term.end_date)
        start = datetime.combine(first_day, item.start)
        build_event(
            db,
            NewEvent(
                user_id=user.id,
                title=item.title,
                start_time=start,
                end_time=start + timedelta(minutes=item.minutes),
                importance=item.importance,
                is_recurring=True,
                recurrence_rule=item.rule,
                date_range_id=term.id,
                location_id=places[item.location].id if item.location else None,
            ),
        )

    for title, days, due, importance in DEADLINES:
        build_event(
            db,
            NewEvent(
                user_id=user.id,
                title=title,
                event_type=EventType.DEADLINE,
                end_time=datetime.combine(today + timedelta(days=days), due),
                importance=importance,
            ),
        )

    _mark_past_occurrences(db, user.id, today)
    db.commit()
    recalculate_points_since(db, user.id, term.start_date, today)


def _mark_past_occurrences(db: Session, user_id: int, today: date) -> None:
    """Everything before today is done, except the first occurrence on MISSED_DAYS_AGO — so the streak starts after
    it, like a real week."""
    missed_day = today - timedelta(days=MISSED_DAYS_AGO)
    past = db.execute(
        select(EventInstance)
        .join(Event, EventInstance.event_id == Event.id)
        .where(Event.user_id == user_id, EventInstance.date < today)
        .order_by(EventInstance.date, EventInstance.id)
    ).scalars()
    missed_one = False
    for instance in past:
        if instance.date == missed_day and not missed_one and instance.event.parent_event_id is None:
            instance.status = EventInstanceStatus.MISSED
            missed_one = True
            continue
        instance.status = EventInstanceStatus.DONE
        instance.completion_method = CompletionMethod.MANUAL
