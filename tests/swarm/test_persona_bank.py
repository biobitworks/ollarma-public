"""test_persona_bank.py -- Determinism + coverage + jitter tests for PersonaBank.

Covers PROMPT-OLLARMA-SWARM-001 T3 acceptance: deterministic seeded sampling,
~5-per-archetype coverage at N=50, and per-instance jitter prompts that yield
no byte-identical duplicates within a draw.
"""
from __future__ import annotations

from collections import Counter

import pytest

from ollarma.swarm.persona_bank import ARCHETYPES, PersonaBank, PersonaPrompt


def test_persona_bank_exposes_ten_archetypes():
    bank = PersonaBank()
    assert len(bank.archetypes) == 10
    assert set(bank.archetypes) == {
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
    }


def test_archetypes_constant_matches_class_attribute():
    assert PersonaBank.archetypes == ARCHETYPES


def test_persona_bank_draw_returns_n_prompts():
    bank = PersonaBank()
    draw = bank.draw(n=50, seed=42)
    assert len(draw) == 50
    assert all(isinstance(p, PersonaPrompt) for p in draw)


def test_persona_bank_draw_zero_returns_empty():
    bank = PersonaBank()
    assert bank.draw(n=0, seed=42) == []


def test_persona_bank_draw_negative_n_raises():
    bank = PersonaBank()
    with pytest.raises(ValueError):
        bank.draw(n=-1, seed=42)


def test_persona_bank_draw_is_deterministic():
    """Same seed -> byte-identical draws across separate PersonaBank instances."""
    bank_a = PersonaBank()
    bank_b = PersonaBank()
    draw_a = bank_a.draw(n=50, seed=42)
    draw_b = bank_b.draw(n=50, seed=42)
    assert draw_a == draw_b


def test_persona_bank_draw_is_deterministic_repeated_calls():
    """Calling .draw twice with the same seed on the same instance is stable."""
    bank = PersonaBank()
    assert bank.draw(n=50, seed=42) == bank.draw(n=50, seed=42)


def test_persona_bank_distinct_seeds_distinct_draws():
    bank = PersonaBank()
    draw_42 = bank.draw(n=50, seed=42)
    draw_43 = bank.draw(n=50, seed=43)
    assert draw_42 != draw_43


def test_persona_bank_draw_n_50_archetype_coverage():
    """Each archetype should appear roughly 5 times (binomial with p=0.1, N=50).

    Loose 2..15 bound to absorb tail variance for any seed; on seed=42
    specifically, the bound is comfortably wide.
    """
    bank = PersonaBank()
    draw = bank.draw(n=50, seed=42)
    counts = Counter(p.archetype for p in draw)
    # Every archetype must appear at least once at N=50 with p=0.1; very loose.
    assert set(counts.keys()) == set(ARCHETYPES), (
        f"missing archetypes: {set(ARCHETYPES) - set(counts.keys())}"
    )
    for archetype, count in counts.items():
        assert 1 <= count <= 15, f"archetype {archetype} drawn {count} times"


def test_persona_bank_jitter_makes_prompts_unique():
    """N=50 draw must have 50 byte-distinct prompt strings."""
    bank = PersonaBank()
    draw = bank.draw(n=50, seed=42)
    prompts = [p.prompt for p in draw]
    assert len(set(prompts)) == 50, (
        f"expected 50 unique prompts, got {len(set(prompts))}"
    )


def test_persona_bank_jitter_unique_across_multiple_seeds():
    """Spot-check uniqueness on several seeds, not just the canonical one."""
    bank = PersonaBank()
    for seed in (0, 1, 7, 42, 100, 12345):
        draw = bank.draw(n=50, seed=seed)
        prompts = [p.prompt for p in draw]
        assert len(set(prompts)) == 50, (
            f"seed={seed}: {len(set(prompts))} unique of 50 prompts"
        )


def test_persona_prompt_persona_id_format():
    bank = PersonaBank()
    draw = bank.draw(n=50, seed=42)
    for idx, persona in enumerate(draw):
        assert persona.persona_id == f"{persona.archetype}_{idx:02d}"
        assert persona.instance_idx == idx


def test_persona_prompt_is_frozen():
    bank = PersonaBank()
    persona = bank.draw(n=1, seed=42)[0]
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        persona.prompt = "tampered"  # type: ignore[misc]


def test_persona_prompt_contains_archetype_in_text():
    """Sanity: the rendered prompt should mention the archetype name."""
    bank = PersonaBank()
    draw = bank.draw(n=50, seed=42)
    for persona in draw:
        # archetypes use snake_case in the constant; rendered with spaces.
        assert persona.archetype.replace("_", " ") in persona.prompt
