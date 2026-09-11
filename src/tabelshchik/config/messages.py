"""Schema for config/messages.yaml.

Every user-facing string lives in that file, not in Python source, so the bot's voice can
be rewritten without touching code — and so the five moods can be checked for coverage at
startup instead of discovering a missing one at 15:30 on a Friday.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from tabelshchik.config.models import Base
from tabelshchik.domain.mood import Mood


class MoodVariants(Base):
    """Several hand-written lines per mood. One is chosen stably for the day.

    All five moods are required: a silent fallback to a default voice would make a
    missing mood invisible until it was needed.
    """

    toxic: list[str] = Field(min_length=1)
    fun: list[str] = Field(min_length=1)
    happy: list[str] = Field(min_length=1)
    sad: list[str] = Field(min_length=1)
    depressive: list[str] = Field(min_length=1)

    def for_mood(self, mood: Mood) -> list[str]:
        return list(getattr(self, mood.value))

    def require_tokens(self, *tokens: str) -> None:
        for mood in Mood:
            for line in self.for_mood(mood):
                missing = [token for token in tokens if token not in line]
                if missing:
                    raise ValueError(f"{mood.value} line is missing {missing}: {line!r}")


class MoodText(Base):
    """A single line per mood, for things that are not picked from variants."""

    toxic: str
    fun: str
    happy: str
    sad: str
    depressive: str

    def for_mood(self, mood: Mood) -> str:
        return str(getattr(self, mood.value))


class AttendanceMessages(Base):
    #: Must carry {date} and {office}; the roster is appended below it.
    intro: MoodVariants
    #: When the next working day has nobody on it.
    empty: MoodVariants
    #: When an already-announced day changes.
    correction: str
    header_separator: str = ""

    @model_validator(mode="after")
    def intro_lines_carry_their_tokens(self) -> AttendanceMessages:
        self.intro.require_tokens("{date}", "{office}")
        self.empty.require_tokens("{date}")
        return self


class TempoMessages(Base):
    url: str = ""
    weekly: MoodVariants
    month_warning: MoodVariants
    month_end: MoodVariants


class AiMessages(Base):
    #: Appended to the system prompt to set the voice.
    persona: MoodText
    rate_limited: MoodVariants
    failed: MoodVariants
    disabled: str


class ScheduleMessages(Base):
    caption: str
    no_changes: str
    shortfall_note: str
    week_header: str


class CommonMessages(Base):
    weekdays: list[str] = Field(min_length=7, max_length=7)
    weekdays_short: list[str] = Field(min_length=7, max_length=7)
    months: list[str] = Field(min_length=12, max_length=12)
    tomorrow: str
    on_weekday: str


class MessagesConfig(Base):
    common: CommonMessages
    attendance: AttendanceMessages
    tempo: TempoMessages
    ai: AiMessages
    schedule: ScheduleMessages
