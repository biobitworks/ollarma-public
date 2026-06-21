#!/usr/bin/env python3
"""Rebuild only STALE Ollarma project KBs to keep the local routing lane warm.

Why: project KBs go stale after `freshness_hours` (default 24h). A stale KB makes
`/route` escalate to frontier/human — i.e. the caller falls back to cloud. Rebuild
is local-only (nomic-embed-text), so refreshing stale KBs on a schedule keeps the
local-first lane usable and avoids needless cloud spend.

Design notes:
  - Idempotent: KBs that are already `ready` are skipped (most runs do nothing).
  - ALLOWLIST-scoped: only refreshes operator/infra projects by default. PI-gated
    research KBs are intentionally excluded from the unattended job — refresh those
    manually (`ollarma kb-build --project <name>`) after the usual approval.
    Override with OLLARMA_KB_REFRESH_PROJECTS="A,B,C" (or "*" for all discovered).
  - Read-only w.r.t. truth: kb-build only re-indexes the adapter's already-declared
    sources. It does not add new data or write upstream.

Exit code is always 0 (a stale KB that fails to rebuild is logged, not fatal — the
next run retries). Intended to run from a 12h LaunchAgent.
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys

OLLARMA_ROOT = "<repo>"
sys.path.insert(0, os.path.join(OLLARMA_ROOT, "src"))

# Conservative default scope: operator/governance/infra stack + adapter-scaffolded
# infra/public repos. PI-gated research KBs are deliberately NOT here — refresh
# those manually after the usual approval.
DEFAULT_ALLOWLIST = [
    "Watchtower", "Ollarma", "Overwatch", "gettingsciencedone",
    "metarepo", "bioviz-tech", "synapse",
]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _allowlist(discovered: list[str]) -> list[str]:
    raw = os.environ.get("OLLARMA_KB_REFRESH_PROJECTS", "").strip()
    if raw == "*":
        return discovered
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return DEFAULT_ALLOWLIST


def main() -> int:
    from ollarma import service
    from ollarma.kb_contract import build_project_knowledge_contract
    from ollarma.kb_search import load_kb_status

    reg = service.list_projects(service_mode=True)
    allow = _allowlist(list(reg.keys()))

    stale: list[str] = []
    for name in allow:
        ad = reg.get(name)
        if ad is None:
            print(f"{_now()} [skip] {name}: no adapter")
            continue
        try:
            contract = build_project_knowledge_contract(
                project_name=ad.project_name,
                project_root=ad.project_root,
                knowledge_base=ad.knowledge_base,
                databases=ad.databases,
            )
            status = load_kb_status(contract)
        except Exception as exc:  # noqa: BLE001 - log and continue
            print(f"{_now()} [skip] {name}: status error: {exc}")
            continue
        if status.status == "stale":
            stale.append(name)

    print(f"{_now()} scope={allow} stale={stale}")
    for name in stale:
        try:
            result = subprocess.run(
                ["ollarma", "kb-build", "--project", name],
                cwd=OLLARMA_ROOT, capture_output=True, text=True, timeout=900,
            )
            ok = "KB built" in result.stdout
            detail = "" if ok else (result.stderr or result.stdout)[:160].replace("\n", " ")
            print(f"{_now()}   rebuild {name}: {'OK' if ok else 'FAIL'} {detail}")
        except Exception as exc:  # noqa: BLE001
            print(f"{_now()}   rebuild {name}: ERROR {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
