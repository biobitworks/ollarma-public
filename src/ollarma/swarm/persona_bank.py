"""persona_bank.py -- Fixed persona archetype bank with deterministic draws.

10 archetypes from PROMPT-OLLARMA-SWARM-001 line 118. ``PersonaBank.draw(n,
seed)`` samples N persona prompts uniformly with replacement using
``random.Random(seed)`` (stdlib only -- no numpy, no external rng).

Each draw appends a per-instance jitter suffix so two instances of the same
archetype in the same draw are byte-different but reproducible across runs.
This satisfies PROMPT line 118's "personality-jitter prompts to avoid
identical responses" requirement.
"""
from __future__ import annotations

import random
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Fixed archetypes -- pre-registered, do not extend without a new PROMPT.
# ---------------------------------------------------------------------------

ARCHETYPES: Final[tuple[str, ...]] = (
    "analyst",
    "retail_investor",
    "harm_reduction_advocate",
    "peer_reviewer",
    "lay_reader",
    "history_of_science_scholar",
    "working_clinician",
    "ethicist",
    "regulator",
    "journalist",
)
"""The 10 persona archetypes from PROMPT-OLLARMA-SWARM-001 line 118."""


# Small fixed vocabularies for jitter. Sized so that 5 instances of an
# archetype (the expected count at N=50) are very likely to receive distinct
# (tone, emphasis) pairs even if the rng draws repeats. 6*6 = 36 unique
# (tone, emphasis) pairs >> 5 instances per archetype.
_TONES: Final[tuple[str, ...]] = (
    "measured",
    "skeptical",
    "earnest",
    "blunt",
    "wry",
    "guarded",
)
_EMPHASES: Final[tuple[str, ...]] = (
    "evidence",
    "stakeholders",
    "history",
    "risk",
    "incentives",
    "trade-offs",
)


# ---------------------------------------------------------------------------
# Persona prompt container.
# ---------------------------------------------------------------------------

class PersonaPrompt(BaseModel):
    """One persona instance for one simulation.

    ``persona_id`` is stable across rounds within a single simulation; the
    engine (Wave 2) uses it as the dict key for per-persona memory.
    """

    model_config = ConfigDict(frozen=True)

    persona_id: str
    """Stable id of the form ``{archetype}_{instance_idx:02d}``."""

    archetype: str
    """One of ARCHETYPES."""

    instance_idx: int = Field(ge=0)
    """0-indexed position within this draw (NOT within the archetype)."""

    prompt: str
    """The fully-rendered system prompt with jitter applied."""


# ---------------------------------------------------------------------------
# PersonaBank
# ---------------------------------------------------------------------------

class PersonaBank:
    """Fixed bank of 10 archetypes with deterministic seeded draws.

    Stateless; ``draw(n, seed)`` is a pure function of (n, seed). The same
    (n, seed) pair returns byte-identical results across processes and
    Python versions (stdlib ``random.Random`` is documented as stable for
    a given seed).
    """

    archetypes: tuple[str, ...] = ARCHETYPES

    def draw(self, n: int, seed: int) -> list[PersonaPrompt]:
        """Sample ``n`` persona prompts uniformly with replacement.

        Args:
            n: Number of personas to draw (e.g., 50 for the H0 contract).
            seed: Integer seed for ``random.Random``; same seed -> same draw.

        Returns:
            List of ``n`` PersonaPrompt objects. Each ``prompt`` field is
            byte-unique within the returned list (jitter guarantees this so
            long as N is small relative to the jitter vocabulary, which is
            the case for N=50 with 36 jitter pairs per archetype).

        Raises:
            ValueError: If ``n`` is negative.
        """
        if n < 0:
            raise ValueError(f"n must be non-negative, got {n}")

        rng = random.Random(seed)
        prompts: list[PersonaPrompt] = []
        seen_prompts: set[str] = set()

        for instance_idx in range(n):
            archetype = rng.choice(self.archetypes)

            # Draw jitter; on the rare collision (same archetype + same tone
            # + same emphasis already seen in this draw), re-roll up to a few
            # times. With 36 jitter pairs per archetype and ~5 instances per
            # archetype at N=50, collisions are uncommon but possible.
            for _ in range(16):
                tone = rng.choice(_TONES)
                emphasis = rng.choice(_EMPHASES)
                prompt = self._render_prompt(archetype, instance_idx, tone, emphasis)
                if prompt not in seen_prompts:
                    break
            else:
                # Pathological collision (would require N to vastly exceed the
                # jitter space). Append the instance_idx as a last-resort
                # disambiguator -- still deterministic in (n, seed).
                prompt = (
                    self._render_prompt(archetype, instance_idx, tone, emphasis)
                    + f" [variant {instance_idx}]"
                )

            seen_prompts.add(prompt)
            prompts.append(
                PersonaPrompt(
                    persona_id=f"{archetype}_{instance_idx:02d}",
                    archetype=archetype,
                    instance_idx=instance_idx,
                    prompt=prompt,
                )
            )

        return prompts

    @staticmethod
    def _render_prompt(
        archetype: str,
        instance_idx: int,
        tone: str,
        emphasis: str,
    ) -> str:
        """Render the persona system prompt with jitter applied.

        Format kept deliberately simple and stable; downstream engine (Wave
        2) prepends the scenario seed and prior-round aggregate stance.
        """
        article = "an" if archetype[0] in "aeiou" else "a"
        return (
            f"You are {article} {archetype.replace('_', ' ')}. "
            f"(Voice variant {instance_idx}: tone={tone}, emphasis={emphasis}.)"
        )
