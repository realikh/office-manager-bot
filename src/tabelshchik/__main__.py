"""Command line entry points.

``run`` is what the container executes. Everything else exists so the interesting parts
can be exercised without a Telegram token: validate the config, preview a schedule,
simulate a year of fairness, or boot the whole thing and send nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from tabelshchik.adapters.fakes import RecordingNotifier
from tabelshchik.adapters.reports.xlsx import build_workbook, upcoming_week
from tabelshchik.application.build_report import ReportDay, build_report
from tabelshchik.application.regenerate_schedule import regenerate
from tabelshchik.application.send_attendance_reminder import send_attendance_reminder
from tabelshchik.bootstrap.container import Services, build_services
from tabelshchik.bootstrap.logging import configure_logging
from tabelshchik.bootstrap.settings import config_dir, database_path, load_secrets
from tabelshchik.config.loader import ConfigError, load


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tabelshchik", description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--json-logs", action="store_true")

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run the bot")
    commands.add_parser("validate", help="check the configuration and exit")

    regen = commands.add_parser("regenerate", help="rebuild a schedule")
    regen.add_argument("--office", required=True)
    regen.add_argument("--out", type=Path, default=Path("out"))

    prev = commands.add_parser("preview", help="render a schedule and reminder, send nothing")
    prev.add_argument("--office", required=True)
    prev.add_argument("--out", type=Path, default=Path("out"))

    sim = commands.add_parser("simulate", help="run the long-run fairness simulation")
    sim.add_argument("--weeks", type=int, default=52)
    sim.add_argument("--employees", type=int, default=12)
    sim.add_argument("--prune", action="store_true")

    dry = commands.add_parser("dry-run", help="boot everything and send nothing")
    dry.add_argument("--office", default=None)

    args = parser.parse_args(argv)
    configure_logging(args.log_level, json_output=args.json_logs)

    handlers = {
        "run": _run,
        "validate": _validate,
        "regenerate": _regenerate,
        "preview": _preview,
        "simulate": _simulate,
        "dry-run": _dry_run,
    }
    return handlers[args.command](args)


def _services(args: argparse.Namespace, *, notifier: object = None) -> Services:
    return build_services(
        config_dir=args.config or config_dir(),
        database_path=args.db or database_path(),
        secrets=load_secrets(),
        notifier=notifier,  # type: ignore[arg-type]
    )


def _run(args: argparse.Namespace) -> int:
    from tabelshchik.bootstrap.lifespan import run_bot

    secrets = load_secrets()
    if not secrets.has_telegram:
        print("TELEGRAM_BOT_TOKEN is not set.", file=sys.stderr)
        return 2

    services = _services(args)
    if not services.admin_ids:
        print("warning: no admins configured — /admin will be unreachable", file=sys.stderr)

    try:
        asyncio.run(run_bot(services))
    except KeyboardInterrupt:
        print("stopped")
    return 0


def _validate(args: argparse.Namespace) -> int:
    directory = args.config or config_dir()
    try:
        loaded = load(directory)
    except ConfigError as error:
        print(f"✖ {error}", file=sys.stderr)
        return 1

    print(f"✓ configuration in {directory} is valid")
    for office in loaded.offices:
        chat = office.chat_id if office.chat_id is not None else "— not set"
        desks = sum(office.schedule.vacant_desks.values())
        fixed = sum(len(ids) for ids in office.schedule.fixed.values())
        print(
            f"  {office.id}: {len(office.employees)} employees, "
            f"{desks} vacant desks/week, {fixed} fixed slots, chat {chat}"
        )
        if office.chat_id is None:
            print("    warning: no chatId, so this office cannot be messaged")
    return 0


def _regenerate(args: argparse.Namespace) -> int:
    services = _services(args)
    result = regenerate(
        office_id=args.office,
        offices=services.offices,
        schedule=services.schedule,
        ledger=services.ledger,
        clock=services.clock,
        policy=services.schedule_policy,
        triggered_by="cli",
    )
    print(
        f"{args.office}: {result.horizon_start} — {result.horizon_end}; "
        f"+{len(result.diff.added)} −{len(result.diff.removed)} ={result.diff.kept}, "
        f"shortfall {result.shortfall}"
    )
    _write_workbook(services, args.office, result, args.out)
    return 0


def _preview(args: argparse.Namespace) -> int:
    services = _services(args, notifier=RecordingNotifier())
    result = regenerate(
        office_id=args.office,
        offices=services.offices,
        schedule=services.schedule,
        ledger=services.ledger,
        clock=services.clock,
        policy=services.schedule_policy,
        triggered_by="preview",
    )

    outcome = asyncio.run(
        send_attendance_reminder(
            office_id=args.office,
            offices=services.offices,
            schedule=services.schedule,
            notifier=services.notifier,  # type: ignore[arg-type]
            voice=services.voice,
            clock=services.clock,
            silent_policy=services.silent_policy,
            dry_run=True,
        )
    )

    print(f"--- reminder for {outcome.target} (silent: {outcome.silent}) ---")
    print(outcome.text or f"(nothing to send: {outcome.skipped})")
    print()
    _write_workbook(services, args.office, result, args.out)
    return 0


def _simulate(args: argparse.Namespace) -> int:
    from tabelshchik.domain.entities import SCALE
    from tabelshchik.domain.simulation import SimulationConfig, run

    result = run(
        SimulationConfig(
            weeks=args.weeks,
            employees=args.employees,
            retain_weeks=12 if args.prune else None,
        )
    )
    series = [value / SCALE for value in result.spread_series()]

    print(f"{args.weeks} weeks, {args.employees} employees")
    print(f"  spread now:  {series[-1]:.2f} days")
    print(f"  spread max:  {max(series):.2f} days")
    print(f"  spread mean: {sum(series) / len(series):.2f} days")
    print(f"  retained day records: {len(result.records)}")
    print()
    print("  employee     days   surplus")
    for employee_id in sorted(result.ledger):
        entry = result.ledger[employee_id]
        print(f"  {employee_id:<12} {entry.days:>4}   {entry.surplus_scaled / SCALE:+.2f}")
    return 0


def _dry_run(args: argparse.Namespace) -> int:
    """Boot everything, register the jobs, send nothing."""
    import asyncio as _asyncio

    from tabelshchik.adapters.scheduling.runner import JobRunner
    from tabelshchik.bootstrap.jobs import register_jobs

    services = _services(args, notifier=RecordingNotifier())
    runner = JobRunner(ledger=services.jobs, timezone=services.app.timezone)
    register_jobs(runner, services)

    print(f"timezone {services.app.timezone}, admins {sorted(services.admin_ids) or '—'}")
    print(f"offices: {[office.id for office in services.offices.active_offices()]}")
    print("jobs:")
    for job in runner.jobs:
        label = f"{job.name}:{job.scope}" if job.scope else job.name
        print(f"  {label:<24} {job.trigger}")

    missed = _asyncio.run(runner.catch_up(now=services.clock.now(), grace_hours=0))
    print(f"catch-up would replay: {len(missed)}")
    return 0


def _write_workbook(services: Services, office_id: str, result: object, out: Path) -> None:
    context = services.offices.planning_context(
        office_id,
        start=result.horizon_start,  # type: ignore[attr-defined]
        end=result.horizon_end,  # type: ignore[attr-defined]
    )
    days = [
        ReportDay(
            day=plan.day,
            weekday=plan.weekday,
            attendees=result.roster_on(plan.day),  # type: ignore[attr-defined]
            desks_required=plan.desks_total,
        )
        for plan in result.instance.days  # type: ignore[attr-defined]
        if plan.desks_total
    ]
    report = build_report(
        office_name=context.office.name,
        start=result.horizon_start,  # type: ignore[attr-defined]
        end=result.horizon_end,  # type: ignore[attr-defined]
        employees=context.employees,
        days=days,
        spec=context.spec,
        ledger=services.ledger.entries(office_id),
    )

    out.mkdir(parents=True, exist_ok=True)
    destination = out / f"{office_id}.xlsx"
    destination.write_bytes(build_workbook(report, services.voice.catalog.common))

    common = services.voice.catalog.common
    print(upcoming_week(report, common))
    print(
        f"\nspread {report.stats.spread}, "
        f"filled {report.stats.desks_filled}/{report.stats.desks_offered}"
    )
    print(f"workbook: {destination}")


if __name__ == "__main__":
    raise SystemExit(main())
