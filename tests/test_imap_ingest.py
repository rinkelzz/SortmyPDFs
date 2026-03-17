"""Tests for imap_ingest.py helper functions.

Covers pure utility functions (filename sanitization, MIME decoding, PDF
attachment detection, state serialization) without requiring a real IMAP
server or OneDrive connection.
"""

from __future__ import annotations

import email
import email.mime.application
import email.mime.multipart
import email.mime.text
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Setup: make imap_ingest importable without real credentials
# ---------------------------------------------------------------------------
os.environ.setdefault("GRAPH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GRAPH_TENANT", "consumers")
os.environ.setdefault("IMAP_HOST", "imap.example.com")
os.environ.setdefault("IMAP_USER", "user@example.com")
os.environ.setdefault("IMAP_PASSWORD", "test-password")

SRC_DIR = Path(__file__).resolve().parent.parent / "SortmyPDFs"
sys.path.insert(0, str(SRC_DIR))

import imap_ingest as ii  # noqa: E402


# ---------------------------------------------------------------------------
# _safe_name
# ---------------------------------------------------------------------------

class TestSafeName:
    def test_strips_whitespace(self):
        assert ii._safe_name("  test.pdf  ") == "test.pdf"

    def test_replaces_forbidden_chars(self):
        result = ii._safe_name("file:name/with\\bad?chars")
        assert "/" not in result
        assert "\\" not in result
        assert ":" not in result
        assert "?" not in result

    def test_collapses_spaces(self):
        assert ii._safe_name("a  b   c") == "a b c"

    def test_keeps_valid_filename(self):
        assert ii._safe_name("Rechnung_2024-01-15.pdf") == "Rechnung_2024-01-15.pdf"

    def test_replaces_angle_brackets(self):
        result = ii._safe_name("file<test>.pdf")
        assert "<" not in result
        assert ">" not in result


# ---------------------------------------------------------------------------
# _decode_mime_words
# ---------------------------------------------------------------------------

class TestDecodeMimeWords:
    def test_plain_string(self):
        assert ii._decode_mime_words("hello") == "hello"

    def test_utf8_encoded(self):
        encoded = "=?utf-8?q?Rechnung=5FJanuar.pdf?="
        result = ii._decode_mime_words(encoded)
        assert "Rechnung" in result

    def test_does_not_raise_on_garbage(self):
        # Should not raise, just return something
        result = ii._decode_mime_words("=?invalid?x?garbage?=")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# iter_pdf_attachments
# ---------------------------------------------------------------------------

def _make_pdf_email(filename: str = "test.pdf", content_type: str = "application/pdf") -> email.message.Message:
    """Build a minimal MIME email with a PDF attachment."""
    msg = email.mime.multipart.MIMEMultipart()
    pdf_part = email.mime.application.MIMEApplication(b"%PDF-1.4 fake", _subtype="pdf")
    pdf_part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(pdf_part)
    return msg


def _make_text_email() -> email.message.Message:
    msg = email.mime.multipart.MIMEMultipart()
    text_part = email.mime.text.MIMEText("Hello", "plain")
    msg.attach(text_part)
    return msg


class TestIterPdfAttachments:
    def test_detects_pdf_attachment(self):
        msg = _make_pdf_email("invoice.pdf")
        attachments = list(ii.iter_pdf_attachments(msg))
        assert len(attachments) == 1
        name, data = attachments[0]
        assert name == "invoice.pdf"
        assert data == b"%PDF-1.4 fake"

    def test_ignores_text_parts(self):
        msg = _make_text_email()
        assert list(ii.iter_pdf_attachments(msg)) == []

    def test_uses_default_name_when_filename_missing(self):
        msg = email.mime.multipart.MIMEMultipart()
        pdf_part = email.mime.application.MIMEApplication(b"%PDF", _subtype="pdf")
        # No filename header → should fall back to "attachment.pdf"
        pdf_part.add_header("Content-Disposition", "attachment")
        msg.attach(pdf_part)
        attachments = list(ii.iter_pdf_attachments(msg))
        assert len(attachments) == 1
        assert attachments[0][0] == "attachment.pdf"

    def test_detects_pdf_by_filename_extension(self):
        """Even if content-type is octet-stream, a .pdf filename should be picked up."""
        msg = email.mime.multipart.MIMEMultipart()
        part = email.mime.application.MIMEApplication(b"%PDF", _subtype="octet-stream")
        part.add_header("Content-Disposition", "attachment", filename="doc.pdf")
        msg.attach(part)
        attachments = list(ii.iter_pdf_attachments(msg))
        assert len(attachments) == 1


# ---------------------------------------------------------------------------
# State serialization / atomic write
# ---------------------------------------------------------------------------

class TestSaveLoadState:
    def test_round_trip(self, tmp_path):
        state_file = tmp_path / "state_imap.json"
        state = {"processed_uids": {"123": {"ts": "2024-01-01", "uploaded": 1}}, "processed_hashes": {}}

        with patch.object(ii, "STATE_PATH", state_file):
            ii.save_state(state)
            loaded = ii.load_state()

        assert loaded["processed_uids"]["123"]["uploaded"] == 1

    def test_atomic_write_uses_tmp(self, tmp_path):
        state_file = tmp_path / "state_imap.json"
        state = {"processed_uids": {}, "processed_hashes": {}}

        with patch.object(ii, "STATE_PATH", state_file):
            ii.save_state(state)
            # Final file must exist; tmp file must be gone
            assert state_file.exists()
            assert not state_file.with_suffix(".tmp").exists()

    def test_load_returns_default_when_missing(self, tmp_path):
        missing = tmp_path / "no_such_state.json"
        with patch.object(ii, "STATE_PATH", missing):
            result = ii.load_state()
        assert result == {"processed_uids": {}, "processed_hashes": {}}


# ---------------------------------------------------------------------------
# require_env
# ---------------------------------------------------------------------------

class TestRequireEnv:
    def test_passes_when_all_set(self):
        with patch.object(ii, "IMAP_HOST", "imap.test.com"), \
             patch.object(ii, "IMAP_USER", "u"), \
             patch.object(ii, "IMAP_PASSWORD", "p"):
            ii.require_env()  # should not raise

    def test_raises_when_missing(self):
        with patch.object(ii, "IMAP_HOST", None), \
             patch.object(ii, "IMAP_USER", "u"), \
             patch.object(ii, "IMAP_PASSWORD", "p"):
            with pytest.raises(SystemExit):
                ii.require_env()
