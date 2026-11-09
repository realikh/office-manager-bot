from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tabelshchik.application.ports import OfficeStore
from tabelshchik.domain.entities import Office


@dataclass(frozen=True, slots=True)
class Destination:
    chat_id: int
    #: Offices sharing one group are one destination: a post there is read by all of them,
    #: and offering it twice would invite sending it twice.
    offices: tuple[Office, ...]

    @property
    def office_id(self) -> str:
        """What the button carries. Any of them resolves back to this same group."""
        return self.offices[0].id

    @property
    def label(self) -> str:
        return " · ".join(office.name for office in self.offices)


def destinations(offices: OfficeStore) -> list[Destination]:
    """Every open office's group, once per group, in the order the offices are listed.

    Open only: a closed office answers nobody, and its group is no exception.
    """
    grouped: dict[int, list[Office]] = {}
    for office in offices.active_offices():
        if office.chat_id is not None:
            grouped.setdefault(office.chat_id, []).append(office)
    return [
        Destination(chat_id=chat_id, offices=tuple(members)) for chat_id, members in grouped.items()
    ]


def destination_of(offices: OfficeStore, office_id: str) -> Destination | None:
    return next(
        (
            target
            for target in destinations(offices)
            if any(office.id == office_id for office in target.offices)
        ),
        None,
    )


def speaker(destination: Destination, due: Callable[[Office], bool]) -> Office | None:
    return next((office for office in destination.offices if due(office)), None)
