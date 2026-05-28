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
    "app",
]

# No middleware: CommonMiddleware is sync and would be wrapped in
# sync_to_async on every async request, throttling the async path through a
# thread pool and obscuring what the benchmark measures (the ORM).
MIDDLEWARE = []

# The DB-bound scenario needs a real async-capable backend; point at the
# local postgres container when BENCH_DB=postgres, else use sqlite.
if os.environ.get("BENCH_DB") == "postgres":
    # Optional psycopg connection pool. Without it (and with CONN_MAX_AGE=0)
    # every request opens and closes a fresh connection, so the db scenario
    # measures connection setup as much as query execution. Set BENCH_PG_POOL=1
    # to hand out pooled connections instead (applies to sync and async paths).
    _pg_options = {}
    if os.environ.get("BENCH_PG_POOL") == "1":
        _pg_options["pool"] = {
            "min_size": int(os.environ.get("BENCH_PG_POOL_MIN", "4")),
            "max_size": int(os.environ.get("BENCH_PG_POOL_MAX", "16")),
        }
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("BENCH_PG_NAME", "djangoasync"),
            "USER": os.environ.get("BENCH_PG_USER", "djangoasync"),
            "PASSWORD": os.environ.get("BENCH_PG_PASSWORD", "djangoasync"),
            "HOST": os.environ.get("BENCH_PG_HOST", "127.0.0.1"),
            "PORT": os.environ.get("BENCH_PG_PORT", "55432"),
            "CONN_MAX_AGE": int(os.environ.get("BENCH_PG_CONN_MAX_AGE", "0")),
            "OPTIONS": _pg_options,
        },
    }
else:
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
