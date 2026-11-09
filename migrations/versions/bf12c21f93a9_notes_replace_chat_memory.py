"""Notes replace chat memory

Revision ID: bf12c21f93a9
Revises: 47b7acff4a10
Create Date: 2026-11-09 16:36:05.951571

"""

import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "bf12c21f93a9"
down_revision: Union[str, Sequence[str], None] = "47b7acff4a10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TOPIC_CHARS = 40
_WORD = re.compile(r"[\w']+")

CHAT_MEMORY = sa.table(
    "chat_memory",
    sa.column("id", sa.Integer),
    sa.column("scope", sa.String),
    sa.column("subject", sa.String),
    sa.column("fact", sa.Text),
    sa.column("at", sa.DateTime),
)
EMPLOYEE = sa.table(
    "employee",
    sa.column("id", sa.String),
    sa.column("telegram_user_id", sa.Integer),
)
MEMORY_NOTE = sa.table(
    "memory_note",
    sa.column("scope", sa.String),
    sa.column("subject", sa.String),
    sa.column("topic", sa.String),
    sa.column("text", sa.Text),
    sa.column("until", sa.Date),
    sa.column("created_at", sa.DateTime),
    sa.column("updated_at", sa.DateTime),
    sa.column("touched_at", sa.DateTime),
)


def _topic(fact: str) -> str:
    topic = ""
    for word in _WORD.findall(fact.casefold()):
        candidate = f"{topic} {word}".strip()
        if len(candidate) > TOPIC_CHARS:
            break
        topic = candidate
    return topic


def _copy_chat_memory() -> None:
    bind = op.get_bind()
    linked = {
        employee_id: user_id
        for employee_id, user_id in bind.execute(
            sa.select(EMPLOYEE.c.id, EMPLOYEE.c.telegram_user_id).where(
                EMPLOYEE.c.telegram_user_id.is_not(None)
            )
        )
    }
    rows = bind.execute(
        sa.select(
            CHAT_MEMORY.c.scope, CHAT_MEMORY.c.subject, CHAT_MEMORY.c.fact, CHAT_MEMORY.c.at
        ).order_by(CHAT_MEMORY.c.id)
    ).all()

    kept: dict[tuple[str, str, str], dict[str, object]] = {}
    for scope, subject, fact, at in rows:
        if scope == "office":
            target = ("office", subject)
        elif scope == "employee" and subject in linked:
            target = ("person", str(linked[subject]))
        else:
            continue
        topic = _topic(fact)
        if not topic:
            continue
        kept[(*target, topic)] = {
            "scope": target[0],
            "subject": target[1],
            "topic": topic,
            "text": fact,
            "until": None,
            "created_at": at,
            "updated_at": at,
            "touched_at": at,
        }
    if kept:
        op.bulk_insert(MEMORY_NOTE, list(kept.values()))


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "memory_note",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "scope",
            sa.Enum("person", "office", name="notescope", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("subject", sa.String(length=64), nullable=False),
        sa.Column("topic", sa.String(length=48), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("until", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("touched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "subject", "topic", name="uq_memory_note_topic"),
    )
    with op.batch_alter_table("memory_note", schema=None) as batch_op:
        batch_op.create_index(
            "ix_memory_note_touched", ["scope", "subject", "touched_at"], unique=False
        )

    _copy_chat_memory()

    with op.batch_alter_table("chat_memory", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_chat_memory_scope_subject"))

    op.drop_table("chat_memory")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "chat_memory",
        sa.Column("id", sa.INTEGER(), nullable=False),
        sa.Column("scope", sa.VARCHAR(length=16), nullable=False),
        sa.Column("subject", sa.VARCHAR(length=64), nullable=False),
        sa.Column("fact", sa.TEXT(), nullable=False),
        sa.Column("at", sa.DATETIME(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("chat_memory", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_chat_memory_scope_subject"), ["scope", "subject", "id"], unique=False
        )

    with op.batch_alter_table("memory_note", schema=None) as batch_op:
        batch_op.drop_index("ix_memory_note_touched")

    op.drop_table("memory_note")
