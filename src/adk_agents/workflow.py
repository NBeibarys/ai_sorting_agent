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
from .prompts import build_dedup_instruction
from .schemas import DedupGroups

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
    """Owns only immutable configuration; each invocation is isolated.

    The root agent is built once in __init__ (audit_v3 BUG 6): ADK agents are
    config-only and reusable; the per-invocation session (InMemorySessionService
    + unique session_id) provides isolation, so rebuilding the agent on every
    chunk was wasted work (14 rebuilds for a 7-chunk batch).
    """

    def __init__(self, model: str, country_field_label: str = ""):
        os.environ["SSL_CERT_FILE"] = certifi.where()
        self.model = model
        self.country_field_label = country_field_label
        self._agent = build_root_agent(model, country_field_label)
        # Lazy genai Client for the semantic-dedup pass. Vertex AI is already
        # configured via env (GOOGLE_GENAI_USE_VERTEXAI / GOOGLE_CLOUD_PROJECT
        # / GOOGLE_CLOUD_LOCATION) by Config.from_env, so a bare Client() picks
        # up the same Vertex credentials the ADK path uses.
        self._genai_client = None
        self._dedup_instruction = build_dedup_instruction()

    def _get_genai_client(self):
        """Lazily build a google-genai Client bound to Vertex AI.

        Built on first use (not in __init__) so a dry-run or a run with LLM
        dedup disabled never pays the client-construction cost or trips over
        env var ordering.
        """
        if self._genai_client is None:
            from google import genai  # local import keeps module import cheap
            self._genai_client = genai.Client()
        return self._genai_client

    async def _invoke_dedup_async(self, names: list[str]) -> list[list[str]]:
        """One LLM call: ask Gemini to group semantically-equal startup names.

        Returns a list of groups (each a list of verbatim input names) with
        2+ members. On any error, or if the model returns no usable groups,
        returns an empty list -- the caller treats that as "no semantic
        duplicates found", which is the safe (keep-everything) fallback.

        Conservative by design: the prompt instructs the model to only group
        names that are CLEARLY the same startup, and to never group when in
        doubt. A false positive (merging two distinct startups) is far worse
        than a false negative (leaving a duplicate in place).
        """
        client = self._get_genai_client()
        payload = json.dumps(names, ensure_ascii=False)
        response = await client.aio.models.generate_content(
            model=self.model,
            contents=payload,
            config=types.GenerateContentConfig(
                system_instruction=self._dedup_instruction,
                temperature=0,
                response_mime_type="application/json",
                response_schema=DedupGroups,
            ),
        )
        parsed = getattr(response, "parsed", None)
        if parsed is None or not isinstance(parsed, DedupGroups):
            # Fallback: parse the text field as JSON. Some Vertex responses
            # populate .text but not .parsed.
            text = getattr(response, "text", "") or ""
            if not text.strip():
                return []
            data = json.loads(text)
            parsed = DedupGroups.model_validate(data)

        # Filter to groups of 2+ members whose names actually appear in the
        # input. The model is told to omit single-name groups and to only use
        # verbatim input names, but we enforce it defensively here -- never
        # trust LLM output to be well-formed.
        name_set = set(names)
        groups: list[list[str]] = []
        for grp in parsed.groups or []:
            members = [n for n in (grp.names or []) if n in name_set]
            if len(members) >= 2:
                groups.append(members)
        return groups

    def dedup_names_batch(self, names: list[str]) -> list[list[str]]:
        """Synchronous wrapper: group semantically-equal startup names.

        Input:  a flat list of startup name strings (up to ~500).
        Output: a list of groups, each a list of 2+ verbatim input names that
                refer to the same startup. An empty list means "no semantic
                duplicates found" (the safe fallback on any failure too).

        Runs a SINGLE LLM call for the whole batch. If the input exceeds the
        LLM's comfortable single-call limit (DEDUP_NAMES_CHUNK = 500), the
        names are chunked and each chunk gets its own call; results are
        concatenated. A chunk that fails is skipped (its names are all kept),
        so a transient API error never drops real startups.
        """
        if not names:
            return []

        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("loop closed")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        DEDUP_NAMES_CHUNK = 500  # safe single-call ceiling for name lists

        all_groups: list[list[str]] = []
        for start in range(0, len(names), DEDUP_NAMES_CHUNK):
            chunk = names[start:start + DEDUP_NAMES_CHUNK]
            try:
                groups = loop.run_until_complete(self._invoke_dedup_async(chunk))
            except Exception as exc:  # noqa: BLE001 - keep startups on failure
                print(
                    f"  llm-dedup: chunk starting at {start} failed "
                    f"({type(exc).__name__}: {exc}); keeping all {len(chunk)} "
                    f"names ungrouped.",
                    flush=True,
                )
                continue
            all_groups.extend(groups)
        return all_groups

    async def _invoke_async(self, country_items: list[dict]) -> tuple[dict, dict]:
        """Run classifier -> verifier on a batch. Returns (cls_by_row_id, ver_by_row_id)."""
        session_service = InMemorySessionService()
        runner = Runner(
            app_name=APP_NAME,
            agent=self._agent,
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

    CHUNK_SIZE = 100  # 100 confirmed safe; 100+ hangs the LLM (~25s/chunk)

    def _classify_chunk(self, chunk: list[dict]) -> dict[int, tuple[str, bool]]:
        """Classify a single chunk (<=CHUNK_SIZE rows) with retry loop. Returns row_id -> (bucket, needs_review).

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
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError("loop closed")
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            classifications, verdicts = loop.run_until_complete(self._invoke_async(active_items))
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

    def classify_batch(
        self,
        country_items: list[dict],
        *,
        on_chunk_done=None,
    ) -> tuple[list[tuple[str, bool]], set[int]]:
        """Classify a batch of rows; return (bucket, needs_review) tuples in input order.

        Input:  [{"row_id": 0, "country_raw": "Astana"}, ...]
        Output: [("Kazakhstan", False), ...] -- one (bucket, needs_review) per input, in row_id order.

        Chunks input at CHUNK_SIZE (100) to avoid LLM hangs on large batches.
        Each chunk runs independently with its own retry loop. If the Gemini
        API fails on one chunk, that chunk is retried with backoff (3 retries,
        5s/10s/20s); if it still fails, only that chunk is marked 'Other' and
        an error is logged, while the remaining chunks continue. After each
        successful chunk, on_chunk_done(merged_for_this_chunk) is invoked so
        the caller can checkpoint progress and avoid re-paying for re-run
        chunks on a mid-batch crash. Results are concatenated in row_id order.
        Interface changed in audit_v3: returns (results_list, errored_rids)
        where errored_rids is the set of row_ids whose chunk exhausted all
        retries and was marked 'Other'. Callers can surface these in their
        error report so a fully-failed chunk is visible (not silently 'Other').
        """
        if not country_items:
            return [], set()

        total = len(country_items)
        all_merged: dict[int, tuple[str, bool]] = {}
        errored_rids: set[int] = set()

        def _process_chunk(chunk: list[dict], chunk_num: int, num_chunks: int) -> None:
            """Classify one chunk with 3-attempt backoff retry (5s/10s/20s)."""
            backoff_seconds = (5, 10, 20)
            last_exc: Exception | None = None
            for attempt in range(len(backoff_seconds) + 1):
                try:
                    merged = self._classify_chunk(chunk)
                    all_merged.update(merged)
                    if on_chunk_done is not None:
                        on_chunk_done(merged)
                    return
                except Exception as exc:  # noqa: BLE001 - retry any failure
                    last_exc = exc
                    if attempt < len(backoff_seconds):
                        wait = backoff_seconds[attempt]
                        print(
                            f"  [chunk {chunk_num}/{num_chunks}] failed "
                            f"(attempt {attempt + 1}/{len(backoff_seconds) + 1}): "
                            f"{type(exc).__name__}: {exc} -- retrying in {wait}s",
                            flush=True,
                        )
                        import time as _time
                        _time.sleep(wait)
            # All retries exhausted: mark only this chunk's rows as Other.
            print(
                f"  [chunk {chunk_num}/{num_chunks}] FAILED after {len(backoff_seconds) + 1} "
                f"attempts: {type(last_exc).__name__}: {last_exc} -- "
                f"marking {len(chunk)} rows as Other",
                flush=True,
            )
            for item in chunk:
                try:
                    rid = int(item["row_id"])
                    errored_rids.add(rid)
                    all_merged[rid] = ("Other", False)
                except (KeyError, TypeError, ValueError):
                    pass

        if total <= self.CHUNK_SIZE:
            _process_chunk(country_items, 1, 1)
        else:
            num_chunks = (total + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE
            for i in range(0, total, self.CHUNK_SIZE):
                chunk = country_items[i:i + self.CHUNK_SIZE]
                chunk_num = i // self.CHUNK_SIZE + 1
                print(f"  [chunk {chunk_num}/{num_chunks}] classifying {len(chunk)} rows...", flush=True)
                _process_chunk(chunk, chunk_num, num_chunks)

        out = []
        for item in country_items:
            try:
                rid = int(item["row_id"])
            except (KeyError, TypeError, ValueError):
                out.append(("Other", False))
                continue
            out.append(all_merged.get(rid, ("Other", False)))
        return out, errored_rids

    def classify(self, country_raw: str) -> tuple[str, bool]:
        """Single-row wrapper around classify_batch (for --limit 1 testing)."""
        results, _errored = self.classify_batch([{"row_id": 0, "country_raw": country_raw}])
        return results[0]
