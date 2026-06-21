"""store.py — Append-only JSONL results store.

Active run writes rows to results/run-{run_id}.jsonl.
seal() reads all rows and writes a JSON array to results/run-{run_id}.json.
The .jsonl is preserved after seal (partial run recovery — D-17).

Threat T-03-04: run_id is validated against ^[\\w:.-]+$ before use in path.
"""
from __future__ import annotations

import pathlib
import re

import orjson

from ollarma.executor import BenchmarkResult


# ---------------------------------------------------------------------------
# T-03-04: run_id validation — ISO timestamps and 'TEST' are safe; slashes/spaces are not
# ---------------------------------------------------------------------------
_RUN_ID_RE = re.compile(r"^[\w:.-]+$")


class ResultStore:
    """Append-only JSONL store. Active run writes to .jsonl; seal() writes .json array.

    Usage:
        store = ResultStore("2026-04-06T14:30:00Z")
        store.append(result)        # writes one JSONL row, creates results/ if needed
        sealed_path = store.seal()  # writes JSON array; .jsonl is preserved
    """

    RESULTS_DIR = pathlib.Path("results")

    def __init__(self, run_id: str) -> None:
        """Construct store for the given run_id (ISO timestamp string, e.g. '2026-04-06T14:30:00Z').

        Raises ValueError if run_id contains characters that could construct unsafe paths.
        T-03-04: run_id validated with regex ^[\\w:.-]+$ before use in filesystem path.
        """
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(
                f"Invalid run_id {run_id!r}. "
                r"Only word characters, ':', '.', and '-' are allowed (must match ^[\w:.-]+$)."
            )
        self.run_id = run_id
        self.jsonl_path = self.RESULTS_DIR / f"run-{run_id}.jsonl"
        self.json_path = self.RESULTS_DIR / f"run-{run_id}.json"

    def append(self, result: BenchmarkResult) -> None:
        """Write one BenchmarkResult as a newline-delimited JSON row to the .jsonl file.

        Creates results/ directory on first call. Uses append binary mode ("ab") so
        partial runs are never lost on interrupt (D-17). orjson.dumps returns bytes;
        model_dump(mode="json") converts datetime fields to ISO strings.
        """
        self.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        # mode="json" ensures datetime fields serialize as ISO strings, not Python datetime reprs
        row = orjson.dumps(result.model_dump(mode="json"))
        with self.jsonl_path.open("ab") as f:
            f.write(row + b"\n")

    def seal(self) -> pathlib.Path:
        """Read all .jsonl rows and write as a JSON array to the .json file.

        Preserves the .jsonl file — does NOT delete or modify it (D-17 partial run recovery).
        Returns the path to the sealed .json file.
        """
        rows = []
        with self.jsonl_path.open("rb") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(orjson.loads(line))
        with self.json_path.open("wb") as f:
            f.write(orjson.dumps(rows, option=orjson.OPT_INDENT_2))
        return self.json_path
