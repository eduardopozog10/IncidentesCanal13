from django.contrib import admin
from django.urls import include, path
from django.contrib.auth.views import LogoutView

from incidents.dashboard_views import DashboardView
from incidents import user_views


urlpatterns = [
    # Mantener los enlaces y redirecciones históricos /admin/login/?next=...
    path("admin/login/", user_views.DashboardLoginView.as_view()),
    path("dashboard/login/", user_views.DashboardLoginView.as_view(), name="dashboard-login"),
    path("dashboard/password-change/", user_views.DashboardPasswordChangeView.as_view(), name="dashboard-password-change"),
    path("dashboard/users/", user_views.user_list, name="dashboard-users"),
    path("dashboard/users/new/", user_views.user_edit, name="dashboard-user-create"),
    path("dashboard/users/<int:pk>/edit/", user_views.user_edit, name="dashboard-user-edit"),
    path("dashboard/users/<int:pk>/password/", user_views.user_password_reset, name="dashboard-user-password"),
    path("dashboard/users/password-link/", user_views.user_handoff, name="dashboard-user-handoff"),
    path("dashboard/password/<uidb64>/<token>/", user_views.LocalPasswordConfirmView.as_view(), name="dashboard-password-confirm"),
    path("dashboard/password-complete/", user_views.LocalPasswordCompleteView.as_view(), name="dashboard-password-complete"),
    path("admin/", admin.site.urls),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("dashboard/logout/", LogoutView.as_view(next_page="admin:login"), name="dashboard-logout"),
    path("dashboard/incidents/<int:pk>/", DashboardView.as_view(), name="dashboard-incident-detail"),
    path("api/v1/", include("incidents.urls")),
]
