from django.contrib import admin
from django.urls import include, path

from incidents.dashboard_views import DashboardView


urlpatterns = [
    path("admin/", admin.site.urls),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("api/v1/", include("incidents.urls")),
]