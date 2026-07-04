from django.apps import AppConfig


class HippieWebsiteConfig(AppConfig):
    name = "hippie_website"

    def ready(self):
        from . import signals  # noqa: F401
