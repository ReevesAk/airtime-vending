import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"

# The dev fallback only applies with DEBUG on. Anywhere else a missing key is
# a deployment mistake, and silently signing with a value that's committed to
# this repo is worse than refusing to start.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is not true.")
    SECRET_KEY = "dev-secret-key-not-for-production"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "vending",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "airtime"),
        "USER": os.environ.get("POSTGRES_USER", "airtime"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "airtime"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        # Money correctness depends on real row locking (SELECT ... FOR
        # UPDATE) across the vend-once-per-session guard. SQLite's locking
        # model can't give the same guarantee under concurrent writers, so
        # Postgres is required, including for the test suite -- see
        # vending/tests/test_concurrency.py.
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"

REST_FRAMEWORK = {
    # The USSD callback is plain form-encoded in, plain text out -- not a
    # JSON API -- so no DRF parser/renderer/serializer machinery is used
    # for it. DRF is still the framework because it's the fastest one for
    # the team's Python stack; APIView gives consistent request parsing and
    # a place to hang auth/throttling if a JSON endpoint (e.g. an
    # ops-triggered reconciliation run) is added later.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
}

# Not currently enforced anywhere: the only endpoint in this service is the
# /ussd gateway callback, and USSD aggregators don't attach bearer tokens --
# that trust boundary is normally an IP allowlist or mutual TLS at the
# infra layer, which is out of scope here. Kept as a setting because the
# brief allows a shared API key as the auth model for anything beyond the
# callback (e.g. a future ops/reconciliation-trigger endpoint) -- see
# README "What I deliberately did not build".
API_KEY = os.environ.get("API_KEY", "dev-api-key")

OPERATOR_BASE_URL = os.environ.get("OPERATOR_BASE_URL", "http://localhost:9000")

# Budget for the outbound call to the operator during a vend confirmation.
# The USSD gateway gives the whole request 8 seconds; this leaves headroom
# for our own DB work and network transit to/from the gateway. See README
# "USSD timing budget" for the full justification.
OPERATOR_CALL_TIMEOUT_SECONDS = float(os.environ.get("OPERATOR_CALL_TIMEOUT_SECONDS", "4.5"))

# How long an idle USSD session stays valid. Kept short by default so the
# ussd-gateway/driver.py session_expiry scenario doesn't have to sleep long;
# a production deployment would set this to something like 90-120 seconds.
USSD_SESSION_TTL_SECONDS = float(os.environ.get("USSD_SESSION_TTL_SECONDS", "5"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "vending": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
