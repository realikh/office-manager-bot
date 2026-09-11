"""Min-cost max-flow over lexicographically-ordered vector costs.

Successive shortest paths with Johnson potentials and Dijkstra.

Costs are integer tuples compared lexicographically rather than a single packed scalar.
Min-cost-flow theory — reduced costs, potentials, negative-cycle optimality — holds
verbatim over any ordered abelian group, and lexicographically-ordered Z^k is one;
Dijkstra needs only monotonicity and non-negative arcs, both of which hold. That buys
exact lexicographic priority between objectives with no separation constants to derive
(and re-derive every time a tier is added), and makes tier priority literally the
component order.

Hand-rolled on purpose. A third-party solver gives no guarantee that tie-breaks stay
stable across versions, and a schedule that reshuffles because a patch dependency moved
is a support ticket.
"""

from __future__ import annotations

from heapq import heappop, heappush

Cost = tuple[int, ...]


def zero_cost(dim: int) -> Cost:
    return (0,) * dim


def add_cost(left: Cost, right: Cost) -> Cost:
    return tuple(a + b for a, b in zip(left, right, strict=True))


def sub_cost(left: Cost, right: Cost) -> Cost:
    return tuple(a - b for a, b in zip(left, right, strict=True))


def neg_cost(cost: Cost) -> Cost:
    return tuple(-a for a in cost)


class NegativeArcCostError(ValueError):
    """Raised when an arc would break the non-negativity Dijkstra depends on."""


class MinCostFlow:
    """A flow network whose arc costs are lexicographic integer vectors.

    Arcs must have non-negative cost; the caller is responsible for shifting costs into
    the non-negative range (see ``solver.build_graph``, which does exactly that).
    """

    __slots__ = ("_cap", "_cost", "_cost_dim", "_graph", "_head", "_n")

    def __init__(self, n_nodes: int, cost_dim: int) -> None:
        self._n = n_nodes
        self._cost_dim = cost_dim
        self._graph: list[list[int]] = [[] for _ in range(n_nodes)]
        self._head: list[int] = []
        self._cap: list[int] = []
        self._cost: list[Cost] = []

    @property
    def n_nodes(self) -> int:
        return self._n

    def add_arc(self, tail: int, head: int, capacity: int, cost: Cost) -> int:
        """Add an arc and its residual twin. Returns the forward arc's id."""
        if len(cost) != self._cost_dim:
            raise ValueError(f"cost must have {self._cost_dim} components, got {len(cost)}")
        if any(component < 0 for component in cost):
            raise NegativeArcCostError(f"arc {tail}->{head} has negative cost {cost}")

        arc_id = len(self._head)
        self._head.append(head)
        self._cap.append(capacity)
        self._cost.append(cost)
        self._graph[tail].append(arc_id)

        self._head.append(tail)
        self._cap.append(0)
        self._cost.append(neg_cost(cost))
        self._graph[head].append(arc_id + 1)

        return arc_id

    def flow_on(self, arc_id: int) -> int:
        """How much flow the forward arc ``arc_id`` ended up carrying."""
        return self._cap[arc_id ^ 1]

    def solve(self, source: int, sink: int) -> tuple[int, Cost, list[Cost]]:
        """Push maximum flow at minimum lexicographic cost.

        Returns ``(flow_value, total_cost, potentials)``. The potentials are returned
        because the zero-reduced-cost residual subgraph they define is what a later
        local-search pass would need to explore alternative optima.
        """
        dim = self._cost_dim
        zero = zero_cost(dim)
        potential: list[Cost] = [zero] * self._n
        total: Cost = zero
        flow = 0

        while True:
            dist, parent = self._dijkstra(source, potential)
            sink_dist = dist[sink]
            if sink_dist is None:
                break

            for node in range(self._n):
                node_dist = dist[node]
                # Unreachable nodes take the sink's distance, which keeps every reduced
                # cost non-negative without pretending we found a path to them.
                step = sink_dist if node_dist is None or node_dist > sink_dist else node_dist
                potential[node] = add_cost(potential[node], step)

            bottleneck = self._bottleneck(source, sink, parent)
            node = sink
            while node != source:
                arc = parent[node]
                assert arc is not None
                self._cap[arc] -= bottleneck
                self._cap[arc ^ 1] += bottleneck
                total = add_cost(total, _scale_cost(self._cost[arc], bottleneck))
                node = self._head[arc ^ 1]

            flow += bottleneck

        return flow, total, potential

    def _bottleneck(self, source: int, sink: int, parent: list[int | None]) -> int:
        amount = -1
        node = sink
        while node != source:
            arc = parent[node]
            assert arc is not None
            amount = self._cap[arc] if amount < 0 else min(amount, self._cap[arc])
            node = self._head[arc ^ 1]
        return max(amount, 0)

    def _dijkstra(
        self, source: int, potential: list[Cost]
    ) -> tuple[list[Cost | None], list[int | None]]:
        dist: list[Cost | None] = [None] * self._n
        parent: list[int | None] = [None] * self._n
        settled = [False] * self._n

        start = zero_cost(self._cost_dim)
        dist[source] = start
        # The node index in the heap key is not decoration: it makes tie-breaking
        # deterministic instead of an accident of heap ordering.
        heap: list[tuple[Cost, int]] = [(start, source)]

        while heap:
            node_dist, node = heappop(heap)
            if settled[node]:
                continue
            settled[node] = True

            for arc in self._graph[node]:
                if self._cap[arc] <= 0:
                    continue
                head = self._head[arc]
                if settled[head]:
                    continue
                reduced = add_cost(self._cost[arc], sub_cost(potential[node], potential[head]))
                candidate = add_cost(node_dist, reduced)
                current = dist[head]
                if current is None or candidate < current:
                    dist[head] = candidate
                    parent[head] = arc
                    heappush(heap, (candidate, head))

        return dist, parent


def _scale_cost(cost: Cost, factor: int) -> Cost:
    return tuple(component * factor for component in cost)
