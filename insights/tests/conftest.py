import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def stub_the_api_key(request, settings):
    """
    Give the mocked suite a key that is not a real one.

    Without this the suite quietly depends on whoever is running it having a
    .env, so it passes on a developer machine and fails in CI: green where it
    does not matter and red where it does. Twenty-four tests were in that state
    before this fixture existed.

    Tests marked `live` are left alone, because they call the real API and need
    the real key.
    """
    if "live" in request.keywords:
        return
    settings.ANTHROPIC_API_KEY = "test-key-not-a-real-one"


@pytest.fixture(autouse=True)
def clear_cache():
    """
    Give every test an empty cache.

    The local-memory backend lives for the life of the process, so without this
    a test that caches a model reply changes the behaviour of every later test
    that happens to use the same facts. Tests would then pass or fail depending
    on what ran before them, which is the kind of failure that takes an
    afternoon to understand.
    """
    cache.clear()
    yield
    cache.clear()
