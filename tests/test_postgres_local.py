# Local-dev settings for running Django's test suite against the postgres
# container started for this fork. Pair with runtests.py --settings=test_postgres_local.
# The container is spun up by `docker run -d --name django-asyncio-pg
# -e POSTGRES_PASSWORD=djangoasync -e POSTGRES_USER=djangoasync
# -e POSTGRES_DB=djangoasync -p 55432:5432 postgres:17`.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "djangoasync",
        "USER": "djangoasync",
        "PASSWORD": "djangoasync",
        "HOST": "127.0.0.1",
        "PORT": "55432",
    },
    "other": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "djangoasync_other",
        "USER": "djangoasync",
        "PASSWORD": "djangoasync",
        "HOST": "127.0.0.1",
        "PORT": "55432",
    },
}

SECRET_KEY = "django_tests_secret_key"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

USE_TZ = False
