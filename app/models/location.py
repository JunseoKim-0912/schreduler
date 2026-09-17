from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.user import User


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    default_travel_minutes: Mapped[int] = mapped_column(Integer)

    user: Mapped["User"] = relationship(back_populates="locations")
    events: Mapped[list["Event"]] = relationship(back_populates="location")
