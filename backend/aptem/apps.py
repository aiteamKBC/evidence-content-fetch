from django.apps import AppConfig


class AptemConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "aptem"

    def ready(self):
        from . import db  # noqa: F401
