# Meter Insight Service

A small Django service that turns a list of meter readings into three plain-English
energy-saving recommendations.

**The code does all the arithmetic. The language model only writes the narrative.**
The model never calculates a number and never sees a raw reading — it receives a
handful of already-computed facts and turns them into prose.

> Work in progress. This README is expanded with the architecture, the design
> decisions behind them, and measured latency and cost figures once the service
> is complete.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Running the tests

```bash
pytest
```

The test suite mocks the language model, so it needs no API key and costs nothing
to run.
