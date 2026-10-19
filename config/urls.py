from django.contrib import admin
from django.urls import include, path
from django.contrib.auth.views import LogoutView

from incidents.dashboard_views import DashboardView


urlpatterns = [
    path("admin/", admin.site.urls),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("dashboard/logout/", LogoutView.as_view(next_page="admin:login"), name="dashboard-logout"),
    path("dashboard/incidents/<int:pk>/", DashboardView.as_view(), name="dashboard-incident-detail"),
    path("api/v1/", include("incidents.urls")),
]
