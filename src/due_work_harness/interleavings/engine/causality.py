"""Evidence eligibility shared by deterministic enumeration and optional exploration."""

from collections.abc import Set


def available_facts(
    facts: tuple[str, ...], dependencies: tuple[tuple[str, str], ...], seen: Set[str]
) -> tuple[str, ...]:
    """Duplicates remain legal; prerequisites must have been consumed before this arrival."""
    return tuple(fact for fact in facts if all(before in seen for before, after in dependencies if after == fact))


def valid_evidence_order(order: tuple[str, ...], dependencies: tuple[tuple[str, str], ...]) -> bool:
    seen: set[str] = set()
    for fact in order:
        if not available_facts((fact,), dependencies, seen):
            return False
        seen.add(fact)
    return True


def reachable_fact_sets(facts: tuple[str, ...], dependencies: tuple[tuple[str, str], ...]) -> set[frozenset[str]]:
    """All legal prefixes, including empty evidence; reuse the delivery eligibility rule."""
    pending = [frozenset[str]()]
    reached = set(pending)
    while pending:
        seen = pending.pop()
        for fact in available_facts(facts, dependencies, seen):
            following = seen | {fact}
            if following not in reached:
                reached.add(following)
                pending.append(following)
    return reached
