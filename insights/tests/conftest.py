import pytest
from django.core.cache import cache


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
