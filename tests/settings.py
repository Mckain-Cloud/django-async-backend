import os

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
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django_async_backend",
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "test_app",
]

MIDDLEWARE = [
    "django_async_backend.middleware.close_async_connections",
    "django_async_backend.contrib.sessions.AsyncSessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_async_backend.contrib.messages.AsyncMessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ROOT_URLCONF = "admin_urls"
STATIC_URL = "static/"

SECRET_KEY = "tests-only"

AUTHENTICATION_BACKENDS = [
    "django_async_backend.contrib.auth.AsyncModelBackend",
]
SESSION_ENGINE = "django_async_backend.contrib.sessions"
ASYNC_BACKEND_ASYNC_LAST_LOGIN = True

# The test database creation runs createcachetable for database caches.
CACHES = {
    "default": {
        "BACKEND": "django_async_backend.core.cache.AsyncDatabaseCache",
        "LOCATION": "test_cache_table",
        "OPTIONS": {"MAX_ENTRIES": 50, "CULL_FREQUENCY": 2},
    },
    # tests/core/test_redis_cache.py skips itself when nothing listens here.
    "redis": {
        "BACKEND": "django_async_backend.core.cache.redis.AsyncRedisCache",
        "LOCATION": os.environ.get(
            "DAB_TEST_REDIS_URL", "redis://localhost:6379/9"
        ),
        "KEY_PREFIX": "dab-tests",
    },
}
# Fast hasher: these tests log in a lot.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

USE_TZ = False
