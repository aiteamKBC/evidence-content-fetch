import os
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(BASE_DIR / ".env")


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "False").lower() in {"1", "true", "yes", "on"}
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174",
    ).split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "aptem",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "config.wsgi.application"

def database_from_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use postgres:// or postgresql://")

    options = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": parsed.port or 5432,
        "OPTIONS": options,
    }


DATABASE_URL = os.environ.get("DATABASE_URL", "")
DATABASE_SEARCH_PATH = os.environ.get("DATABASE_SEARCH_PATH", "fetching_evidence,public")
DJANGO_FORCE_SQLITE = os.environ.get("DJANGO_FORCE_SQLITE", "False").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DATABASES = {
    "default": database_from_url(DATABASE_URL)
    if DATABASE_URL and not DJANGO_FORCE_SQLITE
    else {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Cloud Postgres (Neon) can drop a connection that sits idle during a long cycle.
# Fail fast and keep the socket alive instead of hanging; health-check reused conns.
if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
    _pg_options = DATABASES["default"].setdefault("OPTIONS", {})
    for _key, _value in {
        "connect_timeout": 15,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }.items():
        _pg_options.setdefault(_key, _value)
    DATABASES["default"].setdefault("CONN_HEALTH_CHECKS", True)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

APTEM_BASE_URL = os.environ.get("APTEM_BASE_URL", "https://kentbusinesscollege.aptem.co.uk")
APTEM_COOKIE_HEADER = os.environ.get("APTEM_COOKIE_HEADER", "")
APTEM_BROWSER_HEADLESS = os.environ.get("APTEM_BROWSER_HEADLESS", "true").lower() in {"1", "true", "yes", "on"}
APTEM_LOGIN_EMAIL = os.environ.get("APTEM_LOGIN_EMAIL", "")
APTEM_LOGIN_PASSWORD = os.environ.get("APTEM_LOGIN_PASSWORD", "")
APTEM_AUTO_REFRESH_COOKIE = os.environ.get("APTEM_AUTO_REFRESH_COOKIE", "True").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
APTEM_DOWNLOAD_DIRECTORY = os.environ.get("APTEM_DOWNLOAD_DIRECTORY", "media/aptem_evidence")
AZURE_REVIEWS_CONTAINER_URL = os.environ.get("AZURE_REVIEWS_CONTAINER_URL", "")
REVIEW_PDF_MAX_BYTES = int(os.environ.get("REVIEW_PDF_MAX_BYTES", str(50 * 1024 * 1024)))
REVIEW_PDF_MAX_ATTEMPTS = int(os.environ.get("REVIEW_PDF_MAX_ATTEMPTS", "5"))
APTEM_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("APTEM_REQUEST_TIMEOUT_SECONDS", "120"))
APTEM_REQUEST_DELAY_SECONDS = float(os.environ.get("APTEM_REQUEST_DELAY_SECONDS", "1.5"))
APTEM_MAX_RETRIES = int(os.environ.get("APTEM_MAX_RETRIES", "2"))
APTEM_MAX_BATCH_SIZE = int(os.environ.get("APTEM_MAX_BATCH_SIZE", "100"))
APTEM_MIN_WORD_COUNT = int(os.environ.get("APTEM_MIN_WORD_COUNT", "1000"))
APTEM_LOCAL_AUTH_BYPASS = os.environ.get("APTEM_LOCAL_AUTH_BYPASS", "True").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")
