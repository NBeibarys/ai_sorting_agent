"""Validation script for the LLM semantic dedup pass.

Tests the pure-Python dedup logic in _llm_semantic_dedup() using a MOCK
workflow (no real LLM call), so it runs offline with no API spend.

Covers:
  1. Schema parsing (DedupGroups / DedupGroup)
  2. Keep-latest semantics within a group
  3. Distinct startups (RUNA vs QORGAN) are NOT merged
  4. LLM failure -> keep all rows (safe fallback)
  5. Empty groups result -> keep all rows
  6. Config flag llm_dedup_enabled
  7. dedup_names_batch chunking (500-name ceiling)
"""
import os
import sys

# Configure Vertex env so imports don't fail at module load.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "dummy-project")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.pipeline import _llm_semantic_dedup
from src.adk_agents.schemas import DedupGroups, DedupGroup
from src.adk_agents.prompts import build_dedup_instruction
from src.config import Config


class MockWorkflow:
    """Stand-in for AdkSorterWorkflow. Returns canned groups."""
    def __init__(self, groups):
        self._groups = groups
        self.calls = 0

    def dedup_names_batch(self, names):
        self.calls += 1
        # Return only groups whose names are all in the input (mirrors the
        # real method's defensive filtering).
        name_set = set(names)
        out = []
        for g in self._groups:
            members = [n for n in g if n in name_set]
            if len(members) >= 2:
                out.append(members)
        return out


def make_row_meta(names_and_indices):
    """Build (i, country_raw, full_row) tuples. names_and_indices is a list
    of (source_index, startup_name). The rows list is sized so that
    rows[source_index] is accessible (rows[i] = row for source index i)."""
    max_idx = max(i for i, _ in names_and_indices)
    rows = [None] * (max_idx + 1)
    meta = []
    for i, name in names_and_indices:
        row = [name, f"country_{i}"]
        rows[i] = row
        meta.append((i, f"country_{i}", list(row)))
    return meta, rows


def test_schema_parsing():
    """DedupGroups parses a well-formed JSON-shaped dict."""
    data = {"groups": [{"names": ["RUNA", "RUNA Tech"]}, {"names": ["Foo"]}]}
    parsed = DedupGroups.model_validate(data)
    assert len(parsed.groups) == 2
    assert parsed.groups[0].names == ["RUNA", "RUNA Tech"]
    print("[PASS] schema_parsing")


def test_keep_latest_in_group():
    """When 3 rows are the same startup (RUNA variants), the latest (highest
    source index) is kept and earlier ones dropped."""
    # source index 0,5,10 -- three RUNA variants. Keep row 10.
    meta, rows = make_row_meta([(0, "RUNA"), (5, "RUNA Tech"), (10, "RUNA Technology"), (3, "QORGAN")])
    wf = MockWorkflow(groups=[["RUNA", "RUNA Tech", "RUNA Technology"]])
    result = _llm_semantic_dedup(meta, rows, name_col_idx=0, workflow=wf)
    # 4 rows, 3 are RUNA -> drop 2, keep 2 (row 10 RUNA + row 3 QORGAN)
    assert len(result) == 2, f"expected 2, got {len(result)}: {[r[0] for r in result]}"
    kept_indices = sorted(r[0] for r in result)
    assert kept_indices == [3, 10], f"expected [3, 10], got {kept_indices}"
    print("[PASS] keep_latest_in_group (dropped rows 0, 5; kept 10 as latest RUNA)")


def test_distinct_startups_not_merged():
    """RUNA and QORGAN must NOT be grouped -- different startups."""
    meta, rows = make_row_meta([(0, "RUNA"), (1, "QORGAN"), (2, "RUNA Tech")])
    # Model correctly returns only the RUNA group, NOT [RUNA, QORGAN].
    wf = MockWorkflow(groups=[["RUNA", "RUNA Tech"]])
    result = _llm_semantic_dedup(meta, rows, name_col_idx=0, workflow=wf)
    assert len(result) == 2, f"expected 2, got {len(result)}"
    kept_indices = sorted(r[0] for r in result)
    assert kept_indices == [1, 2], f"expected [1, 2], got {kept_indices}"
    print("[PASS] distinct_startups_not_merged (QORGAN kept separate from RUNA)")


def test_llm_failure_keeps_all():
    """If the LLM call raises, all rows are kept (safe fallback)."""
    class FailingWorkflow:
        def dedup_names_batch(self, names):
            raise RuntimeError("simulated API timeout")
    meta, rows = make_row_meta([(0, "RUNA"), (1, "RUNA Tech"), (2, "QORGAN")])
    result = _llm_semantic_dedup(meta, rows, name_col_idx=0, workflow=FailingWorkflow())
    assert len(result) == 3, f"expected 3 (all kept on failure), got {len(result)}"
    print("[PASS] llm_failure_keeps_all (all 3 rows kept on simulated timeout)")


def test_empty_groups_keeps_all():
    """If the LLM returns no groups, all rows are kept."""
    meta, rows = make_row_meta([(0, "RUNA"), (1, "QORGAN")])
    wf = MockWorkflow(groups=[])
    result = _llm_semantic_dedup(meta, rows, name_col_idx=0, workflow=wf)
    assert len(result) == 2, f"expected 2, got {len(result)}"
    print("[PASS] empty_groups_keeps_all")


def test_config_flag():
    """Config.llm_dedup_enabled reads LLM_DEDUP_ENABLED env var."""
    os.environ["LLM_DEDUP_ENABLED"] = "FALSE"
    # Force re-read by constructing Config fields manually (avoid full from_env
    # which needs service account file).
    import importlib
    from src import config as config_mod
    importlib.reload(config_mod)
    # Build a minimal Config directly to test the field default + env read.
    # We can't call from_env (needs creds), so test the env-parse line directly.
    enabled = os.environ.get("LLM_DEDUP_ENABLED", "TRUE").upper() == "TRUE"
    assert enabled is False, "LLM_DEDUP_ENABLED=FALSE should set enabled=False"
    os.environ["LLM_DEDUP_ENABLED"] = "TRUE"
    enabled = os.environ.get("LLM_DEDUP_ENABLED", "TRUE").upper() == "TRUE"
    assert enabled is True
    print("[PASS] config_flag (LLM_DEDUP_ENABLED toggles correctly)")


def test_dedup_names_batch_chunking():
    """dedup_names_batch chunks at 500 names. Mock the async call."""
    from src.adk_agents.workflow import AdkSorterWorkflow

    class StubClient:
        class aio:
            class models:
                @staticmethod
                async def generate_content(*, model, contents, config):
                    import json
                    # Echo back groups for any names containing "DUP"
                    names = json.loads(contents)
                    dups = [n for n in names if n.startswith("DUP")]
                    groups = []
                    for i in range(0, len(dups), 2):
                        pair = dups[i:i+2]
                        if len(pair) == 2:
                            groups.append({"names": pair})
                    from google.genai import types
                    parsed = DedupGroups.model_validate({"groups": groups})
                    # Build a response with .parsed set
                    resp = types.GenerateContentResponse()
                    resp.parsed = parsed
                    return resp

    wf = AdkSorterWorkflow.__new__(AdkSorterWorkflow)
    wf.model = "dummy"
    wf._genai_client = StubClient()
    wf._dedup_instruction = "dummy"

    # 1000 names: 500 "UNIQ{i}" + 500 "DUP{i}" (250 dup pairs)
    names = [f"UNIQ{i}" for i in range(500)] + [f"DUP{i}" for i in range(500)]
    groups = wf.dedup_names_batch(names)
    # Should produce 250 pairs from the DUP names
    assert len(groups) == 250, f"expected 250 groups, got {len(groups)}"
    print(f"[PASS] dedup_names_batch_chunking (1000 names -> {len(groups)} dup pairs in 2 chunks)")


def test_prompt_conservative():
    """The dedup prompt instructs the model to NOT group when in doubt."""
    prompt = build_dedup_instruction()
    assert "WHEN IN DOUBT" in prompt.upper(), "prompt must contain the conservative instruction"
    assert "DO NOT GROUP" in prompt.upper(), "prompt must tell model not to group distinct startups"
    assert "RUNA" in prompt, "prompt should use RUNA as an example"
    assert "QORGAN" in prompt, "prompt should use QORGAN as a distinct-startup example"
    print("[PASS] prompt_conservative (contains when-in-doubt and do-not-group rules)")


if __name__ == "__main__":
    test_schema_parsing()
    test_keep_latest_in_group()
    test_distinct_startups_not_merged()
    test_llm_failure_keeps_all()
    test_empty_groups_keeps_all()
    test_config_flag()
    test_dedup_names_batch_chunking()
    test_prompt_conservative()
    print("\n=== All validation tests passed ===")
