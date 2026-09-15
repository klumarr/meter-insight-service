"""
Django settings for the Meter Insight Service.

This is a stateless JSON API. It has no database, no sessions, no user accounts
and no HTML templates: every value it returns is derived from the request body.
The installed apps and middleware below are trimmed to match, so nothing is
loaded that the service does not actually use.
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
]

# No session or authentication middleware, because the service holds no per-user
# state. CSRF protection is also omitted: it defends cookie-authenticated browser
# requests, and this API has no cookies and no ambient credentials to forge.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

WSGI_APPLICATION = "config.wsgi.application"

# Deliberately empty. Nothing is persisted, so configuring a database would
# imply a capability the service does not have.
DATABASES: dict[str, dict[str, object]] = {}

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
