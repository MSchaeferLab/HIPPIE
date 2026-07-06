"""Template context processors for the HIPPIE website."""

from __future__ import annotations

from django.http import HttpRequest

from .models import ReleaseMeta


def release(request: HttpRequest) -> dict[str, "ReleaseMeta | None"]:
    """Expose the current HIPPIE release to every template as ``current_release``."""
    return {"current_release": ReleaseMeta.current()}
