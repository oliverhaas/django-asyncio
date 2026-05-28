"""Minimal Django settings for the benchmark app.

No database is needed for the I/O-bound and CPU-bound scenarios; they
exercise the framework request path, not the ORM. A sqlite entry is
present only so `django.setup()` is happy if a view ever touches the ORM.
"""

import os

SECRET_KEY = "benchmark-not-a-secret"
DEBUG = False
ALLOWED_HOSTS = ["*"]

ROOT_URLCONF = "app.urls"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

USE_TZ = True

# Sleep duration (seconds) for the I/O-bound scenario.
BENCH_IO_SLEEP = float(os.environ.get("BENCH_IO_SLEEP", "0.05"))
# Number of sha256 rounds over a 64 KB buffer for the CPU-bound scenario.
BENCH_CPU_ROUNDS = int(os.environ.get("BENCH_CPU_ROUNDS", "60"))

LOGGING_CONFIG = None
