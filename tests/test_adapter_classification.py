"""Tests for the adapter governance classification model.

Covers classification / canon_status / has_canon inference (back-compat) and the
consistency invariants added so non-science repos can connect without a CANON.
"""
import pytest
from pydantic import ValidationError

from ollarma.fleet import AdapterConfig, parse_markdown_adapter


def _cfg(**kw) -> AdapterConfig:
    base = dict(project_name="x", project_root="/tmp/x")
    base.update(kw)
    return AdapterConfig(**base)


class TestGovernanceInference:
    def test_has_canon_true_infers_science_present(self) -> None:
        c = _cfg(has_canon=True)
        assert (c.classification, c.canon_status) == ("science", "present")

    def test_default_infers_other_not_applicable(self) -> None:
        c = _cfg()
        assert (c.classification, c.canon_status, c.has_canon) == (
            "other", "not_applicable", False,
        )

    def test_explicit_science_pending(self) -> None:
        c = _cfg(classification="science", canon_status="pending")
        assert c.has_canon is False


class TestGovernanceInvariants:
    @pytest.mark.parametrize("kw", [
        dict(classification="other", canon_status="present"),       # other must be n/a
        dict(classification="other", canon_status="not_applicable", has_canon=True),
        dict(classification="science", canon_status="not_applicable"),  # science needs canon
        dict(classification="science", canon_status="present", has_canon=False),
        dict(classification="science", canon_status="pending", has_canon=True),
    ])
    def test_invalid_combinations_rejected(self, kw: dict) -> None:
        with pytest.raises(ValidationError):
            _cfg(**kw)

    def test_other_not_applicable_ok(self) -> None:
        assert _cfg(classification="other", canon_status="not_applicable").has_canon is False

    def test_science_present_ok(self) -> None:
        assert _cfg(classification="science", canon_status="present", has_canon=True).has_canon


class TestMarkdownParsing:
    def test_parses_classification_and_canon_status(self) -> None:
        md = (
            "# Adapter: Foo\n**Path:** /tmp/foo\n\n## Notes\n\n"
            "classification: science\ncanon_status: pending\nhas_canon: false\n"
        )
        result = parse_markdown_adapter(md)
        assert result["classification"] == "science"
        assert result["canon_status"] == "pending"
        assert result["has_canon"] is False
