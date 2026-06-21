#!/usr/bin/env python3
"""Build a public extraction tree from public-manifest.txt and measure residual coupling.

Implements the overlay-not-fork mechanism from
``docs/public-release/PUBLIC_RELEASE_PLAN.md`` §3:

  1. read the allowlist (``public-manifest.txt``),
  2. copy matching TRACKED files into ``dist/public/``,
  3. scrub personal tokens + machine-specific path fallbacks,
  4. grep the scrubbed tree against a private-name DENYLIST,
  5. report residual hits and exit non-zero if any remain (fail-closed gate).

``dist/`` is gitignored, so the public tree is a build artifact, not committed.
This script + the manifest ARE committed — they are the reproducible extraction
contract. Run: ``python scripts/build_public_tree.py``.

This is honest tooling: it does NOT claim the tree is clean. It reports exactly
how much private coupling survives the allowlist + scrubber, so the residual is a
measured number, not an assumption.
"""
from __future__ import annotations

import glob
import pathlib
import re
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "dist" / "public"
MANIFEST = REPO / "public-manifest.txt"

# Private portfolio names + personal markers that must NOT appear in a public tree.
DENYLIST = [
    "cellico", "overwatch", "watchtower", "deltaprot", "biobitworks",
    "healthclock", "<local-path>", "byron@", "vitalist", "immunos",
]

# Scrubber: (regex, replacement). Applied to every copied text file in the
# dist/public/ BUILD ARTIFACT only — never the real repo. Paths first, then
# private portfolio names genericized (case-insensitive) so the public tree
# carries no internal project identity.
SCRUBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<repo>/adapters"),
     "~/.config/ollarma/adapters"),
    (re.compile(r"<repo>/skills"),
     "~/.config/ollarma/skills"),
    (re.compile(r"<repo>"), "."),
    (re.compile(r"<repo>[A-Za-z0-9_-]+"), "<project-root>"),
    (re.compile(r"<local-path>[^\s\"')]*"), "<local-path>"),
    (re.compile(r"byron@biobitworks\.com"), "operator@example.org"),
    # Private portfolio names -> generic placeholders. IDENTIFIER-SAFE
    # (underscores, no hyphens) because these names appear inside Python
    # identifiers (e.g. RESERVED_ANTIGENCE_SENTINEL_MODELS); a hyphen would
    # produce invalid syntax. Same regex applied to source AND tests keeps the
    # tree internally consistent. Case-insensitive; longest variants first.
    (re.compile(r"cellico[-_]?bio", re.I), "example_science"),
    (re.compile(r"cellico", re.I), "example_science"),
    (re.compile(r"deltaprot", re.I), "example_proteomics"),
    (re.compile(r"antigence[-_]?bittensor", re.I), "example_review"),
    (re.compile(r"antigence", re.I), "example_review"),
    (re.compile(r"overwatch", re.I), "governance_backend"),
    (re.compile(r"watchtower", re.I), "operator_console"),
    (re.compile(r"healthclock", re.I), "example_service"),
    (re.compile(r"vitalist\w*", re.I), "example_program"),
    (re.compile(r"biobitworks", re.I), "example_org"),
    (re.compile(r"\bimmunos\b", re.I), "example_memory"),
    (re.compile(r"gettingsciencedone", re.I), "workflow_skills"),
    (re.compile(r"\bgsigmad\b", re.I), "workflow_gov"),
    (re.compile(r"\bByron\b"), "the maintainer"),
]

TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg"}


def tracked_files() -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True, text=True, check=True,
    )
    return set(out.stdout.splitlines())


def read_manifest() -> list[str]:
    patterns: list[str] = []
    for line in MANIFEST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def expand(patterns: list[str], tracked: set[str]) -> set[str]:
    selected: set[str] = set()
    for pat in patterns:
        for hit in glob.glob(str(REPO / pat), recursive=True):
            rel = str(pathlib.Path(hit).resolve().relative_to(REPO))
            if rel in tracked and (REPO / rel).is_file():
                selected.add(rel)
    return selected


def scrub_text(text: str) -> str:
    for rx, repl in SCRUBS:
        text = rx.sub(repl, text)
    return text


def build() -> set[str]:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    tracked = tracked_files()
    selected = expand(read_manifest(), tracked)
    for rel in sorted(selected):
        src = REPO / rel
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix in TEXT_SUFFIXES:
            try:
                dst.write_text(scrub_text(src.read_text()))
            except UnicodeDecodeError:
                shutil.copy2(src, dst)
        else:
            shutil.copy2(src, dst)
    return selected


def denylist_report() -> dict[str, list[str]]:
    report: dict[str, list[str]] = {}
    for term in DENYLIST:
        out = subprocess.run(
            ["grep", "-ril", "--", term, str(OUT)],
            capture_output=True, text=True,
        )
        hits = [
            str(pathlib.Path(p).relative_to(OUT))
            for p in out.stdout.splitlines() if p
        ]
        if hits:
            report[term] = hits
    return report


def main() -> int:
    selected = build()
    print(f"[build] copied {len(selected)} files into {OUT.relative_to(REPO)}/")
    report = denylist_report()
    total = sum(len(v) for v in report.values())
    if not report:
        print("[gate] PASS — 0 denylist hits in the public tree.")
        return 0
    print(f"[gate] FAIL — {total} denylist hit(s) across {len(report)} term(s):")
    for term, hits in sorted(report.items(), key=lambda kv: -len(kv[1])):
        print(f"  {term!r}: {len(hits)} file(s) -> {', '.join(hits[:6])}"
              + (" ..." if len(hits) > 6 else ""))
    print("\n[gate] Residual coupling above. Tighten the manifest (drop the "
          "offending files) or extend SCRUBS, then re-run. Fail-closed: a "
          "public release is NOT ready while this is non-zero.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
