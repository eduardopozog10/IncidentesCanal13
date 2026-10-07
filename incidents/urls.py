from django.urls import path

from .views import (
    IncidentAnalyticsView,
    IncidentDetailView,
    IncidentListView,
    IncidentSummaryView,
    IncomingTextMessageTestView,
    TelegramWebhookView,
)


app_name = "incidents"

urlpatterns = [
    path(
        "incidents/",
        IncidentListView.as_view(),
        name="incident-list",
    ),
    path(
        "incidents/summary/",
        IncidentSummaryView.as_view(),
        name="incident-summary",
    ),
    path(
        "incidents/analytics/",
        IncidentAnalyticsView.as_view(),
        name="incident-analytics",
    ),
    path(
        "incidents/<int:pk>/",
        IncidentDetailView.as_view(),
        name="incident-detail",
    ),
    path(
        "messages/test/",
        IncomingTextMessageTestView.as_view(),
        name="incoming-text-message-test",
    ),
    path(
        "messaging/telegram/webhook/",
        TelegramWebhookView.as_view(),
        name="telegram-webhook",
    ),
]