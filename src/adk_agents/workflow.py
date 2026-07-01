"""Synchronous batch-facing wrapper around the asynchronous ADK workflow."""
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


def _as_dict(value) -> dict:
    """Normalize ADK structured output (Pydantic / dict / JSON str) to dict."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise ValueError(f"Unsupported structured agent output: {type(value).__name__}")


class AdkSorterWorkflow:
    """Owns only immutable configuration; batch threads must not share state."""

    def __init__(self, model: str):
        # certifi provides a consistent trust store across Windows deployments.
        os.environ["SSL_CERT_FILE"] = certifi.where()
        self.model = model

    async def _invoke_async(
        self, country_raw: str
    ) -> dict:
        session_service = InMemorySessionService()
        runner = Runner(
            app_name=APP_NAME,
            agent=build_root_agent(self.model),
            session_service=session_service,
        )
        session_id = uuid.uuid4().hex
        # Seed loop-control state so the classifier sees empty feedback on the
        # first attempt and the gate can route deterministically. Mirrors the
        # sibling fellowship repo's initial_state pattern.
        initial_state = {
            "verifier_feedback": "",
            "attempt": 1,
            "review_approved": False,
        }
        await session_service.create_session(
            app_name=APP_NAME,
            user_id="sorter",
            session_id=session_id,
            state=initial_state,
        )

        payload = {"country_raw": country_raw}
        new_message = types.Content(
            role="user",
            parts=[types.Part(text=json.dumps(payload, ensure_ascii=False))],
        )
        # Loop runs classifier+verifier per iteration (the gate is zero-model),
        # so two iterations cost up to 4 LLM calls; 5 leaves headroom.
        async for _event in runner.run_async(
            user_id="sorter",
            session_id=session_id,
            new_message=new_message,
            run_config=RunConfig(max_llm_calls=5),
        ):
            pass

        session = await session_service.get_session(
            app_name=APP_NAME, user_id="sorter", session_id=session_id
        )
        if session is None:
            raise RuntimeError("ADK session disappeared before result collection.")

        classifier_result = _as_dict(session.state.get("classifier_result", {}))
        verifier_verdict = _as_dict(session.state.get("verifier_verdict", {}))
        approved = bool(verifier_verdict.get("approved"))
        corrected = verifier_verdict.get("corrected_bucket")

        if approved:
            # Verifier accepted: trust the classifier bucket.
            bucket = classifier_result.get("country_bucket", "Other")
        elif corrected:
            # Verifier rejected and supplied the correct bucket. This covers
            # both a mid-loop rejection and the loop exhausting after two
            # rejected attempts — either way the verifier's correction wins.
            bucket = corrected
        else:
            # Rejected without a correction (or loop ran dry): fall back to
            # the last classifier result rather than inventing a bucket.
            bucket = classifier_result.get("country_bucket", "Other")

        return {
            "country_bucket": bucket,
            "confidence": classifier_result.get("confidence"),
            "notes": classifier_result.get("notes"),
            "approved": approved,
            "corrected_bucket": corrected,
            "attempts": session.state.get("attempt"),
        }

    def classify(self, country_raw: str) -> str:
        """Return the canonical bucket string for one raw HQ-country value."""
        result = asyncio.run(self._invoke_async(country_raw))
        bucket = result.get("country_bucket", "Other")
        if bucket not in (
            "Uzbekistan", "Turkiye", "Georgia", "Kyrgyzstan", "Azerbaijan",
            "USA", "Kazakhstan", "Mong. Turkmenistan Tajikistan", "Other",
        ):
            bucket = "Other"
        return bucket
