"""sonifier.py -- Audio rendering of evidence chains for tamper detection.

Implements SONI-01 (evidence chain -> WAV), SONI-02 (dissonant tones at
tampered positions), SONI-04 (deterministic waveform, stdlib wave + numpy only).

All functions are pure (no I/O). WAV file writing is handled by bench.py.
"""
from __future__ import annotations

import io
import wave

import numpy as np

from ollarma.evidence import canonical_hash, deterministic_row, GENESIS_PARENT_HASH


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_RATE: int = 44100
"""Audio sample rate in Hz."""

TONE_DURATION: float = 0.3
"""Duration of each receipt tone in seconds."""

ATTACK_MS: int = 10
"""Linear attack envelope duration in milliseconds."""

RELEASE_MS: int = 10
"""Linear release envelope duration in milliseconds."""

C4: float = 261.63
"""Base frequency: middle C (C4) in Hz."""

SEMITONE: float = 2 ** (1 / 12)
"""Equal temperament semitone ratio."""

MAJOR_SCALE_SEMITONES: tuple[int, ...] = (0, 2, 4, 5, 7, 9, 11)
"""Semitone offsets for C major scale (7 notes per octave)."""


# ---------------------------------------------------------------------------
# Layer 1: Per-receipt validation (non-throwing)
# ---------------------------------------------------------------------------

def validate_receipts(rows: list[dict], receipts: list[dict]) -> list[bool]:
    """Validate each receipt independently. Returns per-receipt boolean list.

    Unlike verify_evidence_chain() which raises on first failure,
    this returns the FULL validity map for sonification.
    Uses STORED parent_hash to avoid cascade failure (Pitfall 1).

    If len(rows) != len(receipts): returns [False] * max(len(rows), len(receipts)).
    """
    if len(rows) != len(receipts):
        return [False] * max(len(rows), len(receipts))

    parent = GENESIS_PARENT_HASH
    validity: list[bool] = []

    for seq, (row, receipt) in enumerate(zip(rows, receipts)):
        # Recompute row_hash from deterministic fields
        expected_row_hash = canonical_hash(deterministic_row(row))
        row_valid = expected_row_hash == receipt["row_hash"]

        # Recompute receipt_hash from {row_hash, parent_hash, sequence}
        expected_receipt_data = {
            "row_hash": expected_row_hash,
            "parent_hash": parent,
            "sequence": seq,
        }
        expected_receipt_hash = canonical_hash(expected_receipt_data)
        receipt_valid = expected_receipt_hash == receipt["receipt_hash"]

        validity.append(row_valid and receipt_valid)

        # Use STORED receipt_hash as parent for next receipt (Pitfall 1)
        parent = receipt["receipt_hash"]

    return validity


# ---------------------------------------------------------------------------
# Layer 2: Frequency mapping
# ---------------------------------------------------------------------------

def receipt_index_to_freq(index: int) -> float:
    """Map receipt index to ascending C major scale frequency.

    Wraps into higher octaves: index 0=C4, 7=C5, 14=C6, etc.
    Wraps at index 28 to keep frequencies in comfortable range.
    """
    index = index % 28  # Wrap at 4 octaves (Pitfall 5)
    octave = index // 7
    degree = index % 7
    semitones = MAJOR_SCALE_SEMITONES[degree] + (12 * octave)
    return C4 * (SEMITONE ** semitones)


def dissonant_freq(index: int) -> float:
    """Tritone (6 semitones) above the consonant frequency at this index.

    The tritone is the maximally dissonant interval in Western music theory
    ("diabolus in musica"), making tampered receipts aurally obvious.
    """
    return receipt_index_to_freq(index) * (SEMITONE ** 6)


# ---------------------------------------------------------------------------
# Layer 3: Tone generation
# ---------------------------------------------------------------------------

def make_tone(freq: float, duration: float = TONE_DURATION) -> np.ndarray:
    """Generate a sine tone with linear attack/release envelope.

    Returns float64 array in [-1.0, 1.0] range.
    Deterministic: same freq + duration always produces identical output.
    """
    n_samples = int(SAMPLE_RATE * duration)
    t = np.linspace(0, duration, n_samples, endpoint=False)
    samples = np.sin(2 * np.pi * freq * t)

    # Build envelope
    attack_samples = int(ATTACK_MS / 1000 * SAMPLE_RATE)
    release_samples = int(RELEASE_MS / 1000 * SAMPLE_RATE)
    envelope = np.ones(n_samples)
    if attack_samples > 0:
        envelope[:attack_samples] = np.linspace(0, 1, attack_samples)
    if release_samples > 0:
        envelope[-release_samples:] = np.linspace(1, 0, release_samples)

    return samples * envelope


# ---------------------------------------------------------------------------
# Layer 4: WAV byte assembly
# ---------------------------------------------------------------------------

def _chain_to_samples(rows: list[dict], receipts: list[dict]) -> np.ndarray:
    """Generate float64 sample array for an evidence chain.

    Valid receipts get consonant tones (ascending C major scale).
    Invalid receipts get dissonant tones (tritone substitution at that position).

    Returns float64 array in [-1.0, 1.0] range. Empty chain returns empty array.
    Shared by sonify_chain (mono) and sonify_comparison (stereo).
    """
    validity = validate_receipts(rows, receipts)

    tone_arrays: list[np.ndarray] = []
    for i, valid in enumerate(validity):
        if valid:
            freq = receipt_index_to_freq(i)
        else:
            freq = dissonant_freq(i)
        tone_arrays.append(make_tone(freq))

    if tone_arrays:
        return np.concatenate(tone_arrays)
    return np.array([], dtype=np.float64)


def sonify_chain(rows: list[dict], receipts: list[dict]) -> bytes:
    """Generate mono WAV PCM bytes for an evidence chain.

    Valid receipts get consonant tones (ascending C major scale).
    Invalid receipts get dissonant tones (tritone substitution at that position).

    Returns complete WAV file as bytes (RIFF/WAVE header + 16-bit PCM data).
    Deterministic: same inputs always produce identical bytes (SONI-04).
    """
    all_samples = _chain_to_samples(rows, receipts)

    # Clip to [-1.0, 1.0] (Pitfall 3) and convert to int16 PCM
    clipped = np.clip(all_samples, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)

    # Write to in-memory BytesIO using wave module
    buf = io.BytesIO()
    with wave.open(buf, "w") as wf:
        wf.setnchannels(1)       # mono
        wf.setsampwidth(2)       # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())

    return buf.getvalue()


def sonify_comparison(
    rows_a: list[dict], receipts_a: list[dict],
    rows_b: list[dict], receipts_b: list[dict],
) -> bytes:
    """Generate stereo WAV PCM bytes (L=chain A, R=chain B).

    Shorter chain is zero-padded to match longer chain's audio length.
    Concordant receipts produce unison (same frequency L+R).
    Discordant receipts produce different frequencies (audible clash).

    Returns complete WAV file as bytes (RIFF/WAVE header + 16-bit stereo PCM).
    Deterministic: same inputs always produce identical bytes (SONI-04).
    """
    left_samples = _chain_to_samples(rows_a, receipts_a)
    right_samples = _chain_to_samples(rows_b, receipts_b)

    # Zero-pad shorter array to match longer (Pitfall 4)
    max_len = max(len(left_samples), len(right_samples))
    if len(left_samples) < max_len:
        left_samples = np.pad(left_samples, (0, max_len - len(left_samples)))
    if len(right_samples) < max_len:
        right_samples = np.pad(right_samples, (0, max_len - len(right_samples)))

    # Clip and convert to int16 PCM
    left_pcm = (np.clip(left_samples, -1.0, 1.0) * 32767).astype(np.int16)
    right_pcm = (np.clip(right_samples, -1.0, 1.0) * 32767).astype(np.int16)

    # Interleave L, R, L, R, ...
    stereo = np.column_stack((left_pcm, right_pcm)).flatten()

    # Write to in-memory BytesIO using wave module
    buf = io.BytesIO()
    with wave.open(buf, "w") as wf:
        wf.setnchannels(2)       # stereo
        wf.setsampwidth(2)       # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(stereo.tobytes())

    return buf.getvalue()
