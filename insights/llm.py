"""
The only module that talks to the language model.

Everything the model is allowed to influence passes through here, which means
the rest of the service can be read, tested and trusted without knowing a model
exists. Delete this file and `insights.fallback` still answers every request.

The model is treated throughout as an unreliable third party: it is given a
bounded input, an explicit timeout, no opportunity to produce a number, and its
reply is validated before any of it is believed.

Two things happen when that validation fails. A reply the model could plausibly
fix gets exactly one retry, with the objection sent back so it can see what was
wrong. Anything past that hands over to the fallback, so the public entry point
returns an answer no matter how the call goes.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic
from django.conf import settings

from insights import analytics, fallback, schemas

logger = logging.getLogger(__name__)

TOOL_NAME = "submit_recommendations"

SYSTEM_PROMPT = """\
You write short energy-saving recommendations for a household, using figures \
that have already been calculated for you.

Rules:
- Use only the figures given to you. Never calculate, estimate or invent a number.
- Do not state a saving. Savings are calculated elsewhere.
- Attribute every recommendation to exactly one supplied fact, via `based_on`.
- Each recommendation must cite a different fact. Never cite the same fact twice.
- Only the facts listed below exist. Never refer to a fact that is not listed.
- Write plainly, in British English, for someone who is not technical.
- Order the recommendations with the most valuable first.\
"""


class LLMError(Exception):
    """Base class for every way this module can fail."""


class LLMUnavailableError(LLMError):
    """
    The model could not be reached, or refused to answer.

    Timeouts, connection failures and API errors land here. Sending the same
    request again is unlikely to help, so the caller should stop asking.
    """


class InvalidModelOutputError(LLMError):
    """
    The model answered, but the answer cannot be trusted.

    The reply broke the schema, or attributed a recommendation to a fact that
    was never supplied. Unlike LLMUnavailableError this is worth one more
    attempt, because the model can be told what it got wrong.
    """


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The arguments the model supplied, and the id needed to answer the call."""

    id: str
    payload: dict[str, Any]


def generate_insights(facts: analytics.ConsumptionFacts) -> schemas.InsightResponse:
    """
    Turn computed facts into written recommendations, whatever happens.

    This function does not raise. The model is a third party that will be slow,
    unreachable or wrong on some proportion of calls, and none of those are the
    caller's problem to solve. When the model cannot be used, the deterministic
    fallback answers from the same facts and the response says so.
    """
    try:
        return generate_from_model(facts)
    except LLMError as failure:
        # Logged at warning rather than swallowed: a fallback that fires on
        # every request looks identical to a healthy service from the outside,
        # and that is exactly the outage you find out about from a customer.
        logger.warning("Falling back to templated insights: %s", failure)
        return fallback.build_fallback(facts)


def generate_from_model(facts: analytics.ConsumptionFacts) -> schemas.InsightResponse:
    """
    Ask the model, and give it exactly one chance to correct itself.

    A rejected reply is worth retrying because the model can be shown what was
    wrong with it. An unreachable model is not, so LLMUnavailableError is left
    to propagate immediately rather than spending a second timeout on it.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": build_prompt(facts)}]

    call: ToolCall | None = None
    try:
        call = request_recommendations(messages)
        model_response = parse_and_validate(call.payload, facts)
    except InvalidModelOutputError as rejection:
        logger.info("Retrying after rejected model output: %s", rejection)
        retry_messages = _repair_messages(messages, failed_call=call, reason=str(rejection))
        call = request_recommendations(retry_messages)
        model_response = parse_and_validate(call.payload, facts)

    return schemas.build_response(model_response, facts=facts)


def build_prompt(facts: analytics.ConsumptionFacts) -> str:
    """
    Render the facts as the entire user message.

    The readings themselves are never sent. A year of half-hourly readings is
    over thirty thousand rows; the facts derived from them are a few hundred
    tokens. Aggregating first is what makes the call fast, cheap and small
    enough that the model has nothing to get lost in.
    """
    summary = summarise_facts(facts)

    # Each recommendation must cite a different fact, so a household with only
    # two facts to its name cannot support three of them. Asking for three
    # anyway guarantees a duplicate, which costs the retry and probably the
    # fallback to discover something we already knew before calling.
    limit = min(schemas.MAX_RECOMMENDATIONS, len(summary))

    return (
        "Here are the only facts available about this household's electricity use.\n\n"
        f"{json.dumps(summary, indent=2)}\n\n"
        f"Write up to {limit} recommendations, each citing a different fact, and "
        f"submit them by calling the {TOOL_NAME} tool."
    )


def summarise_facts(facts: analytics.ConsumptionFacts) -> dict[str, Any]:
    """
    The facts, keyed by the names the model must cite.

    A fact that was not derived is left out entirely rather than sent as null.
    The model cannot cite what it was never shown, so the omission is the
    guardrail: if `week_on_week` comes back in `based_on`, we know for certain
    the reasoning was invented.

    The keys here are exactly analytics.available_fact_keys, which is asserted
    in the tests. The prompt therefore cannot offer a fact the citation check
    will later refuse, and cannot withhold one it would have accepted.

    Decimals become strings so that the exact values survive JSON encoding.
    """
    summary: dict[str, Any] = {
        analytics.FactKey.TOTAL_CONSUMPTION: {
            "total_kwh": str(facts.total_consumption_kwh),
            "days_covered": facts.days_covered,
        },
        analytics.FactKey.ESTIMATED_SHARE: {
            "percent_not_from_a_real_meter_read": str(facts.estimated_share_percent),
        },
    }

    if facts.peak_window is not None:
        summary[analytics.FactKey.PEAK_WINDOW] = {
            "from_hour": facts.peak_window.start_hour,
            "to_hour": facts.peak_window.end_hour,
            "timezone": facts.timezone_name,
            "kwh_in_window": str(facts.peak_window.consumption_kwh),
            "percent_of_total": str(facts.peak_window.share_percent),
        }

    if facts.week_on_week is not None:
        summary[analytics.FactKey.WEEK_ON_WEEK] = {
            "latest_week_kwh": str(facts.week_on_week.latest_week_kwh),
            "previous_week_kwh": str(facts.week_on_week.previous_week_kwh),
            "change_percent": str(facts.week_on_week.change_percent),
        }

    return summary


def request_recommendations(messages: list[dict[str, Any]]) -> ToolCall:
    """
    Make one call and return the raw arguments the model supplied.

    The reply is collected through a tool call rather than by parsing prose. The
    schema is attached to the tool and the model is *required* to call it, so
    "reply in JSON" stops being a polite request the model may ignore and
    becomes the only move available to it.
    """
    if not settings.ANTHROPIC_API_KEY:
        raise LLMUnavailableError("ANTHROPIC_API_KEY is not set")

    try:
        message = _client().messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=settings.ANTHROPIC_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=[
                {
                    "name": TOOL_NAME,
                    "description": "Submit the recommendations. This is the only way to reply.",
                    "input_schema": schemas.ModelInsightResponse.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            timeout=settings.ANTHROPIC_TIMEOUT_SECONDS,
        )
    except anthropic.APITimeoutError as error:
        raise LLMUnavailableError(
            f"the model did not respond within {settings.ANTHROPIC_TIMEOUT_SECONDS}s"
        ) from error
    except anthropic.APIError as error:
        raise LLMUnavailableError(f"the model could not be reached: {error}") from error

    for block in message.content:
        if block.type == "tool_use" and block.name == TOOL_NAME:
            return ToolCall(id=block.id, payload=dict(block.input))

    # Forcing tool_choice makes this very unlikely, but "very unlikely" is not
    # "impossible" when the other end is a language model.
    raise InvalidModelOutputError(f"the model replied without calling {TOOL_NAME}")


def _repair_messages(
    messages: list[dict[str, Any]], *, failed_call: ToolCall | None, reason: str
) -> list[dict[str, Any]]:
    """
    Extend the conversation with the rejected reply and the objection to it.

    The model is shown its own output next to the specific reason it was
    refused, which repairs far more reliably than simply being asked again. The
    objection travels as a tool_result marked is_error, because a rejected tool
    call is exactly what this is, and the model has been trained to act on one.

    The reason strings come from schemas.check_citations and from Pydantic, both
    of which name the offending field. They were always destined to be read by
    the model rather than only by us.
    """
    if failed_call is None:
        # It never called the tool, so there is no tool call to answer.
        return [
            *messages,
            {
                "role": "user",
                "content": f"{reason}. You must reply by calling the {TOOL_NAME} tool.",
            },
        ]

    return [
        *messages,
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": failed_call.id,
                    "name": TOOL_NAME,
                    "input": failed_call.payload,
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": failed_call.id,
                    "is_error": True,
                    "content": f"Rejected: {reason} Correct it and call {TOOL_NAME} again.",
                }
            ],
        },
    ]


def parse_and_validate(
    payload: dict[str, Any], facts: analytics.ConsumptionFacts
) -> schemas.ModelInsightResponse:
    """
    Check the reply against the contract, in both senses.

    The schema catches the wrong shape. check_citations catches the right shape
    supporting an invented claim, which no schema can detect because it depends
    on which facts this particular reading set produced.
    """
    try:
        response = schemas.ModelInsightResponse.model_validate(payload)
    except ValueError as error:
        raise InvalidModelOutputError(str(error)) from error

    try:
        schemas.check_citations(response, facts)
    except schemas.CitationError as error:
        raise InvalidModelOutputError(str(error)) from error

    return response


def _client() -> anthropic.Anthropic:
    # max_retries=0 because the SDK retries twice by default. Left on, a single
    # call could quietly take three times as long and cost three times as much,
    # and the retry policy this service actually wants belongs in one visible
    # place rather than split between here and a vendor default.
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=0)
