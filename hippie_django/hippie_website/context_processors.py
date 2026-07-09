"""Template context processors for the HIPPIE website."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest

from .models import ReleaseMeta


def release(request: HttpRequest) -> dict[str, "ReleaseMeta | None"]:
    """Expose the current HIPPIE release to every template as ``current_release``."""
    return {"current_release": ReleaseMeta.current()}


def google_analytics(request: HttpRequest) -> dict[str, str]:
    """Expose the GA4 measurement ID to every template as ``ga_measurement_id``."""
    return {"ga_measurement_id": settings.GA_MEASUREMENT_ID}
