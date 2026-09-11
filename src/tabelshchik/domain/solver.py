"""The fairness solver: instance in, draft decisions out.

What gets equalised is *surplus* — days attended minus entitlement accrued — not raw day
counts. Equalising raw totals looks right and feels terrible: someone back from three
weeks away carries a large deficit and gets drafted every eligible day for a month to
catch up. Surplus makes a returner resume a normal share, and a new joiner fair on day one.

The objective is to minimise the sum of squared end-of-horizon surpluses. That is not a
proxy for fairness, it *is* the fairest assignment: the achievable draft-count vectors
form the integer points of a base polytope (day capacities are a sum of uniform-matroid
rank functions, hence submodular), and over a base polyhedron the minimiser of a sum of
strictly convex terms is the same point for every such function — the decreasingly
minimal element. So this simultaneously minimises the maximum surplus, maximises the
minimum, minimises the max-min spread, and minimises variance, Gini and every other
Schur-convex measure. Squares are chosen only because the marginal cost is cheap.

The guarantee is per-horizon, given frozen days and the current ledger. Long-run fairness
is emergent from per-window optimality plus a ledger that is never reset — which is the
thing the previous implementation got wrong.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from tabelshchik.domain.entities import SCALE
from tabelshchik.domain.instance import ScheduleInstance
from tabelshchik.domain.mcmf import Cost, MinCostFlow, zero_cost
from tabelshchik.domain.rng import jitter

SOLVER_VERSION = "1.0"

#: (fairness, shortfall spread, per-week spread, seeded jitter), most important first.
COST_DIM = 4
_FAIRNESS, _SHORTFALL, _WEEK, _JITTER = range(COST_DIM)

#: Jitter only ever breaks exact ties, so the range just needs to be wide enough to make
#: collisions rare.
JITTER_RANGE = 1 << 16


class SolverInvariantError(AssertionError):
    """A post-condition of the solve was violated. Never expected; always loud."""


@dataclass(frozen=True, slots=True)
class DraftResult:
    drafted: Mapping[date, tuple[str, ...]]
    filled: int
    shortfall: int
    objective: Cost

    def on(self, day: date) -> tuple[str, ...]:
        return self.drafted.get(day, ())


def solve(instance: ScheduleInstance, *, check: bool = True) -> DraftResult:
    """Draft people into the vacant desks of ``instance`` as fairly as possible."""
    if not instance.needs_draft:
        # A fixed-schedule-only office never builds a graph at all.
        return DraftResult(drafted={}, filled=0, shortfall=0, objective=zero_cost(COST_DIM))

    graph, index = _build_graph(instance)
    filled, objective, _potentials = graph.solve(index.source, index.sink)

    drafted: dict[date, list[str]] = {}
    for (employee_id, day), arc_id in index.day_arcs.items():
        if graph.flow_on(arc_id) > 0:
            drafted.setdefault(day, []).append(employee_id)

    result = DraftResult(
        drafted={day: tuple(sorted(names)) for day, names in sorted(drafted.items())},
        filled=filled,
        shortfall=instance.total_demand - filled,
        objective=objective,
    )

    if check:
        _assert_invariants(instance, result, graph, index)

    return result


@dataclass(frozen=True, slots=True)
class _Index:
    source: int
    sink: int
    day_arcs: Mapping[tuple[str, date], int]
    source_arcs: Mapping[str, tuple[int, ...]]


def _build_graph(instance: ScheduleInstance) -> tuple[MinCostFlow, _Index]:
    employees = instance.employees
    weeks = sorted({plan.week for plan in instance.days})
    days = [plan for plan in instance.days if plan.demand > 0 or plan.eligible]

    source, sink = 0, 1
    next_node = 2

    employee_node: dict[str, int] = {}
    for employee_id in employees:
        employee_node[employee_id] = next_node
        next_node += 1

    week_node: dict[tuple[str, int], int] = {}
    for employee_id in employees:
        for week in weeks:
            week_node[(employee_id, week)] = next_node
            next_node += 1

    day_node: dict[date, int] = {}
    for plan in days:
        day_node[plan.day] = next_node
        next_node += 1

    graph = MinCostFlow(next_node, COST_DIM)

    # Dijkstra needs non-negative arcs, but base surplus is signed, so the cheapest
    # marginal can be negative. Every unit of flow crosses exactly one source arc and
    # every maximum flow has the same value, so adding a constant to all source arcs
    # adds a constant to every candidate solution and cannot change which one wins.
    # Do not "simplify" this away.
    shift = max(
        0,
        -min(
            (2 * instance.base_scaled[employee_id] + SCALE for employee_id in employees),
            default=0,
        ),
    )

    source_arcs: dict[str, tuple[int, ...]] = {}
    day_arcs: dict[tuple[str, date], int] = {}

    for employee_id in employees:
        base = instance.base_scaled[employee_id]
        capacity = sum(instance.week_cap.get((employee_id, week), 0) for week in weeks)

        arcs: list[int] = []
        for k in range(1, capacity + 1):
            # Marginal cost of this person's k-th extra day: (base+k)^2 - (base+k-1)^2.
            # Convex and increasing, which is what makes the total cost telescope into
            # the sum of squared surpluses. Never perturb these arcs — see the jitter note.
            marginal = 2 * base + (2 * k - 1) * SCALE + shift
            cost = _cost(fairness=marginal)
            arcs.append(graph.add_arc(source, employee_node[employee_id], 1, cost))
        source_arcs[employee_id] = tuple(arcs)

        for week in weeks:
            cap = instance.week_cap.get((employee_id, week), 0)
            if cap <= 0:
                continue
            node = week_node[(employee_id, week)]
            for k in range(1, cap + 1):
                # Spreads a person's days evenly across the weeks of the horizon rather
                # than clumping them into one fortnight. The node itself carries the hard
                # weekly cap as a capacity.
                graph.add_arc(employee_node[employee_id], node, 1, _cost(week=2 * k - 1))

            for plan in days:
                if plan.week != week or employee_id not in plan.eligible:
                    continue
                # Jitter lives here, on the employee-day arcs, and never on the convex
                # source arcs: perturbing those would break the monotone marginals, the
                # telescoping identity would stop holding, and the optimality guarantee
                # would quietly evaporate. As the last cost component it can only choose
                # among already-optimal solutions.
                noise = jitter(
                    instance.seed, employee_id, plan.day.isoformat(), modulo=JITTER_RANGE
                )
                arc = graph.add_arc(node, day_node[plan.day], 1, _cost(jitter=noise))
                day_arcs[(employee_id, plan.day)] = arc

    for plan in days:
        for k in range(1, plan.demand + 1):
            # Convex too, so when there are not enough eligible people the shortfall is
            # spread (4 of 5 on both days) instead of concentrated (5 and 3).
            graph.add_arc(day_node[plan.day], sink, 1, _cost(shortfall=2 * k - 1))

    return graph, _Index(source=source, sink=sink, day_arcs=day_arcs, source_arcs=source_arcs)


def _cost(fairness: int = 0, shortfall: int = 0, week: int = 0, jitter: int = 0) -> Cost:
    components = [0] * COST_DIM
    components[_FAIRNESS] = fairness
    components[_SHORTFALL] = shortfall
    components[_WEEK] = week
    components[_JITTER] = jitter
    return tuple(components)


def _assert_invariants(
    instance: ScheduleInstance,
    result: DraftResult,
    graph: MinCostFlow,
    index: _Index,
) -> None:
    """Cheap post-conditions, kept enabled in production.

    They cost microseconds and turn a silent fairness bug into a loud alert, which is
    precisely the failure mode this system exists to avoid.
    """
    for plan in instance.days:
        chosen = result.on(plan.day)

        if len(chosen) > plan.demand:
            raise SolverInvariantError(
                f"{plan.day}: drafted {len(chosen)} for {plan.demand} vacant desks"
            )
        if len(set(chosen)) != len(chosen):
            raise SolverInvariantError(f"{plan.day}: the same person drafted twice")

        for employee_id in chosen:
            if employee_id not in plan.eligible:
                raise SolverInvariantError(
                    f"{plan.day}: drafted {employee_id}, who is not eligible "
                    "(absent, out of tenure, already fixed, or already committed)"
                )

    per_week: dict[tuple[str, int], int] = {}
    for plan in instance.days:
        for employee_id in result.on(plan.day):
            per_week[(employee_id, plan.week)] = per_week.get((employee_id, plan.week), 0) + 1
    for key, count in per_week.items():
        cap = instance.week_cap.get(key, 0)
        if count > cap:
            raise SolverInvariantError(
                f"{key[0]} drafted {count} times in week {key[1]}, cap is {cap}"
            )

    # Convexity sanity: the parallel source arcs must be consumed cheapest-first. If a
    # later change ever puts jitter on them, this is what catches it.
    for employee_id, arcs in index.source_arcs.items():
        used = [graph.flow_on(arc) > 0 for arc in arcs]
        if any(used[i] < used[i + 1] for i in range(len(used) - 1)):
            raise SolverInvariantError(
                f"{employee_id}: convex source arcs used out of order — the cost "
                "expansion no longer equals the sum of squared surpluses"
            )
