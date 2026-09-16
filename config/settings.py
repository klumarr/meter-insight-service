"""
Django settings for the Meter Insight Service.

This is a stateless JSON API. It has no database, no sessions and no user
accounts: every value it returns is derived from the request body. The installed
apps and middleware below are trimmed to match, so nothing is loaded that the
service does not actually use.

The single HTML template in the project belongs to `demo`, which is a client of
the API rather than a part of it. Template rendering is configured for that one
page and nothing else.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Reads BASE_DIR/.env into the environment if the file exists. In a deployed
# environment there is no .env file and the variables are set directly, so this
# is a no-op there rather than a second source of truth.
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "insecure-key-for-local-development-only")

DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "insights",
    # A demonstration client for the API, served at /. It is a separate app
    # rather than another view inside `insights` because it consumes the service
    # over HTTP like any other caller, and the directory boundary says so.
    "demo",
]

# No session or authentication middleware, because the service holds no per-user
# state. CSRF protection is also omitted: it defends cookie-authenticated browser
# requests, and this API has no cookies and no ambient credentials to forge.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

# APP_DIRS is what finds demo/templates/. There are no context processors because
# the demo page receives no server-side context: it builds its request in the
# browser and calls the public endpoint, so Django has nothing to inject into it.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

WSGI_APPLICATION = "config.wsgi.application"

# Deliberately empty. Nothing is persisted, so configuring a database would
# imply a capability the service does not have.
DATABASES: dict[str, dict[str, object]] = {}

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True


# Language model
# The key is read from the environment and has no default, so a missing key
# fails loudly at call time rather than silently producing fallback responses.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# An explicit timeout, because the default in most HTTP clients is either very
# long or absent entirely. A request that hangs holds a worker open, and the
# fallback can answer in microseconds, so waiting is never worth it.
ANTHROPIC_TIMEOUT_SECONDS = float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "20"))

# Three recommendations capped at 80 and 300 characters cannot need more than
# this. The ceiling bounds the cost of a single call.
ANTHROPIC_MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "1024"))


# Caching
# In-memory and per-process, which is the honest choice for a service that runs
# as a single container in this form. It is also a lie at scale: four workers
# means four caches and four times the misses. Swapping in Redis is a settings
# change and nothing else, because the code only ever talks to Django's cache
# API, and that is the point of configuring it here rather than reaching for a
# dictionary in the module that needs it.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "meter-insight",
    }
}

# How long a set of model-written recommendations stays usable. Identical
# readings produce an identical cache key, so this is not about correctness --
# it bounds how long the service can keep serving wording from a prompt or a
# model that has since been replaced.
INSIGHT_CACHE_SECONDS = int(os.environ.get("INSIGHT_CACHE_SECONDS", "3600"))
