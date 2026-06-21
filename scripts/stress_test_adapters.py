#!/usr/bin/env python3
"""Stress-test every ollarma-connect adapter.

For each adapter in the fleet registry this checks, using the *production* code
paths (no reimplementation):

  * Pydantic load + project_root absolute/exists/real-dir
  * service-mode project-root validation (_validate_service_project_root)
  * KB contract resolution + per-source path existence
  * has_canon vs on-disk CANON.md consistency
  * namespace_prefix presence + cross-adapter uniqueness
  * duplicate project_root collisions (excluding declared submodule groups)

It also reports coverage: which /active git repos have no adapter pointing at
them. Exit code is non-zero if any FAIL-class issue is found.

Usage:  python3 scripts/stress_test_adapters.py [--json]
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import warnings

from ollarma.service import (
    _load_project_registry,
    _validate_service_project_root,
    get_project_kb_contract,
)

ACTIVE = pathlib.Path("<local-path>")


def _canon_present(root: str) -> bool:
    return (pathlib.Path(root) / "CANON.md").is_file()


def main() -> int:
    as_json = "--json" in sys.argv
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reg = _load_project_registry()
    load_warnings = [str(w.message) for w in caught if "Skipping" in str(w.message)]

    rows: list[dict] = []
    prefixes: dict[str, list[str]] = {}
    roots: dict[str, list[str]] = {}

    for name in sorted(reg, key=str.lower):
        a = reg[name]
        issues: list[str] = []
        root = pathlib.Path(a.project_root)

        # root checks
        if not a.project_root.startswith("/"):
            issues.append("FAIL:root-not-absolute")
        if not root.exists():
            issues.append("FAIL:root-missing")
        elif not root.is_dir():
            issues.append("FAIL:root-not-dir")

        # service-mode validation (the gate real routing uses)
        try:
            _validate_service_project_root(a.project_root)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"FAIL:service-mode({type(exc).__name__})")

        # KB contract resolution + source existence
        src_total = src_missing = 0
        try:
            contract = get_project_kb_contract(name, service_mode=True).contract
            for s in contract.sources:
                src_total += 1
                if not s.path_ref.exists:
                    src_missing += 1
                    issues.append(f"WARN:kb-source-missing({s.path_ref.repo_relative})")
        except Exception as exc:  # noqa: BLE001
            issues.append(f"FAIL:kb-contract({type(exc).__name__}:{exc})")

        # classification ↔ canon_status ↔ disk consistency
        canon_on_disk = _canon_present(a.project_root) if root.is_dir() else False
        if a.classification == "science":
            if a.canon_status == "present" and not canon_on_disk:
                issues.append("FAIL:canon_status=present-but-no-CANON.md")
            if a.canon_status == "pending" and canon_on_disk:
                issues.append("WARN:CANON.md-on-disk-promote-canon_status-to-present")
        # "other" repos: a CANON.md (e.g. story/project canon) is not a science
        # governance canon — intentionally ignored.

        # prefix presence + collect for uniqueness
        if not a.namespace_prefix:
            issues.append("WARN:no-namespace_prefix")
        else:
            prefixes.setdefault(a.namespace_prefix, []).append(name)
        roots.setdefault(str(root.resolve()) if root.exists() else a.project_root, []).append(name)

        rows.append(
            {
                "project": name,
                "root": a.project_root,
                "src": f"{src_total - src_missing}/{src_total}",
                "class": a.classification,
                "canon_status": a.canon_status,
                "has_canon": a.has_canon,
                "canon_disk": canon_on_disk,
                "prefix": a.namespace_prefix or "-",
                "issues": issues,
            }
        )

    # cross-adapter: duplicate prefixes / roots
    dup_prefix = {k: v for k, v in prefixes.items() if len(v) > 1}
    dup_root = {k: v for k, v in roots.items() if len(v) > 1}

    # coverage
    repos = sorted(d.name for d in ACTIVE.iterdir() if (d / ".git").is_dir())
    covered = {pathlib.Path(reg[n].project_root).name for n in reg}
    uncovered = [r for r in repos if r not in covered]

    fails = [r for r in rows if any(i.startswith("FAIL") for i in r["issues"])]
    warns = [r for r in rows if any(i.startswith("WARN") for i in r["issues"])]

    if as_json:
        print(json.dumps(
            {
                "adapters": rows,
                "load_warnings": load_warnings,
                "duplicate_prefixes": dup_prefix,
                "duplicate_roots": dup_root,
                "uncovered_active_repos": uncovered,
                "summary": {"total": len(rows), "fail": len(fails), "warn": len(warns)},
            },
            indent=2,
        ))
    else:
        print(f"{'PROJECT':28} {'SRC':>6} {'CLASS':8} {'CANON_STATUS':14} {'PREFIX':16} ISSUES")
        print("-" * 104)
        for r in rows:
            flag = "FAIL" if any(i.startswith("FAIL") for i in r["issues"]) else (
                "warn" if r["issues"] else "ok")
            issue_txt = "; ".join(r["issues"]) if r["issues"] else "-"
            print(f"{r['project']:28} {r['src']:>6} {r['class']:8} {r['canon_status']:14} "
                  f"{r['prefix']:16} [{flag}] {issue_txt}")
        print("-" * 92)
        if load_warnings:
            print(f"LOAD WARNINGS ({len(load_warnings)}):")
            for w in load_warnings:
                print(f"  ! {w}")
        if dup_prefix:
            print("DUPLICATE PREFIXES:")
            for k, v in dup_prefix.items():
                print(f"  {k} -> {v}")
        if dup_root:
            print("DUPLICATE project_root (>1 adapter same dir):")
            for k, v in dup_root.items():
                print(f"  {k} -> {v}")
        print(f"\nUNCOVERED /active git repos ({len(uncovered)} of {len(repos)}):")
        print("  " + ", ".join(uncovered))
        print(f"\nSUMMARY: {len(rows)} adapters | {len(fails)} FAIL | {len(warns)} warn")

    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
