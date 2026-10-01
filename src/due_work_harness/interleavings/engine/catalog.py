"""Deterministic history generation, independent of Django and provider bindings."""

from itertools import combinations, permutations

from ..model import Fault, History, Step
from ..model import Operation as Op
from .causality import available_facts, valid_evidence_order


def step(
    operation: Op, *, target: str = "a", value: str = "", seam: str = "", index: int = 0, fault: Fault = Fault.HOLD
) -> Step:
    return Step(operation=operation, target=target, value=value, seam=seam, index=index, fault=fault)


def normalized(histories: list[History]) -> tuple[History, ...]:
    unique: dict[tuple[Step, ...], History] = {}
    for history in histories:
        # Executable steps define identity; family labels are coverage metadata.
        # Keep the first ID stable for exact legacy gaps and saved reproductions,
        # while retaining every family that generated this same schedule.
        previous = unique.get(history.steps)
        if previous is None:
            unique[history.steps] = history
        else:
            unique[history.steps] = previous.model_copy(
                update={"families": tuple(dict.fromkeys((*previous.families, *history.families)))}
            )
    return tuple(unique.values())


def in_flight_histories(
    intents: tuple[str, ...],
    seams: tuple[str, ...],
    retirement: bool,
    independent: bool,
    transport: bool,
    repair_seams: tuple[str, ...] = (),
    replay_safe: bool = True,
) -> tuple[History, ...]:
    histories = []
    admit = step(Op.ADMIT, value=intents[0])
    start = step(Op.START)
    settle = step(Op.SETTLE)
    quiet = step(Op.QUIET)
    changed = step(Op.RETIRE) if retirement else step(Op.CHANGE, value=intents[1])

    def add(family: str, variant: str, steps: list[Step]) -> None:
        histories.append(History(id=f"{family}/{variant}", families=(family,), steps=tuple(steps)))

    add("IF.control", "normal", [admit, start, settle, changed, settle, quiet])
    for seam in seams:
        arm = step(Op.ARM, seam=seam)
        prefix = [admit, arm, start]
        # Same accepted operation before or after newer authority. Both controls
        # execute real commands, including locally serialized callers.
        for late in (False, True):
            family = "IF.retire-before-completion" if retirement else "IF.two-orders"
            body = [changed, step(Op.RECOVER)] if late else [step(Op.COMPLETE), step(Op.RECOVER), changed]
            if late:
                # Revision writes may settle before the first RPC finishes;
                # retirement often deliberately waits for evidence of that RPC.
                if not retirement:
                    body.append(settle)
                body.append(step(Op.COMPLETE))
            add(family, f"{seam}/{'late' if late else 'early'}", [*prefix, *body, settle, quiet])
        add(
            "IF.unresolved",
            seam,
            [
                *prefix,
                step(Op.RECOVER),
                step(Op.OWED),
                step(Op.RECOVER),
                step(Op.OWED),
                step(Op.COMPLETE),
                settle,
                quiet,
            ],
        )
        add(
            "IF.control",
            f"{seam}/response-lost",
            [admit, step(Op.ARM, seam=seam, fault=Fault.LOSE_RESPONSE), start, settle, changed, settle, quiet],
        )
        # A success-shaped reply that never applied. Replay-safe work must still
        # converge, which needs provider state rather than the reply; replay-unsafe
        # work may not re-send, so it must stay unconfirmed instead.
        accepted = step(Op.ARM, seam=seam, fault=Fault.ACCEPT_WITHOUT_EFFECT)
        if replay_safe:
            add("IF.false-acceptance", seam, [admit, accepted, start, settle, changed, settle, quiet])
        elif not retirement:
            unconfirmed = step(Op.UNCONFIRMED)
            add(
                "IF.false-acceptance",
                f"{seam}/no-replay",
                [admit, accepted, start, step(Op.RECOVER), unconfirmed, step(Op.RECOVER), unconfirmed],
            )
        if retirement:
            add(
                "IF.retire-between-resources",
                seam,
                [*prefix, changed, step(Op.RECOVER), step(Op.COMPLETE), settle, quiet],
            )
        if independent:
            for order in ((0, 1), (1, 0)):
                body = [
                    step(Op.ADMIT, target="control", value=intents[0]),
                    step(Op.START, target="control"),
                    step(Op.SETTLE, target="control"),
                ]
                for alias in ("a", "b"):
                    body += [
                        step(Op.ADMIT, target=alias, value=intents[0]),
                        arm,
                        step(Op.START, target=alias),
                        step(Op.RETIRE, target=alias)
                        if retirement
                        else step(Op.CHANGE, target=alias, value=intents[1]),
                    ]
                for index in order:
                    body += [
                        step(Op.COMPLETE, index=index),
                        step(Op.SETTLE, target=("a", "b")[index]),
                        step(Op.SETTLE, target="control"),
                    ]
                add("IF.independent-progress", f"{seam}/{order[0]}-first", [*body, quiet])
        if not retirement and len(intents) >= 3:
            for aba in (False, True):
                family = "IF.return-to-value" if aba else "IF.second-late-completion"
                values = (intents[0], intents[1], intents[0] if aba else intents[2])
                for order in permutations(range(3)):
                    body = [admit, arm, start]
                    for value in values[1:]:
                        body += [arm, step(Op.CHANGE, value=value)]
                    for index in order:
                        body += [step(Op.COMPLETE, index=index), step(Op.RECOVER)]
                    add(family, f"{seam}/{''.join(map(str, order))}", [*body, settle, quiet])
    for seam in repair_seams or (() if retirement else seams):
        if retirement:
            body = [admit, start, settle, step(Op.ARM, seam=seam, fault=Fault.REFUSE), changed]
        else:
            body = [
                admit,
                step(Op.ARM, seam=seams[0]),
                start,
                changed,
                settle,
                step(Op.COMPLETE),
                step(Op.ARM, seam=seam, fault=Fault.REFUSE),
                step(Op.RECOVER),
            ]
        add("IF.repair-fails-once", seam, [*body, settle, quiet])
    if transport:
        add(
            "IF.lost-notification",
            "admission",
            [step(Op.DROP, value="on"), admit, start, step(Op.DROP, value="off"), settle, changed, settle, quiet],
        )
        add(
            "IF.lost-notification",
            "recovery",
            [
                step(Op.DROP, value="on"),
                admit,
                start,
                step(Op.RECOVER),
                step(Op.DROP, value="off"),
                settle,
                changed,
                settle,
                quiet,
            ],
        )
        for after_change in (False, True):
            body = [admit, start, settle]
            body += [changed, settle] if after_change else []
            body += [step(Op.DELIVER)]
            body += [] if after_change else [changed]
            add("IF.duplicate-delivery", str(after_change).lower(), [*body, settle, quiet])
        add("IF.settled-tail", "old-delivery", [admit, start, settle, changed, settle, step(Op.DELIVER), quiet])
    else:
        add("IF.settled-tail", "recovery", [admit, start, settle, changed, settle, quiet])
    return normalized(histories)


def evidence_histories(
    facts: tuple[str, ...],
    dependencies: tuple[tuple[str, str], ...],
    ordered_pair: tuple[str, str] | None,
    retry_turnover: bool,
    batchable: bool = False,
) -> tuple[History, ...]:
    histories = []

    orders = [order for order in permutations(facts) if valid_evidence_order(order, dependencies)]
    assert orders, "evidence dependency cycle has no legal ordering"

    def evidence(order: tuple[str, ...]) -> list[Step]:
        return [item for fact in order for item in (step(Op.EVIDENCE, value=fact), step(Op.CHECK_EVIDENCE))]

    def add(family: str, variant: str, body: list[Step]) -> None:
        histories.append(
            History(
                id=f"{family}/{variant}", families=(family,), steps=(step(Op.PREPARE), *body, step(Op.CHECK_EVIDENCE))
            )
        )

    add("EC.control", "canonical", evidence(orders[0]))
    for order in orders:
        name = "-".join(order)
        add("EC.permutations", name, evidence(order))
        for fact in facts:
            for gap in range(len(order) + 1):
                duplicate = order[:gap] + (fact,) + order[gap:]
                if valid_evidence_order(duplicate, dependencies):
                    add("EC.duplicates", f"{name}/{fact}@{gap}", evidence(duplicate))
            add(
                "EC.late-after-settlement",
                f"{name}/{fact}",
                [*evidence(order), step(Op.RECOVER), step(Op.EVIDENCE, value=fact), step(Op.RECOVER)],
            )
        for gap in range(len(order) + 1):
            add(
                "EC.recovery-gaps",
                f"{name}/{gap}",
                [*evidence(order[:gap]), step(Op.RECOVER), step(Op.CHECK_EVIDENCE), *evidence(order[gap:])],
            )
        add(
            "EC.recovery-gaps",
            f"{name}/every",
            [
                item
                for fact in order
                for item in (step(Op.EVIDENCE, value=fact), step(Op.RECOVER), step(Op.CHECK_EVIDENCE))
            ],
        )
    for size in range(1, len(facts)):
        for subset in combinations(facts, size):
            order = next((o for o in orders if set(o[:size]) == set(subset)), None)
            if order is not None:
                add(
                    "EC.partial",
                    "-".join(subset),
                    [*evidence(order[:size]), step(Op.RECOVER), step(Op.CHECK_EVIDENCE), *evidence(order[size:])],
                )
    if batchable:
        for order in orders:
            # Every ordered partition, including single deliveries and all-at-once.
            # Singletons reuse the ordinary operation so normalization deduplicates them.
            for cuts in range(1 << (len(order) - 1)):
                groups: list[tuple[str, ...]] = []
                start = 0
                for index in range(len(order) - 1):
                    if cuts & (1 << index):
                        groups.append(order[start : index + 1])
                        start = index + 1
                groups.append(order[start:])
                body = []
                seen: set[str] = set()
                for group in groups:
                    if available_facts(group, dependencies, seen) != group:
                        break  # Prerequisites must have been consumed in an earlier batch.
                    body += (
                        evidence(group)
                        if len(group) == 1
                        else [Step(operation=Op.BATCH, facts=group), step(Op.CHECK_EVIDENCE)]
                    )
                    seen.update(group)
                else:
                    add("EC.batching", f"{'-'.join(order)}/{cuts}", body)
    if ordered_pair:
        for first, second in (ordered_pair, ordered_pair[::-1]):
            remainder = tuple(f for f in facts if f not in ordered_pair)
            assert valid_evidence_order((first, second, *remainder), dependencies), (
                "connection order violates evidence dependencies"
            )
            add(
                "EC.owner-reconciler",
                f"{first}-first",
                [step(Op.RACE, value=first, target=second), *evidence(remainder)],
            )
    if retry_turnover:
        # Attempt the real sender before complete evidence, including no facts.
        # The reviewed subset oracle determines whether retry is safe or blocked.
        for size in range(len(facts)):
            prefixes = dict.fromkeys(order[:size] for order in orders)
            for prefix in prefixes:
                add(
                    "EC.partial",
                    f"retry/{'-'.join(prefix) or 'no-facts'}",
                    [*evidence(prefix), step(Op.RECOVER), step(Op.RETRY)],
                )
        for order in orders:
            for during in (False, True):
                add(
                    "EC.retry-turnover",
                    f"{'-'.join(order)}/{'during' if during else 'after'}",
                    [
                        *evidence(order),
                        step(Op.RETRY, value=",".join(order) if during else ""),
                        *evidence(order),
                        step(Op.RECOVER),
                    ],
                )
    return normalized(histories)
