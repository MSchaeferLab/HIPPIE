from django.urls import path

from . import views

urlpatterns = [
    # ── Main tabs ──────────────────────────────────────────────
    path("", views.protein_query_view, name="index"),
    path("interaction-query/", views.interaction_query_view, name="interaction_query"),
    path("network/", views.network_query_view, name="network_query"),
    path("browse/", views.browse_view, name="browse"),
    # ── Utility pages ──────────────────────────────────────────
    path("download/", views.download_view, name="download"),
    path("information/", views.information_view, name="information"),
    # ── Detail pages ───────────────────────────────────────────
    path(
        "interaction/<int:pk>/",
        views.interaction_detail_view,
        name="interaction_detail",
    ),
    path(
        "noninteraction/<int:pk>/",
        views.noninteraction_detail_view,
        name="noninteraction_detail",
    ),
    path("protein/<int:pk>/", views.protein_detail_view, name="protein_detail"),
    # ── JSON API endpoints ──────────────────────────────────────
    path("api/query/", views.protein_query_api, name="protein_query_api"),
    path("api/interaction/", views.interaction_query_api, name="interaction_query_api"),
    path("api/network/", views.network_query_api, name="network_query_api"),
    path("api/browse/", views.browse_api, name="browse_api"),
    path("api/browse/filters/", views.browse_filter_meta, name="browse_filter_meta"),
]
