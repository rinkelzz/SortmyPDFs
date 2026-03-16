"""Tests for the SortmyPDFs FastAPI dashboard.

Covers helper functions, input-validation logic, and critical HTTP endpoints
without requiring a real OneDrive / Graph API connection.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Setup: make dashboard importable in test environment
# ---------------------------------------------------------------------------
os.environ.setdefault("GRAPH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GRAPH_TENANT", "consumers")

DASH_DIR = Path(__file__).resolve().parent.parent / "SortmyPDFs" / "dashboard"
SRC_DIR = Path(__file__).resolve().parent.parent / "SortmyPDFs"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(DASH_DIR))

import app as dashboard_app  # noqa: E402


# ---------------------------------------------------------------------------
# _valid_graph_id
# ---------------------------------------------------------------------------

class TestValidGraphId:
    def test_accepts_typical_id(self):
        assert dashboard_app._valid_graph_id("01ABC123DEFGHIJ456KLM7!789") is True

    def test_accepts_alphanumeric(self):
        assert dashboard_app._valid_graph_id("ABCDE12345") is True

    def test_accepts_special_chars(self):
        assert dashboard_app._valid_graph_id("item-id_foo.bar:baz") is True

    def test_rejects_empty(self):
        assert dashboard_app._valid_graph_id("") is False

    def test_rejects_too_short(self):
        assert dashboard_app._valid_graph_id("AB") is False

    def test_rejects_path_traversal(self):
        assert dashboard_app._valid_graph_id("../../etc/passwd") is False

    def test_rejects_space(self):
        assert dashboard_app._valid_graph_id("id with space") is False

    def test_rejects_angle_brackets(self):
        assert dashboard_app._valid_graph_id("<script>") is False

    def test_rejects_semicolon(self):
        assert dashboard_app._valid_graph_id("id;drop") is False


# ---------------------------------------------------------------------------
# _folder_key
# ---------------------------------------------------------------------------

class TestFolderKey:
    def test_drops_gmbh(self):
        assert "gmbh" not in dashboard_app._folder_key("Muster GmbH")

    def test_drops_ag(self):
        key = dashboard_app._folder_key("Muster AG")
        assert "ag" not in key.split()

    def test_casefolds(self):
        assert dashboard_app._folder_key("MUSTER") == dashboard_app._folder_key("muster")

    def test_normalizes_special_chars(self):
        key = dashboard_app._folder_key("Muster.GmbH_(Test)")
        assert "." not in key
        assert "(" not in key


# ---------------------------------------------------------------------------
# _parse_log_ts
# ---------------------------------------------------------------------------

class TestParseLogTs:
    def test_valid_timestamp(self):
        from datetime import timezone
        result = dashboard_app._parse_log_ts("hourly-2024-01-15T10-30-00Z.log")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.tzinfo == timezone.utc

    def test_invalid_returns_none(self):
        assert dashboard_app._parse_log_ts("not-a-log-file.txt") is None

    def test_malformed_timestamp_returns_none(self):
        assert dashboard_app._parse_log_ts("hourly-bad-timestamp.log") is None


# ---------------------------------------------------------------------------
# _count_sonstige and _top_firms
# ---------------------------------------------------------------------------

SAMPLE_PROCESSED = {
    "id1": {"dest": "SortmyPDFs/Tim/FirmaA", "new": "2024-01-01_FirmaA_Rechnung.pdf"},
    "id2": {"dest": "SortmyPDFs/Sonstige/FirmaB", "new": "2024-01-02_FirmaB_Dokument.pdf"},
    "id3": {"dest": "SortmyPDFs/Sonstige/FirmaC", "new": "2024-01-03_FirmaC_Bescheid.pdf"},
    "id4": {"dest": "SortmyPDFs/Chantal/FirmaA", "new": "2024-01-04_FirmaA_Rechnung.pdf"},
}


class TestCountSonstige:
    def test_counts_sonstige_entries(self):
        assert dashboard_app._count_sonstige(SAMPLE_PROCESSED) == 2

    def test_empty_processed(self):
        assert dashboard_app._count_sonstige({}) == 0


class TestTopFirms:
    def test_returns_top_firms(self):
        firms = dict(dashboard_app._top_firms(SAMPLE_PROCESSED, limit=10))
        assert firms.get("FirmaA") == 2
        assert firms.get("FirmaB") == 1

    def test_limit_respected(self):
        assert len(dashboard_app._top_firms(SAMPLE_PROCESSED, limit=1)) == 1

    def test_sorted_by_count_desc(self):
        firms = dashboard_app._top_firms(SAMPLE_PROCESSED, limit=10)
        counts = [c for _, c in firms]
        assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# _build_state_tree
# ---------------------------------------------------------------------------

class TestBuildStateTree:
    def test_tree_structure(self):
        tree = dashboard_app._build_state_tree(SAMPLE_PROCESSED)
        assert "SortmyPDFs" in tree["children"]
        sm_node = tree["children"]["SortmyPDFs"]
        assert "Tim" in sm_node["children"]
        assert "Sonstige" in sm_node["children"]

    def test_files_attached_to_leaf(self):
        tree = dashboard_app._build_state_tree(SAMPLE_PROCESSED)
        firma_node = tree["children"]["SortmyPDFs"]["children"]["Tim"]["children"]["FirmaA"]
        assert len(firma_node["files"]) == 1
        assert firma_node["files"][0]["item_id"] == "id1"

    def test_empty_processed(self):
        tree = dashboard_app._build_state_tree({})
        assert tree["children"] == {}
        assert tree["files"] == []
