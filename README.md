# Meter Insight Service

A small Django service that turns a list of meter readings into plain-English
energy-saving recommendations.

**The code does all the arithmetic. The language model only writes the narrative.**
The model never calculates a number and never sees a raw reading. It receives a
handful of already-computed facts and turns them into prose.

> Work in progress. The HTTP endpoint, response caching and the eval suite are
> still to come, along with measured latency and cost figures.

## The problem this is built around

A language model is fluent in exactly the same tone whether it is right or
wrong. Ask one to work out a saving and it will produce a confident, specific,
plausible number that nobody can reproduce — including the model, if you ask it
twice. That is fine for prose and unacceptable for something a customer might
act on or query a bill against.

So the model is treated as an unreliable third party rather than a component:
given a bounded input, an explicit timeout, no opportunity to produce a number,
and a reply that is validated before any of it is believed.

Every recommendation must cite the one fact it rests on. The saving attached to
it is then calculated by `analytics.py` from that fact, using assumptions that
are named, tested and arguable. The model chooses which fact to talk about and
how to phrase it; it never chooses a figure.

The citation is also what makes fabrication detectable. A fact that could not be
derived is left out of the prompt entirely rather than sent as null, so a reply
citing it is proof the reasoning was invented rather than a matter of judgement.
That reply is refused.

## How it behaves when the model misbehaves

The endpoint does not return a 500 because a third party had a bad afternoon.

| What goes wrong | What the service does |
| --- | --- |
| Model is slow or unreachable | Falls back immediately — retrying only buys a second timeout |
| Reply breaks the schema | Retried once, with the objection sent back |
| Reply cites a fact that was never supplied | Retried once, with the objection sent back |
| Retry fails too | Falls back, and logs a warning |
| No API key configured | Falls back, rather than pretending to be healthy |

The retry replays the model's rejected output as the assistant turn and sends
the reason back as a `tool_result` marked `is_error`. Being shown what you said
alongside the specific objection to it repairs far more reliably than being
asked again from scratch.

The fallback in `fallback.py` writes recommendations from the computed facts
alone. It imports `analytics` and `schemas` but never `llm`: delete the entire
model integration and every request is still answered. Its output is built as a
`ModelInsightResponse` and pushed through the same `build_response`, so it goes
through the same schema and the same saving calculation as the model's wording
does. A fallback response cannot be shaped differently from a generated one, and
the numbers do not depend on which writer produced the sentence around them.

Responses say which writer produced them, in a `source` field. Quietly serving
templated text as though it were generated is how a silent outage lasts weeks.

## What the service refuses to say

Withholding a fact is a feature here, not a gap. In each case the figure could
be produced, would be arithmetically correct, and would support a claim that
rests on nothing:

- **Week-on-week change**, when there is too little history. A confident figure
  derived from four days is worse than no figure: a caller can omit what is
  absent, but cannot detect that a number it was given is meaningless.
- **Peak window**, when usage is evenly spread. Three hours out of twenty-four
  hold 12.5% of a day no matter how the usage falls, so "the busiest window" is
  not on its own a finding. A window has to carry at least 1.3× an even spread
  before the service will advise shifting load away from it.
- **A kWh saving for estimated readings.** Replacing estimates with real reads
  corrects what a customer is billed for, not what they consume. A saving here
  would be a fiction, so none is offered.

A withheld fact disappears from `available_fact_keys`, and therefore from the
prompt, and therefore from anything the model or the fallback can cite. One
decision, enforced everywhere, with no special-casing downstream.

## Layout

| Module | Responsibility |
| --- | --- |
| `insights/analytics.py` | All arithmetic. Standard library only — no Django, no Pydantic, no model |
| `insights/schemas.py` | The request, model and response contracts, and the citation check |
| `insights/llm.py` | The only module that talks to the model |
| `insights/fallback.py` | Recommendations written without one |

All arithmetic uses `Decimal`. Binary floating point cannot represent decimal
fractions exactly, and across thousands of readings those errors accumulate into
a total that does not match the customer's bill. Figures are serialised as JSON
strings for the same reason — emitting `67.20` as a bare number invites the
client's parser to turn it back into a float at the very last step.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` holds the Anthropic API key and is gitignored. The key used to develop
this lives in a workspace with a hard spend cap.

## Running the tests

```bash
pytest
```

The suite mocks the model, so it needs no API key, costs nothing and runs in
under a second. That is not only convenience: a suite that needed a network
could not assert anything about timeouts, malformed replies or repair attempts,
which is most of what is worth testing here.

One live test exercises the real API and is skipped unless asked for:

```bash
RUN_LIVE_LLM_TEST=1 pytest insights/tests/test_live_smoke.py -s
```
