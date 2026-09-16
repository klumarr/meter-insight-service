"""
Tests for the demo page.

There is no logic here to verify, so these do not try to invent any. They pin
the two things that would silently rot: the page's claim to be a client rather
than part of the service, and the handful of strings it shares with the service
it calls. A demo that drifts out of step with the thing it demonstrates is worse
than no demo, because it is still convincing.
"""

import ast
from pathlib import Path

import pytest
from django.urls import reverse

import demo
from insights import analytics

DEMO_ROOT = Path(demo.__file__).parent

# The reading sets the page offers, which are also the four the eval suite grades
# against the live model. Keeping them the same means what a reader watches here
# is what those evals measured.
SCENARIOS = ["Rising usage", "Flat usage", "Mostly estimated", "Short history"]


def packages_imported_by(path: Path) -> set[str]:
    """
    The top-level packages a module imports.

    Parsed rather than grepped, so that naming the service in a comment or a
    docstring -- which the demo does, at length -- is not mistaken for importing
    it. A test that fails on prose gets deleted by the next person to touch it.
    """
    tree = ast.parse(path.read_text())
    packages = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            packages.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            packages.add(node.module.split(".")[0])
    return packages


class TestTheDemoPage:
    def test_it_renders(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/html")

    @pytest.mark.parametrize("label", SCENARIOS)
    def test_it_offers_the_reading_sets_the_evals_grade(self, client, label):
        assert label in client.get("/").content.decode()

    def test_it_calls_the_public_endpoint(self, client):
        """
        The page hardcodes the API path in a fetch call, so this asserts that the
        path it hardcodes is the one the URLconf actually serves. Without it,
        moving the endpoint leaves a demo that fails only when someone clicks it.
        """
        body = client.get("/").content.decode()

        assert f'fetch("{reverse("insights")}"' in body

    @pytest.mark.parametrize("fact_key", list(analytics.FactKey))
    def test_it_displays_every_fact_the_service_can_publish(self, client, fact_key):
        """
        A fact the service derives but the page does not show would be invisible
        to anyone checking a recommendation against it, which is the one job the
        left-hand column has.
        """
        assert f'"{fact_key.value}"' in client.get("/").content.decode()


class TestItIsAClientNotPartOfTheService:
    def test_the_demo_does_not_import_the_service(self):
        """
        The page is evidence that the endpoint works, and it is only evidence
        while it reaches the endpoint the same way a stranger would. A single
        `from insights import ...` would turn it into a view with privileged
        access that happens to look like a client.
        """
        modules = [path for path in DEMO_ROOT.rglob("*.py") if "tests" not in path.parts]
        assert modules, "no demo modules found, so this test is not checking anything"

        reaching_in = [path.name for path in modules if "insights" in packages_imported_by(path)]

        assert not reaching_in, (
            f"the demo should call the API over HTTP, not import it: {reaching_in}"
        )
