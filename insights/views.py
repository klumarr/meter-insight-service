"""
The HTTP boundary.

This module does as little as possible: parse the body, hand the readings to
analytics, hand the facts to llm, serialise what comes back. There is no
business logic here to test, because all of it lives in modules that can be
tested without an HTTP request.

The status codes draw one distinction, and it is the important one:

- 400 means the caller sent something we cannot work with, and the response
  says exactly what, so they can fix it.
- 200 means we have an answer, and the `source` field says whether the model
  or the fallback wrote it.

There is deliberately no path where a language model failure produces a 5xx.
The model is a third party; its bad afternoon is not the caller's problem, and
`llm.generate_insights` is written never to raise.
"""

import json
import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_POST
from pydantic import ValidationError

from insights import analytics, llm, schemas

logger = logging.getLogger(__name__)


@require_POST
def insights(request: HttpRequest) -> HttpResponse:
    """
    POST /api/insights

    Takes a list of meter readings and returns up to three recommendations,
    each citing the computed fact it rests on, with the facts themselves
    included so the advice can be checked against the arithmetic.
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError as error:
        return _bad_request(f"the request body is not valid JSON: {error}")

    try:
        insight_request = schemas.InsightRequest.model_validate(body)
    except ValidationError as error:
        return _bad_request(
            "the request body did not match the expected schema",
            # include_input is off because `input` echoes the offending value
            # back, and for an error at the root of the document that value is
            # the entire body -- up to a year of half-hourly readings. `loc`
            # already identifies exactly which field or index was wrong, which
            # is what the caller needs in order to fix it.
            detail=error.errors(include_url=False, include_context=False, include_input=False),
        )

    # Nothing is caught around compute_facts on purpose. Every input it rejects
    # -- empty reading sets, naive timestamps -- is already refused by the
    # schema above, so an exception here would be a bug in our own code rather
    # than a bad request, and a bug should be loud.
    facts = analytics.compute_facts(insight_request.to_readings(), timezone=insight_request.zone())

    response = llm.generate_insights(facts)
    return JsonResponse(response.model_dump(mode="json"))


def _bad_request(message: str, *, detail: list | None = None) -> JsonResponse:
    payload: dict[str, object] = {"error": message}
    if detail is not None:
        payload["detail"] = detail
    return JsonResponse(payload, status=400)
