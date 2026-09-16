from django.urls import URLPattern, URLResolver, path

from demo import views as demo_views
from insights import views

# No trailing slash on the API route: it is called by other programs, not
# navigated by a browser, and a redirect is a wasted round trip for a POST.
#
# The two routes are two different things sharing a process. `api/insights` is
# the service. `/` is a page that calls it over HTTP like any other client, and
# removing it would take nothing away from the service at all.
urlpatterns: list[URLPattern | URLResolver] = [
    path("api/insights", views.insights, name="insights"),
    path("", demo_views.index, name="demo"),
]
