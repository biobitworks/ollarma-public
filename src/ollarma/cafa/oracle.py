"""D8 — MCP/RAG oracle harness: MEASURE the local↔online boundary.

The oracle is a TEST harness, never in the production verdict path
(``docs/LOCAL_VS_ONLINE_BOUNDARY.md`` zone ③). For each antibody/cell task it
compares the LOCAL model's labels against an MCP/RAG reference on labeled data and
reports, per task, whether local-only suffices (``proven_local``) or lags
(``needs_online`` — the measured residual that is the *only* justified zone-④
traffic). The boundary is thus measured, not assumed.

Pure metric computation: agreement, accuracy, MCC (no model, no network here — the
labels are supplied by the Ollarma model lanes + the MCP/RAG grader upstream).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt


@dataclass
class TaskBoundary:
    task: str                       # antibody/cell key
    n: int
    agreement: float                # fraction local==oracle
    accuracy_local: float | None    # if gold labels supplied
    accuracy_oracle: float | None
    mcc_local: float | None
    verdict: str                    # "proven_local" | "needs_online" | "insufficient_data"
    detail: str = ""


def _accuracy(pred: list[str], gold: list[str]) -> float | None:
    if not gold or len(pred) != len(gold):
        return None
    return sum(1 for p, g in zip(pred, gold) if p == g) / len(gold)


def _mcc_binary(pred: list[str], gold: list[str], positive: str) -> float | None:
    """Matthews correlation for a one-vs-rest binarization on `positive`."""
    if not gold or len(pred) != len(gold):
        return None
    tp = fp = tn = fn = 0
    for p, g in zip(pred, gold):
        pp, gg = (p == positive), (g == positive)
        if pp and gg:
            tp += 1
        elif pp and not gg:
            fp += 1
        elif not pp and gg:
            fn += 1
        else:
            tn += 1
    denom = sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    if denom == 0:
        return 0.0
    return (tp * tn - fp * fn) / denom


def measure_task(task: str, *, local: list[str], oracle: list[str],
                 gold: list[str] | None = None,
                 positive: str | None = None,
                 agreement_threshold: float = 0.9,
                 min_n: int = 20) -> TaskBoundary:
    """Locate the boundary for one task from local vs oracle (vs optional gold)."""
    n = len(local)
    if n < min_n or len(oracle) != n:
        return TaskBoundary(task, n, 0.0, None, None, None,
                            "insufficient_data",
                            f"need >= {min_n} aligned samples, have {n}")
    agreement = sum(1 for a, b in zip(local, oracle) if a == b) / n
    acc_local = _accuracy(local, gold) if gold else None
    acc_oracle = _accuracy(oracle, gold) if gold else None
    mcc = _mcc_binary(local, gold, positive) if (gold and positive) else None
    # proven_local: local agrees with the oracle reference at/above threshold
    # (and, if gold present, local is not materially worse than the oracle).
    proven = agreement >= agreement_threshold
    if gold and acc_local is not None and acc_oracle is not None:
        proven = proven and (acc_local >= acc_oracle - 0.05)
    verdict = "proven_local" if proven else "needs_online"
    detail = (f"agreement={agreement:.3f} (thr {agreement_threshold})"
              + (f", acc_local={acc_local:.3f}, acc_oracle={acc_oracle:.3f}"
                 if acc_local is not None else ""))
    return TaskBoundary(task, n, agreement, acc_local, acc_oracle, mcc, verdict, detail)


@dataclass
class BoundaryReport:
    tasks: list[TaskBoundary] = field(default_factory=list)

    @property
    def proven_local(self) -> list[str]:
        return [t.task for t in self.tasks if t.verdict == "proven_local"]

    @property
    def needs_online(self) -> list[str]:
        return [t.task for t in self.tasks if t.verdict == "needs_online"]

    def summary(self) -> str:
        lines = [f"local↔online boundary ({len(self.tasks)} tasks):"]
        for t in self.tasks:
            lines.append(f"  {t.task:<24} {t.verdict:<16} {t.detail}")
        lines.append(f"  PROVEN-LOCAL: {self.proven_local}")
        lines.append(f"  NEEDS-ONLINE: {self.needs_online}")
        return "\n".join(lines)


def measure_boundary(task_inputs: dict[str, dict]) -> BoundaryReport:
    """task_inputs: {task: {local, oracle, gold?, positive?}} -> BoundaryReport."""
    return BoundaryReport([measure_task(task, **kw) for task, kw in task_inputs.items()])
