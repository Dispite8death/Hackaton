"""Conservative reconciliation; disagreement is a review task, not a site violation."""
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DateDecision:
    value: date | None
    status: str
    candidates: dict[str, str | None]


def reconcile_event_dates(algorithm: date | None, model: date | None = None,
                          *, model_checked: bool = False) -> DateDecision:
    candidates = {"algorithm": algorithm.isoformat() if algorithm else None}
    if not model_checked:
        return DateDecision(algorithm, "algorithm" if algorithm else "unknown", candidates)
    candidates["model"] = model.isoformat() if model else None
    if algorithm != model:
        return DateDecision(None, "review", candidates)
    return DateDecision(algorithm, "agreement" if algorithm else "unknown", candidates)
