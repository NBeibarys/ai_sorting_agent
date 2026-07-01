"""ADK country-classifier agent with an independent verifier and correction loop.

Pipeline: classifier -> verifier -> approval gate, wrapped in a LoopAgent
(max 2 iterations). The gate is a zero-model routing step that escalates
(exits the loop) on approval or after two rejected attempts. The whole loop
is wrapped in a SequentialAgent so it composes as the root agent.
"""
import json
import os
from collections.abc import AsyncGenerator

from google.adk.agents import (
    Agent,
    BaseAgent,
    InvocationContext,
    LoopAgent,
    SequentialAgent,
)
from google.adk.events import Event, EventActions
from google.adk.models.google_llm import Gemini
from google.genai import types

from .prompts import HEAD_INSTRUCTION, SORTER_INSTRUCTION
from .schemas import CountryClassification, VerificationVerdict


def _as_dict(value) -> dict:
    """Normalize ADK structured output (Pydantic / dict / JSON str) to dict."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise ValueError(f"Unsupported structured agent output: {type(value).__name__}")


def gate_decision(verdict: dict, attempt: int) -> tuple[bool, bool]:
    """Return (approved, exhausted) for deterministic loop routing."""
    approved = bool(verdict.get("approved"))
    return approved, (not approved and attempt >= 2)


class ApprovalGate(BaseAgent):
    """Zero-model routing step that exits the loop on approval/exhaustion.

    Mirrors the sibling fellowship repo: reads the verifier verdict and the
    attempt counter from session state, then either escalates (approved or
    attempts exhausted) or hands feedback back to the classifier for another
    iteration.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        attempt = int(state.get("attempt", 1))
        verdict = _as_dict(state.get("verifier_verdict", {}))
        approved, exhausted = gate_decision(verdict, attempt)

        delta = {
            "verifier_feedback": verdict.get("feedback") or "",
            "attempt": attempt if approved else attempt + 1,
        }
        if approved:
            delta["review_approved"] = True

        message = {
            "status": "verified" if approved else "revision_requested",
            "feedback": delta["verifier_feedback"],
            "attempt": delta["attempt"],
            "exhausted": exhausted,
        }
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=json.dumps(message))],
            ),
            actions=EventActions(
                state_delta=delta,
                escalate=approved or exhausted,
            ),
        )


def build_root_agent(model: str) -> SequentialAgent:
    """Classifier -> verifier -> approval-gate correction loop."""
    classifier = Agent(
        name="classifier",
        description=(
            "Classifies a startup's headquarters country string into a "
            "canonical bucket for Excel tab sorting."
        ),
        model=Gemini(
            model=model,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
        instruction=SORTER_INSTRUCTION,
        output_schema=CountryClassification,
        output_key="classifier_result",
        generate_content_config=types.GenerateContentConfig(temperature=0),
        timeout=120,
    )
    verifier = Agent(
        name="verifier",
        description=(
            "Independently verifies the classifier bucket, requests "
            "corrections, and supplies a corrected bucket when wrong."
        ),
        model=Gemini(
            model=model,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
        instruction=HEAD_INSTRUCTION,
        output_schema=VerificationVerdict,
        output_key="verifier_verdict",
        generate_content_config=types.GenerateContentConfig(temperature=0),
        timeout=120,
    )
    gate = ApprovalGate(name="approval_gate")
    classification_loop = LoopAgent(
        name="classification_loop",
        description="Classifier and independent verifier correction loop.",
        sub_agents=[classifier, verifier, gate],
        max_iterations=2,
    )
    return SequentialAgent(
        name="country_sorting",
        description="Country classification with verification and correction.",
        sub_agents=[classification_loop],
    )


# Module-level root agent enables adk run / adk web for interactive use,
# mirroring the sibling fellowship repo. The batch workflow builds its own
# fresh agent per invocation for thread isolation.
root_agent = build_root_agent(
    os.environ.get("SORTER_MODEL", "gemini-3.5-flash"),
)
