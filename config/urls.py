from django.urls import URLPattern, URLResolver, path

from insights import views

# No trailing slash: this is a JSON API called by other programs, not a site
# navigated by a browser, and a redirect is a wasted round trip for a POST.
urlpatterns: list[URLPattern | URLResolver] = [
    path("api/insights", views.insights, name="insights"),
]
