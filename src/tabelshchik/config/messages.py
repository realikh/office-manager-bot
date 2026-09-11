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

    def forbid_braces(self) -> None:
        """For lines that are substituted *into* a template rather than rendered.

        A stray brace in one of these would survive into the final text, where the
        rendering guards treat it as a failed substitution and throw the whole line away.
        """
        for mood in Mood:
            for line in self.for_mood(mood):
                if "{" in line or "}" in line:
                    raise ValueError(f"{mood.value} line must not contain braces: {line!r}")


class GenderedVariants(Base):
    """Lines that have to agree in gender with the person they describe."""

    male: list[str] = Field(min_length=1)
    female: list[str] = Field(min_length=1)

    def for_gender(self, gender: str) -> list[str]:
        return list(self.female if gender == "female" else self.male)


class MoodGendered(Base):
    """A gendered set per mood. All five moods required, as everywhere else."""

    toxic: GenderedVariants
    fun: GenderedVariants
    happy: GenderedVariants
    sad: GenderedVariants
    depressive: GenderedVariants

    def for_mood(self, mood: Mood) -> GenderedVariants:
        value = getattr(self, mood.value)
        assert isinstance(value, GenderedVariants)
        return value


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
    """The daily reminder.

    The division of labour here is the whole point. Everything factual — the weekday, the
    date, the office, who is listed — is substituted by us into `intro`. The model only
    ever supplies `{tail}` and an epithet, neither of which may contain a fact. That is
    what makes "В понедельник В понедельник" impossible rather than merely unlikely.
    """

    #: Must carry {when}, {date}, {office} and {tail}; the roster is appended below it.
    intro: MoodVariants
    #: Fallback flavour for {tail} when the model is off, slow, or produced something the
    #: guards rejected. Plain text: no tokens, or the substitution would be visible.
    tails: MoodVariants
    #: Fallback epithets, by mood and by gender, one per roster line.
    epithets: MoodGendered
    #: Decoration in front of each roster line. Ours, never the model's.
    emojis: MoodVariants
    #: When the next working day has nobody on it.
    empty: MoodVariants
    #: When an already-announced day changes.
    correction: str
    header_separator: str = ""

    @model_validator(mode="after")
    def intro_lines_carry_their_tokens(self) -> AttendanceMessages:
        self.intro.require_tokens("{when}", "{date}", "{office}", "{tail}")
        self.empty.require_tokens("{when}", "{date}")
        self.tails.forbid_braces()
        self.emojis.forbid_braces()
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
    office_header: str = "🏢 <b>{office}</b>"


class MessagesConfig(Base):
    common: CommonMessages
    attendance: AttendanceMessages
    tempo: TempoMessages
    ai: AiMessages
    schedule: ScheduleMessages
