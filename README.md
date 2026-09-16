# Meter Insight Service

[![CI](https://github.com/klumarr/meter-insight-service/actions/workflows/ci.yml/badge.svg)](https://github.com/klumarr/meter-insight-service/actions/workflows/ci.yml)

A small Django service that turns a list of meter readings into plain-English
energy-saving recommendations.

**The code does all the arithmetic. The language model only writes the narrative.**
The model never calculates a number and never sees a raw reading. It receives a
handful of already-computed facts and turns them into prose.

## What this is

A portfolio project, built to work through one question properly: what does it
take to put a language model in front of a customer when the model is fluent,
confident and occasionally wrong?

The short answer is that you treat it as an unreliable third party rather than a
library call. Everything below follows from that.

Three things in here are worth more than the feature list:

- **The evals caught the model doing arithmetic it had been forbidden from
  doing**, in 2 of 20 sampled calls. The fix was not a sterner prompt.
  [What they found](#what-they-found).
- **A running service found a bug that no unit test could have.** Two
  recommendations cited the same fact, so one saving was reported twice. Every
  individual figure was correct; the set of them was not.
  [How it behaves](#how-it-behaves-when-the-model-misbehaves).
- **Several figures are deliberately withheld** even though they are
  arithmetically correct, because a true number can still support a false claim.
  [What the service refuses to say](#what-the-service-refuses-to-say).
- **Measuring the token spend contradicted the design assumption.** The facts,
  which the whole aggregation exists to keep small, are 16% of the input. The
  schema is 70%. [Where the tokens go](#where-the-tokens-actually-go).

If you have five minutes, read [`insights/analytics.py`](insights/analytics.py)
for the arithmetic and [`insights/llm.py`](insights/llm.py) for the model
boundary. The comments there explain the reasoning, not the mechanics.

## The problem this is built around

A language model is fluent in exactly the same tone whether it is right or
wrong. Ask one to work out a saving and it will produce a confident, specific,
plausible number that nobody can reproduce — including the model, if you ask it
twice. That is fine for prose and unacceptable for something a customer might
act on or query a bill against.

So the model is given a bounded input, an explicit timeout, no opportunity to
produce a number, and a reply that is validated before any of it is believed.

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
| Reply cites the same fact twice | Retried once, with the objection sent back |
| Retry fails too | Falls back, and logs a warning |
| No API key configured | Falls back, rather than pretending to be healthy |

The retry replays the model's rejected output as the assistant turn and sends
the reason back as a `tool_result` marked `is_error`. Being shown what you said
alongside the specific objection to it repairs far more reliably than being
asked again from scratch.

That fourth row was added after running the service and reading the output. The
model returned two recommendations about the same evening peak, both citing
`peak_window`, so both carried the saving derived from it — a caller adding them
up was told a 378 kWh window could yield 113 kWh of savings when it can only
yield 57. Every recommendation was individually well-formed and correctly cited.
The schema validates one at a time and the citation check only asked whether
each fact existed, so neither could see a fault that exists only in the
relationship between two valid items.

The fallback in `fallback.py` writes recommendations from the computed facts
alone. It imports `analytics` and `schemas` but never `llm`: delete the entire
model integration and every request is still answered. Its output is built as a
`ModelInsightResponse` and pushed through the same `build_response`, so it goes
through the same schema and the same saving calculation as the model's wording
does. A fallback response cannot be shaped differently from a generated one, and
the numbers do not depend on which writer produced the sentence around them.

It also never had the duplicate problem, because it builds one recommendation
per fact by construction. The model had to be policed into a property the
deterministic path got for free.

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
decision, enforced everywhere, with no special-casing downstream — and a test
asserts that the prompt offers exactly the citable facts, so the two cannot
drift apart. If they did, a model doing as it was told would be refused for it.

## Evals

The mocked suite proves the code handles a bad reply correctly. It can never
tell you how often a bad reply actually happens. The evals answer that: they
call the live model and assert properties of what comes back.

```bash
RUN_LLM_EVALS=1 pytest insights/tests/test_evals.py -v
```

They check that no fact is cited twice, that every citation is a fact that
exists, that a withheld peak is never discussed, that heavily estimated data is
flagged to the customer, and above all that no number appears which cannot be
traced to a supplied figure.

An eval failing does not necessarily mean this repository is broken. A model
update, or simply a different sample, can break one that passed yesterday. That
is the point of having them: the failure is information about the model, and the
fix is usually a prompt change rather than a code change. It is also why they
are opt-in rather than a blocking CI gate.

### What they found

Running them turned up the model performing arithmetic it had been told not to,
in 2 of 20 sampled calls:

| What it wrote | The sum it did |
| --- | --- |
| "which works out to 48 kWh per day" | 240 ÷ 5 |
| "your readings are 100% accurate" | 100 − 0.0 |

Both figures were correct here, which is exactly what makes the behaviour
dangerous: the same reflex on numbers that do not divide cleanly produces a
confident, plausible, wrong figure. Nothing downstream would catch it either,
because the schema validates shape and the citation check validates provenance.
Neither reads the prose.

The fix was not to forbid it more loudly. A daily average means more to a
household than a fortnightly total, so the urge was a good one — the problem was
that the model had to calculate it. Both figures are now computed in
`analytics.py`, where they are exact and tested, and supplied in the prompt. The
rate went from 2/20 to 0/20.

**A model that needs a figure it was not given will make one.** Forbidding is
half the answer; the other half is noticing what it keeps reaching for and
supplying it properly.

Twenty calls is a small sample. It establishes that the behaviour exists and
roughly how often, not that it is now impossible — the value is that the check
can be re-run after a model upgrade or a prompt edit.

## Caching

The cheapest call is the one that never happens. Identical readings reuse the
wording written for them last time, measured on a 672-reading request:

| | Latency | Cost |
| --- | --- | --- |
| Cache miss | ~2.8s | ~$0.0029 |
| Cache hit | ~3ms | nothing |

Two decisions in there are worth more than the speed.

**Only the model's wording is cached, never the finished response.** Savings are
recalculated from the facts on every request, so correcting an assumption in
`analytics.py` takes effect immediately rather than being shadowed by an hour of
cached arithmetic. The expensive, slow, non-deterministic half is cached; the
cheap, instant, deterministic half is not.

**A fallback is never cached.** It is generated in microseconds, so caching it
saves nothing, and a thirty-second outage would otherwise pin templated text in
front of every identical request until the entry expired. A transient failure
should stay transient.

The key is a hash of every input that could change the answer: the prompt, the
model name, the system prompt and the tool schema. Leaving any of them out is
how a service keeps serving wording written under yesterday's instructions for
an hour after they changed. And because a cache is an optimisation, a backend
that fails degrades to calling the model rather than to a 500.

The backend is local memory, which is honest for a single container and a lie at
four workers, where you would have four caches and four times the misses. Moving
to Redis is a settings change only, because nothing in the code talks to
anything but Django's cache API.

## What a call costs

Every call logs its token usage, because spend is invisible until something
records it and "the bill went up" is a poor first indication. Prices are
deliberately not hardcoded anywhere in the service: they change, they differ per
model, and a rate baked into application code is a rate nobody remembers to
update. Tokens are the fact; turning them into money is a billing concern.

Measured across the four eval scenarios, at the rates listed for Claude Haiku
4.5 when this was written ($1 and $5 per million input and output tokens):

| | Tokens |
| --- | --- |
| Input | 1,371 – 1,494 (mean 1,436) |
| Output | 208 – 339 (mean 291) |
| Cost per call | ~$0.0029, or about 350 calls per dollar |

A repaired reply costs two calls rather than one, which is the other reason the
retry is capped at exactly one.

### Where the tokens actually go

This was the surprise. The whole aggregation design exists so that a year of
half-hourly readings never reaches the model — thirty thousand rows reduced to a
few hundred tokens of facts. That worked. It also turned out not to be where the
budget was going:

| | Tokens | Share |
| --- | --- | --- |
| The facts (user message) | 220 | 16% |
| System prompt | 182 | 14% |
| **Tool schema** | **940** | **70%** |

Most of the input is the JSON schema attached to every call — the thing that
makes the model's reply structured and validatable. That is a fair trade and I
would make it again, but it is worth knowing that the expensive part was the
contract, not the data.

Pydantic emits a `"title": "Title"` on every field, duplicating the field name
and telling the model nothing. Stripping those saves 90 tokens, 7% of input.
It is left in: a schema post-processor is a thing to maintain and explain, and
one definition that both instructs and validates the model is worth more than
7%. At volume the arithmetic changes, and that is the first place I would look.

## The endpoint

```
POST /api/insights
```

```jsonc
{
  "readings": [
    {"timestamp": "2026-01-01T00:30:00+00:00", "value": "0.50", "quality": "ACTUAL"}
  ],
  "timezone": "Europe/London"   // optional, defaults to UTC
}
```

`timezone` decides which local hours the peak window is expressed in. A customer
told their peak is "17:00 to 20:00" means their own clock, so bucketing in UTC
would be wrong for anywhere that is not on it.

A `200` carries the recommendations, the facts they were derived from, and a
`source` of `model` or `fallback`:

```jsonc
{
  "source": "model",
  "facts": {
    "total_consumption_kwh": "672.00",
    "average_daily_kwh": "48.00",
    "peak_window": {"start_hour": 17, "end_hour": 20, "share_percent": "56.3"},
    "week_on_week": null            // withheld: too little history to compare
  },
  "recommendations": [
    {
      "title": "Shift energy use away from peak evening hours",
      "rationale": "Over 56% of your electricity is used between 5pm and 8pm...",
      "based_on": "peak_window",    // the model chose the fact
      "confidence": "high",
      "estimated_saving_kwh": "56.70"   // analytics.py calculated the number
    }
  ]
}
```

The facts are returned alongside the advice on purpose. Every recommendation
names the fact it rests on, and that fact is in the same payload, so a reader can
check the advice against the arithmetic without a second request. Advice that
cannot be checked has negative value.

A `400` means the request itself is wrong, and says which field:

```jsonc
{
  "error": "the request body did not match the expected schema",
  "detail": [
    {"type": "timezone_aware", "loc": ["readings", 0, "timestamp"],
     "msg": "Input should have timezone info"}
  ]
}
```

The error reports which field was wrong rather than echoing the offending value
back, because for a mistake at the root of the document that value is the entire
request body.

Client errors and third-party failures are kept apart deliberately. A malformed
request is the caller's to fix and says so; a model that is slow or wrong is not
their problem and never reaches them as an error.

## Layout

| Module | Responsibility |
| --- | --- |
| `insights/analytics.py` | All arithmetic. Standard library only — no Django, no Pydantic, no model |
| `insights/schemas.py` | The request, model and response contracts, and the citation check |
| `insights/llm.py` | The only module that talks to the model |
| `insights/fallback.py` | Recommendations written without one |
| `insights/views.py` | The HTTP boundary, and nothing else |

All arithmetic uses `Decimal`. Binary floating point cannot represent decimal
fractions exactly, and across thousands of readings those errors accumulate into
a total that does not match the customer's bill. Figures are serialised as JSON
strings for the same reason — emitting `67.20` as a bare number invites the
client's parser to turn it back into a float at the very last step.

## Running it

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then add an Anthropic API key
python manage.py runserver
```

`.env` is gitignored and has been since the first commit. The key used to build
this lives in a workspace with a hard spend cap.

```bash
pytest
```

The suite mocks the model, so it needs no API key, costs nothing and runs in
under a second. That is not only convenience: a suite that needed a network
could not assert anything about timeouts, malformed replies or repair attempts,
which is most of what is worth testing here.

It is also hermetic. CI runs it with no API key at all and no secret configured,
so a pull request from a stranger runs the whole thing with nothing to leak and
no spend to authorise.

Two opt-in tests do call the real API — the eval suite above, and a single
smoke test that proves the whole chain works end to end:

```bash
RUN_LIVE_LLM_TEST=1 pytest insights/tests/test_live_smoke.py -s
```
