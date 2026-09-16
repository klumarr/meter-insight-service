"""
A demonstration client for the service, served from the service itself.

This app exists so that the behaviour described in the README can be watched
happening rather than read about: which facts are available, which are withheld,
which fact each recommendation cites, and whether the wording came from the
model or the fallback.

It is a *client*, and the separation is the point. It imports nothing from
`insights`, holds no business logic, and has no privileged access to anything.
The page builds a request in the browser and POSTs it to the same public
`/api/insights` endpoint that curl would, which means a demo that works is also
evidence that the endpoint works. A test in this package pins that import
boundary, because a demo that quietly grew a shortcut into the service would
stop being evidence of anything.
"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render


def index(request: HttpRequest) -> HttpResponse:
    """GET / — the demo page. Takes no parameters and reads no state."""
    return render(request, "demo/index.html")
