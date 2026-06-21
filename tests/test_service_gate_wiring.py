"""test_service_gate_wiring.py -- Verify service.find_on_mac and service.read_sibling_file pass GuardrailGate.

Tests the SERVICE LAYER wiring (that gate is passed through), NOT the underlying
module behavior (already tested in test_safety_surfaces.py).
"""
from unittest.mock import MagicMock, patch
import pytest


class TestFindOnMacGateWiring:
    """SAFE-01: find_on_mac passes GuardrailGate to MacFindSearcher.search()."""

    @patch("ollarma.macfind_searcher.MacFindSearcher.search")
    @patch("ollarma.macfind_config.load_macfind_config")
    def test_find_on_mac_passes_gate(self, mock_config, mock_search):
        """Gate argument to searcher.search() must be a GuardrailGate, not None."""
        from ollarma.service import find_on_mac
        mock_config.return_value = MagicMock(max_results=5, confidence_threshold=0.25)
        mock_search.return_value = MagicMock()

        find_on_mac("test query")

        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args[1]
        assert "gate" in call_kwargs, "search() must receive gate= keyword argument"
        assert call_kwargs["gate"] is not None, "gate must not be None (SAFE-01)"

    @patch("ollarma.macfind_searcher.MacFindSearcher.search")
    @patch("ollarma.macfind_config.load_macfind_config")
    def test_find_on_mac_gate_is_guardrail_gate(self, mock_config, mock_search):
        """Gate must be a GuardrailGate instance, not arbitrary object."""
        from ollarma.guardrail import GuardrailGate
        from ollarma.service import find_on_mac
        mock_config.return_value = MagicMock(max_results=5, confidence_threshold=0.25)
        mock_search.return_value = MagicMock()

        find_on_mac("test query")

        gate = mock_search.call_args[1]["gate"]
        assert isinstance(gate, GuardrailGate)


class TestReadSiblingFileGateWiring:
    """SAFE-05 wiring: read_sibling_file passes GuardrailGate to read_sibling_path()."""

    @patch("ollarma.service.list_projects")
    @patch("ollarma.sibling_reader.read_sibling_path")
    def test_read_sibling_file_passes_gate(self, mock_read, mock_projects):
        """Gate argument to read_sibling_path() must be non-None."""
        from ollarma.service import read_sibling_file
        mock_projects.return_value = {}
        mock_read.return_value = MagicMock()

        read_sibling_file("/some/path")

        mock_read.assert_called_once()
        call_kwargs = mock_read.call_args[1]
        assert "gate" in call_kwargs, "read_sibling_path() must receive gate= keyword argument"
        assert call_kwargs["gate"] is not None, "gate must not be None"

    @patch("ollarma.service.list_projects")
    @patch("ollarma.sibling_reader.read_sibling_path")
    def test_read_sibling_file_gate_is_guardrail_gate(self, mock_read, mock_projects):
        """Gate must be a GuardrailGate instance."""
        from ollarma.guardrail import GuardrailGate
        from ollarma.service import read_sibling_file
        mock_projects.return_value = {}
        mock_read.return_value = MagicMock()

        read_sibling_file("/some/path")

        gate = mock_read.call_args[1]["gate"]
        assert isinstance(gate, GuardrailGate)
