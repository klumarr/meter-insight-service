"""
The only module that talks to the language model.

Everything the model is allowed to influence passes through here, which means
the rest of the service can be read, tested and trusted without knowing a model
exists. Delete this file and `insights.fallback` still answers every request.

The model is treated throughout as an unreliable third party: it is given a
bounded input, an explicit timeout, no opportunity to produce a number, and its
reply is validated before any of it is believed.

This module makes exactly one attempt. Retrying and falling back are deliberately
not here yet.
"""

import json
from typing import Any

import anthropic
from django.conf import settings

from insights import analytics, schemas

TOOL_NAME = "submit_recommendations"

SYSTEM_PROMPT = """\
You write short energy-saving recommendations for a household, using figures \
that have already been calculated for you.

Rules:
- Use only the figures given to you. Never calculate, estimate or invent a number.
- Do not state a saving. Savings are calculated elsewhere.
- Attribute every recommendation to exactly one supplied fact, via `based_on`.
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


def generate_insights(facts: analytics.ConsumptionFacts) -> schemas.InsightResponse:
    """
    Turn computed facts into written recommendations.

    Raises LLMUnavailableError or InvalidModelOutputError rather than returning
    anything doubtful. Deciding what to do about that is the caller's job.
    """
    payload = request_recommendations(build_prompt(facts))
    model_response = parse_and_validate(payload, facts)
    return schemas.build_response(model_response, facts=facts)


def build_prompt(facts: analytics.ConsumptionFacts) -> str:
    """
    Render the facts as the entire user message.

    The readings themselves are never sent. A year of half-hourly readings is
    over thirty thousand rows; the facts derived from them are a few hundred
    tokens. Aggregating first is what makes the call fast, cheap and small
    enough that the model has nothing to get lost in.
    """
    return (
        "Here are the only facts available about this household's electricity use.\n\n"
        f"{json.dumps(summarise_facts(facts), indent=2)}\n\n"
        f"Write up to {schemas.MAX_RECOMMENDATIONS} recommendations and submit them "
        f"by calling the {TOOL_NAME} tool."
    )


def summarise_facts(facts: analytics.ConsumptionFacts) -> dict[str, Any]:
    """
    The facts, keyed by the names the model must cite.

    A fact that was not derived is left out entirely rather than sent as null.
    The model cannot cite what it was never shown, so the omission is the
    guardrail: if `week_on_week` comes back in `based_on`, we know for certain
    the reasoning was invented.

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
        analytics.FactKey.PEAK_WINDOW: {
            "from_hour": facts.peak_window.start_hour,
            "to_hour": facts.peak_window.end_hour,
            "timezone": facts.timezone_name,
            "kwh_in_window": str(facts.peak_window.consumption_kwh),
            "percent_of_total": str(facts.peak_window.share_percent),
        },
    }

    if facts.week_on_week is not None:
        summary[analytics.FactKey.WEEK_ON_WEEK] = {
            "latest_week_kwh": str(facts.week_on_week.latest_week_kwh),
            "previous_week_kwh": str(facts.week_on_week.previous_week_kwh),
            "change_percent": str(facts.week_on_week.change_percent),
        }

    return summary


def request_recommendations(prompt: str) -> dict[str, Any]:
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
            messages=[{"role": "user", "content": prompt}],
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
            return dict(block.input)

    # Forcing tool_choice makes this very unlikely, but "very unlikely" is not
    # "impossible" when the other end is a language model.
    raise InvalidModelOutputError(f"the model replied without calling {TOOL_NAME}")


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
    except schemas.UncitedFactError as error:
        raise InvalidModelOutputError(str(error)) from error

    return response


def _client() -> anthropic.Anthropic:
    # max_retries=0 because the SDK retries twice by default. Left on, a single
    # call could quietly take three times as long and cost three times as much,
    # and the retry policy this service actually wants belongs in one visible
    # place rather than split between here and a vendor default.
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=0)
