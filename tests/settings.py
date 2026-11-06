DATABASES = {
    "default": {
        "ENGINE": "django_async_backend.db.backends.postgresql",
        "NAME": "postgres",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "localhost",
        "PORT": 5432,
    },
    "other": {
        "ENGINE": "django_async_backend.db.backends.postgresql",
        "NAME": "postgres",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "localhost",
        "PORT": 5432,
    },
    "legacy": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django_async_backend",
    "test_app",
]

SECRET_KEY = "tests-only"

AUTHENTICATION_BACKENDS = [
    "django_async_backend.contrib.auth.AsyncModelBackend",
]
SESSION_ENGINE = "django_async_backend.contrib.sessions"
ASYNC_BACKEND_ASYNC_LAST_LOGIN = True
# Fast hasher: these tests log in a lot.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

MIDDLEWARE = [
    "django_async_backend.middleware.close_async_connections",
]

USE_TZ = False
