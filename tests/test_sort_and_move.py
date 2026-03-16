"""Tests for the classification / parsing logic in sort_and_move.py.

These tests cover the pure functions that do NOT need a Graph API token or
network access: recipient detection, company extraction, document-type
detection, date parsing, and helper utilities.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Make sort_and_move importable without a real .env / Graph credentials
# ---------------------------------------------------------------------------

# Patch env vars BEFORE importing the module so it doesn't fail on startup.
os.environ.setdefault("GRAPH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GRAPH_TENANT", "consumers")

# Add source directory to path
SRC_DIR = Path(__file__).resolve().parent.parent / "SortmyPDFs"
sys.path.insert(0, str(SRC_DIR))

import sort_and_move as sm  # noqa: E402


# ===================== pick_recipient =====================


class TestPickRecipient:
    def test_chantal_in_text(self):
        assert sm.pick_recipient("Sehr geehrte Frau Chantal Rinkel", "scan.pdf") == "Chantal"

    def test_chantal_in_filename(self):
        assert sm.pick_recipient("Kein Name im Text", "Chantal_Rechnung.pdf") == "Chantal"

    def test_tim_rinkel_in_text(self):
        assert sm.pick_recipient("Herrn Tim Rinkel\nMusterstr. 1", "scan.pdf") == "Tim"

    def test_herrn_rinkel_in_text(self):
        assert sm.pick_recipient("Sehr geehrter Herrn Rinkel", "scan.pdf") == "Tim"

    def test_tim_in_filename_fallback(self):
        assert sm.pick_recipient("Kein Hinweis", "Tim_Bescheid_2024.pdf") == "Tim"

    def test_rinkel_in_filename_fallback(self):
        assert sm.pick_recipient("Kein Hinweis", "Rinkel_Rechnung.pdf") == "Tim"

    def test_unknown_returns_sonstige(self):
        assert sm.pick_recipient("Allgemeines Dokument", "scan001.pdf") == "Sonstige"

    def test_chantal_takes_priority_over_tim(self):
        # Both names present → Chantal wins (checked first)
        assert sm.pick_recipient("Tim und Chantal Rinkel", "scan.pdf") == "Chantal"


# ===================== normalize_firma =====================


class TestNormalizeFirma:
    def test_strips_whitespace(self):
        assert sm.normalize_firma("  Firma   Test  ") == "Firma Test"

    def test_cuts_noise_tokens(self):
        result = sm.normalize_firma("Muster GmbH Kundennummer 12345")
        assert "Kundennummer" not in result
        assert "Muster GmbH" in result

    def test_replaces_illegal_path_chars(self):
        result = sm.normalize_firma('Firma "Test" <GmbH>')
        assert '"' not in result
        assert "<" not in result
        assert ">" not in result

    def test_truncates_at_80(self):
        long_name = "A" * 100
        assert len(sm.normalize_firma(long_name)) == 80


# ===================== firma_key =====================


class TestFirmaKey:
    def test_case_insensitive(self):
        assert sm.firma_key("HDI Versicherung") == sm.firma_key("hdi versicherung")

    def test_drops_legal_forms(self):
        key = sm.firma_key("Muster GmbH")
        assert "gmbh" not in key
        assert "muster" in key

    def test_normalizes_punctuation(self):
        assert sm.firma_key("Firma_Test.Foo") == sm.firma_key("Firma Test Foo")

    def test_drops_versicherung(self):
        key = sm.firma_key("HDI Versicherung AG")
        assert "versicherung" not in key
        assert "ag" not in key


# ===================== firma_from_filename =====================


class TestFirmaFromFilename:
    def test_extracts_before_rechnung(self):
        result = sm.firma_from_filename("Muster GmbH Rechnung 12345.pdf")
        assert "Muster" in result

    def test_extracts_before_laborbefund(self):
        result = sm.firma_from_filename("Dr. Fischer Laborbefund 2024.pdf")
        assert "Fischer" in result

    def test_strips_leading_date(self):
        result = sm.firma_from_filename("2024-01-15 Muster Rechnung.pdf")
        assert "Muster" in result
        assert "2024" not in result

    def test_fallback_first_5_words(self):
        result = sm.firma_from_filename("Some Random Long Name Without Keywords.pdf")
        assert result is not None
        assert len(result) > 0

    def test_empty_returns_none(self):
        assert sm.firma_from_filename(".pdf") is None


# ===================== pick_firma =====================


class TestPickFirma:
    def test_hdi_in_text(self):
        assert sm.pick_firma("Ihre HDI Versicherung Nr. 123", "scan.pdf") == "HDI"

    def test_hdi_in_filename(self):
        assert sm.pick_firma("Kein Hinweis", "HDI_Rechnung.pdf") == "HDI"

    def test_gmbh_in_ocr_header(self):
        text = "Muster GmbH\nMusterstr. 1\n12345 Berlin\n\nSehr geehrter..."
        result = sm.pick_firma(text, "scan.pdf")
        assert "Muster" in result

    def test_unknown_fallback(self):
        result = sm.pick_firma("", "scan.pdf")
        # Should not crash; returns something from filename or "Unbekannt"
        assert isinstance(result, str)
        assert len(result) > 0

    def test_aliases_loaded(self):
        """Test that firma_aliases.json is found and loadable."""
        aliases = sm.load_aliases()
        assert isinstance(aliases, dict)
        # Our repo has at least Volkswagen in aliases
        assert "Volkswagen" in aliases


# ===================== pick_doc_type =====================


class TestPickDocType:
    def test_rechnung(self):
        assert sm.pick_doc_type("Ihre Rechnung vom 01.01.2024", "scan.pdf") == "Rechnung"

    def test_beitragsrechnung_over_rechnung(self):
        # "Beitragsrechnung" contains "rechnung" but should match first
        assert sm.pick_doc_type("Beitragsrechnung Nr. 123", "scan.pdf") == "Beitragsrechnung"

    def test_kaufvertrag(self):
        assert sm.pick_doc_type("Kaufvertrag über einen PKW", "scan.pdf") == "Kaufvertrag"

    def test_laborbefund(self):
        assert sm.pick_doc_type("Laborbefund vom 15.03.2024", "scan.pdf") == "Laborbefund"

    def test_bescheid(self):
        assert sm.pick_doc_type("Steuerbescheid für 2023", "scan.pdf") == "Bescheid"

    def test_fallback_dokument(self):
        assert sm.pick_doc_type("Allgemeiner Text ohne Keywords", "scan.pdf") == "Dokument"

    def test_keyword_in_filename(self):
        assert sm.pick_doc_type("Kein Keyword im Text", "Rechnung_Firma.pdf") == "Rechnung"


# ===================== pick_date =====================


class TestPickDate:
    def test_german_date_in_text(self):
        result = sm.pick_date("Berlin, den 15.03.2024", "", "scan.pdf")
        assert result == "2024-03-15"

    def test_two_digit_year(self):
        result = sm.pick_date("Datum: 01.06.24", "", "scan.pdf")
        assert result == "2024-06-01"

    def test_filename_date_fallback(self):
        result = sm.pick_date("Kein Datum im Text", "", "2024-07-20 Rechnung.pdf")
        assert result == "2024-07-20"

    def test_filename_date_with_underscore(self):
        # Previously broken due to \b not matching before '_'.
        # Fixed: now uses lookahead (?=[^0-9]|$) instead.
        result = sm.pick_date("Kein Datum", "", "2024-07-20_Rechnung.pdf")
        assert result == "2024-07-20"

    def test_created_datetime_fallback(self):
        result = sm.pick_date("Kein Datum", "2024-09-01T12:00:00Z", "scan.pdf")
        assert result == "2024-09-01"

    def test_ignores_old_dates(self):
        # Birth dates (pre-2010) should be filtered out
        result = sm.pick_date("Geb. 15.03.1985\nBerlin, 20.01.2024", "", "scan.pdf")
        assert "2024" in result
        assert "1985" not in result

    def test_prefers_header_dates(self):
        # Date near "," should score higher (city, date pattern)
        text = "Geb. 01.02.2023\nHannover, 15.06.2024\nSeite 1"
        result = sm.pick_date(text, "", "scan.pdf")
        assert result == "2024-06-15"

    def test_invalid_date_skipped(self):
        # 32.13.2024 is invalid → should not crash
        result = sm.pick_date("Datum: 32.13.2024", "2024-01-01T00:00:00Z", "scan.pdf")
        assert result is not None  # Falls back to createdDateTime


# ===================== load_state / save_state =====================


class TestState:
    def test_load_empty(self, tmp_path):
        with patch.object(sm, "STATE_PATH", tmp_path / "state.json"):
            state = sm.load_state()
            assert state == {"processed": {}}

    def test_save_and_load(self, tmp_path):
        state_file = tmp_path / "state.json"
        with patch.object(sm, "STATE_PATH", state_file):
            sm.save_state({"processed": {"abc": {"src": "test.pdf"}}})
            loaded = sm.load_state()
            assert loaded["processed"]["abc"]["src"] == "test.pdf"
