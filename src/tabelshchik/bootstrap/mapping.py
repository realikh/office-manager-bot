"""Turning validated config into the plain objects the use cases take.

This is the only place that knows both shapes. Keeping it here is what lets every use
case be exercised in a test without a config file, and lets the YAML format change
without touching the rules.
"""

from __future__ import annotations

from datetime import time

from tabelshchik.application.policy import SchedulePolicy, SilentPolicy
from tabelshchik.application.voice import Catalog, CommonText, MoodPolicy
from tabelshchik.config.messages import MessagesConfig, MoodVariants
from tabelshchik.config.models import AppConfig
from tabelshchik.domain.mood import Mood

ALL_DAY = (time(0, 0), time(0, 0))


def schedule_policy(config: AppConfig) -> SchedulePolicy:
    section = config.schedule
    return SchedulePolicy(
        horizon_weeks=section.horizon_weeks,
        freeze_weeks=section.freeze_weeks,
        max_days_per_week=section.max_days_per_week,
        surplus_clamp_days=section.surplus_clamp,
        absence_policy=section.absence_policy,
    )


def silent_policy(config: AppConfig) -> SilentPolicy:
    windows: list[tuple[time, time] | None] = []
    for weekday in range(7):
        rule = config.silent_hours.rule_for(weekday)
        if rule is None:
            windows.append(None)
        elif rule == "all-day":
            windows.append(ALL_DAY)
        else:
            windows.append((rule.start, rule.end))
    return SilentPolicy(windows=tuple(windows))


def mood_policy(config: AppConfig) -> MoodPolicy:
    section = config.personality
    return MoodPolicy(
        enabled=section.enabled,
        weights={Mood(name): weight.weight for name, weight in section.moods.items()},
        safe_mode=section.safe_mode,
        banned_terms=tuple(section.banned_terms),
    )


def catalog(messages: MessagesConfig) -> Catalog:
    common = CommonText(
        weekdays=tuple(messages.common.weekdays),
        weekdays_short=tuple(messages.common.weekdays_short),
        months=tuple(messages.common.months),
        tomorrow=messages.common.tomorrow,
        on_weekday=messages.common.on_weekday,
        office_header=messages.common.office_header,
    )

    variants = {
        "attendance.intro": _spread(messages.attendance.intro),
        "attendance.tails": _spread(messages.attendance.tails),
        "attendance.emojis": _spread(messages.attendance.emojis),
        "attendance.empty": _spread(messages.attendance.empty),
        "tempo.weekly": _spread(messages.tempo.weekly),
        "tempo.monthWarning": _spread(messages.tempo.month_warning),
        "tempo.monthEnd": _spread(messages.tempo.month_end),
        "ai.rateLimited": _spread(messages.ai.rate_limited),
        "ai.failed": _spread(messages.ai.failed),
    }

    gendered = {
        "attendance.epithets": {
            mood: {
                gender: tuple(messages.attendance.epithets.for_mood(mood).for_gender(gender))
                for gender in ("male", "female")
            }
            for mood in Mood
        },
    }

    per_mood = {
        "ai.persona": {mood: messages.ai.persona.for_mood(mood) for mood in Mood},
    }

    plain = {
        "attendance.correction": messages.attendance.correction,
        "ai.disabled": messages.ai.disabled,
        "tempo.url": messages.tempo.url,
        "schedule.caption": messages.schedule.caption,
        "schedule.noChanges": messages.schedule.no_changes,
        "schedule.shortfallNote": messages.schedule.shortfall_note,
        "schedule.weekHeader": messages.schedule.week_header,
    }

    return Catalog(
        common=common,
        variants=variants,
        gendered=gendered,
        per_mood=per_mood,
        plain=plain,
    )


def _spread(source: MoodVariants) -> dict[Mood, tuple[str, ...]]:
    return {mood: tuple(source.for_mood(mood)) for mood in Mood}
