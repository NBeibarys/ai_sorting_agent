"""Synchronous batch wrapper around the asynchronous ADK batch workflow.

A single classify_batch() call sends ALL rows to the classifier+verifier
SequentialAgent (2 LLM calls) and applies verifier corrections in Python. If
the verifier rejects any rows, a retry loop (max 2 iterations) re-runs ONLY
the rejected subset (2 more LLM calls). Total: 2-4 LLM calls for the whole
dataset regardless of row count, replacing the old per-row loop (one ADK
invocation per row = up to 1374 calls for 687 rows).
"""
import asyncio
import json
import os
import uuid

import certifi
from google.adk.agents import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .agent import build_root_agent

APP_NAME = "startup_country_sorter"

_VALID_BUCKETS = (
    "Uzbekistan", "Turkiye", "Georgia", "Kyrgyzstan", "Azerbaijan",
    "USA", "Kazakhstan", "Mong. Turkmenistan Tajikistan", "Other",
)


def _as_dict(value):
    """Normalize ADK structured output (Pydantic / dict / list / JSON str)."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        return json.loads(value)
    if value is None:
        return None
    raise ValueError(f"Unsupported structured agent output: {type(value).__name__}")


def _safe_bucket(bucket) -> str:
    """Coerce to a valid bucket string, defaulting to Other."""
    return bucket if bucket in _VALID_BUCKETS else "Other"


class AdkSorterWorkflow:
    """Owns only immutable configuration; each invocation is isolated."""

    def __init__(self, model: str):
        os.environ["SSL_CERT_FILE"] = certifi.where()
        self.model = model

    async def _invoke_async(self, country_items: list[dict]) -> tuple[dict, dict]:
        """Run classifier -> verifier on a batch. Returns (cls_by_row_id, ver_by_row_id)."""
        session_service = InMemorySessionService()
        runner = Runner(
            app_name=APP_NAME,
            agent=build_root_agent(self.model),
            session_service=session_service,
        )
        session_id = uuid.uuid4().hex
        await session_service.create_session(
            app_name=APP_NAME, user_id="sorter", session_id=session_id,
        )
        payload = json.dumps(country_items, ensure_ascii=False)
        new_message = types.Content(
            role="user", parts=[types.Part(text=payload)],
        )
        async for _event in runner.run_async(
            user_id="sorter", session_id=session_id,
            new_message=new_message, run_config=RunConfig(max_llm_calls=6),
        ):
            pass

        session = await session_service.get_session(
            app_name=APP_NAME, user_id="sorter", session_id=session_id,
        )
        if session is None:
            raise RuntimeError("ADK session disappeared before result collection.")

        batch_cls = _as_dict(session.state.get("batch_classifications")) or {}
        batch_ver = _as_dict(session.state.get("batch_verdicts")) or {}
        if not isinstance(batch_cls, dict):
            batch_cls = {}
        if not isinstance(batch_ver, dict):
            batch_ver = {}

        classifications = self._index_by_row_id(batch_cls.get("items", []))
        verdicts = self._index_by_row_id(batch_ver.get("items", []))
        return classifications, verdicts

    @staticmethod
    def _index_by_row_id(items) -> dict:
        indexed = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            rid = item.get("row_id")
            if rid is None:
                continue
            try:
                indexed[int(rid)] = item
            except (TypeError, ValueError):
                continue
        return indexed

    @staticmethod
    def _merge(cls_map: dict, ver_map: dict, items: list[dict]) -> dict:
        """row_id -> (final_bucket, needs_review), applying verifier corrections.

        needs_review is the classifier's ambiguity flag and is preserved even
        when the verifier corrects the bucket, so ambiguous rows still surface
        for human review.
        """
        merged = {}
        for item in items:
            try:
                rid = int(item["row_id"])
            except (KeyError, TypeError, ValueError):
                continue
            verdict = ver_map.get(rid, {})
            cls = cls_map.get(rid, {})
            approved = bool(verdict.get("approved"))
            if approved:
                bucket = cls.get("country_bucket")
            elif verdict.get("corrected_bucket"):
                bucket = verdict["corrected_bucket"]
            else:
                bucket = cls.get("country_bucket")
            needs_review = bool(cls.get("needs_review", False))
            merged[rid] = (_safe_bucket(bucket), needs_review)
        return merged

    CHUNK_SIZE = 50  # 100+ hangs the LLM; 50 confirmed working (~25s)

    def _classify_chunk(self, chunk: list[dict]) -> dict[int, tuple[str, bool]]:
        """Classify a single chunk (≤50 rows) with retry loop. Returns row_id -> (bucket, needs_review).

        Retry loop (max 2 iterations, 2-4 LLM calls per chunk):
          Iteration 1: classify ALL chunk items + verify ALL items.
          If the verifier rejects any rows, re-run ONLY the rejected subset
          through classifier + verifier (iteration 2). After max iterations the
          verifier's corrected_bucket is accepted for any still-rejected rows
          (applied in _merge). The LLM payload is always only {row_id,
          country_raw} -- no verifier feedback is injected (the classifier
          prompt has no {verifier_feedback} hook).
        """
        MAX_ITERATIONS = 2
        raw_by_id: dict[int, str] = {}
        for item in chunk:
            try:
                raw_by_id[int(item["row_id"])] = item.get("country_raw", "")
            except (KeyError, TypeError, ValueError):
                continue

        merged: dict[int, tuple[str, bool]] = {}
        active_items = chunk  # iteration 1 = every input row
        for iteration in range(MAX_ITERATIONS):
            classifications, verdicts = asyncio.run(self._invoke_async(active_items))
            merged.update(self._merge(classifications, verdicts, active_items))

            rejected_ids = [
                rid for rid, v in verdicts.items() if not bool(v.get("approved"))
            ]
            if not rejected_ids:
                break  # every active row approved -> done

            # Next iteration re-runs ONLY the verifier-rejected subset.
            active_items = [
                {"row_id": rid, "country_raw": raw_by_id.get(rid, "")}
                for rid in rejected_ids
                if rid in raw_by_id
            ]
            if not active_items:
                break  # nothing retriable left

        return merged

    def classify_batch(self, country_items: list[dict]) -> list[tuple[str, bool]]:
        """Classify a batch of rows; return (bucket, needs_review) tuples in input order.

        Input:  [{"row_id": 0, "country_raw": "Astana"}, ...]
        Output: [("Kazakhstan", False), ...] -- one (bucket, needs_review) per input, in row_id order.

        Chunks input at CHUNK_SIZE (50) to avoid LLM hangs on large batches.
        Each chunk runs independently with its own retry loop. Results are
        concatenated in row_id order. Interface unchanged from non-chunked version.
        """
        if not country_items:
            return []

        total = len(country_items)
        all_merged: dict[int, tuple[str, bool]] = {}

        if total <= self.CHUNK_SIZE:
            all_merged = self._classify_chunk(country_items)
        else:
            num_chunks = (total + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE
            for i in range(0, total, self.CHUNK_SIZE):
                chunk = country_items[i:i + self.CHUNK_SIZE]
                chunk_num = i // self.CHUNK_SIZE + 1
                print(f"  [chunk {chunk_num}/{num_chunks}] classifying {len(chunk)} rows...", flush=True)
                all_merged.update(self._classify_chunk(chunk))

        out = []
        for item in country_items:
            try:
                rid = int(item["row_id"])
            except (KeyError, TypeError, ValueError):
                out.append(("Other", False))
                continue
            out.append(all_merged.get(rid, ("Other", False)))
        return out

    def classify(self, country_raw: str) -> tuple[str, bool]:
        """Single-row wrapper around classify_batch (for --limit 1 testing)."""
        return self.classify_batch([{"row_id": 0, "country_raw": country_raw}])[0]
