from django.urls import path

from . import views

urlpatterns = [
    # Landing page — protein search
    path("", views.protein_query_view, name="index"),

    # Protein query results (JSON endpoint consumed by React)
    path("api/query/", views.protein_query_api, name="protein_query_api"),

    # Interaction detail page
    path("interaction/<int:pk>/", views.interaction_detail_view, name="interaction_detail"),

    # Protein detail page (optional: clicked from results table)
    path("protein/<int:pk>/", views.protein_detail_view, name="protein_detail"),
]