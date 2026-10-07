from datetime import timedelta

from django.db.models import (
    Avg,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    Q,
)
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from incidents.messaging.telegram_update_handler import (
    TelegramUpdateHandler,
)
from incidents.models import Incident
from incidents.serializers import (
    IncidentSerializer,
    IncidentStatusUpdateSerializer,
    IncomingTextMessageSerializer,
)
from incidents.services.incoming_message_service import (
    ChannelNotAuthorizedError,
    IncomingMessageService,
)


def filter_queryset_by_date_range(queryset, request):
    date_from_value = request.query_params.get(
        "date_from"
    )
    date_to_value = request.query_params.get(
        "date_to"
    )

    date_from = (
        parse_date(date_from_value)
        if date_from_value
        else None
    )

    date_to = (
        parse_date(date_to_value)
        if date_to_value
        else None
    )

    if date_from:
        queryset = queryset.filter(
            created_at__date__gte=date_from,
        )

    if date_to:
        queryset = queryset.filter(
            created_at__date__lte=date_to,
        )

    return queryset


class IncidentListView(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = IncidentSerializer

    ALLOWED_ORDERING_FIELDS = {
        "created_at",
        "updated_at",
        "priority",
        "status",
        "unit",
        "equipment",
    }

    def get_queryset(self):
        queryset = Incident.objects.select_related(
            "technician"
        )

        queryset = filter_queryset_by_date_range(
            queryset,
            self.request,
        )

        status_value = self.request.query_params.get(
            "status"
        )
        priority = self.request.query_params.get(
            "priority"
        )
        unit = self.request.query_params.get(
            "unit"
        )
        equipment = self.request.query_params.get(
            "equipment"
        )
        search = self.request.query_params.get(
            "search"
        )
        ordering = self.request.query_params.get(
            "ordering",
            "-created_at",
        )

        if status_value:
            queryset = queryset.filter(
                status=status_value,
            )

        if priority:
            queryset = queryset.filter(
                priority=priority,
            )

        if unit:
            queryset = queryset.filter(
                unit__icontains=unit,
            )

        if equipment:
            queryset = queryset.filter(
                equipment__icontains=equipment,
            )

        if search:
            queryset = queryset.filter(
                Q(unit__icontains=search)
                | Q(equipment__icontains=search)
                | Q(failure_type__icontains=search)
                | Q(description__icontains=search)
                | Q(original_message__icontains=search)
                | Q(
                    technician__full_name__icontains=search
                )
            )

        ordering_field = ordering.lstrip("-")

        if ordering_field not in self.ALLOWED_ORDERING_FIELDS:
            ordering = "-created_at"

        return queryset.order_by(ordering)


class IncidentSummaryView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        queryset = Incident.objects.all()

        queryset = filter_queryset_by_date_range(
            queryset,
            request,
        )

        summary = queryset.aggregate(
            total=Count("id"),
            reported=Count(
                "id",
                filter=Q(
                    status=Incident.Status.REPORTED
                ),
            ),
            under_review=Count(
                "id",
                filter=Q(
                    status=Incident.Status.UNDER_REVIEW
                ),
            ),
            in_progress=Count(
                "id",
                filter=Q(
                    status=Incident.Status.IN_PROGRESS
                ),
            ),
            resolved=Count(
                "id",
                filter=Q(
                    status=Incident.Status.RESOLVED
                ),
            ),
            closed=Count(
                "id",
                filter=Q(
                    status=Incident.Status.CLOSED
                ),
            ),
            cancelled=Count(
                "id",
                filter=Q(
                    status=Incident.Status.CANCELLED
                ),
            ),
            low=Count(
                "id",
                filter=Q(
                    priority=Incident.Priority.LOW
                ),
            ),
            medium=Count(
                "id",
                filter=Q(
                    priority=Incident.Priority.MEDIUM
                ),
            ),
            high=Count(
                "id",
                filter=Q(
                    priority=Incident.Priority.HIGH
                ),
            ),
            critical=Count(
                "id",
                filter=Q(
                    priority=Incident.Priority.CRITICAL
                ),
            ),
        )

        return Response(
            {
                "total": summary["total"],
                "by_status": {
                    "reported": summary["reported"],
                    "under_review": summary[
                        "under_review"
                    ],
                    "in_progress": summary[
                        "in_progress"
                    ],
                    "resolved": summary["resolved"],
                    "closed": summary["closed"],
                    "cancelled": summary["cancelled"],
                },
                "by_priority": {
                    "low": summary["low"],
                    "medium": summary["medium"],
                    "high": summary["high"],
                    "critical": summary["critical"],
                },
            },
            status=status.HTTP_200_OK,
        )


class IncidentAnalyticsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        queryset = Incident.objects.all()

        queryset = filter_queryset_by_date_range(
            queryset,
            request,
        )

        open_statuses = [
            Incident.Status.REPORTED,
            Incident.Status.UNDER_REVIEW,
            Incident.Status.IN_PROGRESS,
        ]

        resolution_duration = ExpressionWrapper(
            F("resolved_at") - F("created_at"),
            output_field=DurationField(),
        )

        metrics = queryset.aggregate(
            total=Count("id"),
            open=Count(
                "id",
                filter=Q(
                    status__in=open_statuses
                ),
            ),
            resolved=Count(
                "id",
                filter=Q(
                    status=Incident.Status.RESOLVED
                ),
            ),
            critical=Count(
                "id",
                filter=Q(
                    priority=Incident.Priority.CRITICAL
                ),
            ),
            critical_open=Count(
                "id",
                filter=Q(
                    priority=Incident.Priority.CRITICAL,
                    status__in=open_statuses,
                ),
            ),
            average_resolution=Avg(
                resolution_duration,
                filter=Q(
                    resolved_at__isnull=False
                ),
            ),
        )

        average_resolution = metrics[
            "average_resolution"
        ]

        average_resolution_seconds = None

        if average_resolution is not None:
            average_resolution_seconds = int(
                average_resolution.total_seconds()
            )

        older_than_24_hours = queryset.filter(
            status__in=open_statuses,
            created_at__lt=(
                timezone.now()
                - timedelta(hours=24)
            ),
        ).count()

        by_status = queryset.values(
            "status"
        ).annotate(
            count=Count("id")
        ).order_by(
            "-count"
        )

        by_priority = queryset.values(
            "priority"
        ).annotate(
            count=Count("id")
        ).order_by(
            "-count"
        )

        by_equipment = queryset.exclude(
            equipment=""
        ).values(
            "equipment"
        ).annotate(
            count=Count("id")
        ).order_by(
            "-count",
            "equipment",
        )[:10]

        by_unit = queryset.exclude(
            unit=""
        ).values(
            "unit"
        ).annotate(
            count=Count("id")
        ).order_by(
            "-count",
            "unit",
        )[:10]

        daily_trend = queryset.annotate(
            day=TruncDate("created_at")
        ).values(
            "day"
        ).annotate(
            count=Count("id")
        ).order_by(
            "day"
        )

        status_labels = dict(
            Incident.Status.choices
        )

        priority_labels = dict(
            Incident.Priority.choices
        )

        status_data = [
            {
                "value": item["status"],
                "label": status_labels.get(
                    item["status"],
                    item["status"],
                ),
                "count": item["count"],
            }
            for item in by_status
        ]

        priority_data = [
            {
                "value": item["priority"],
                "label": priority_labels.get(
                    item["priority"],
                    item["priority"],
                ),
                "count": item["count"],
            }
            for item in by_priority
        ]

        equipment_data = [
            {
                "equipment": item["equipment"],
                "count": item["count"],
            }
            for item in by_equipment
        ]

        unit_data = [
            {
                "unit": item["unit"],
                "count": item["count"],
            }
            for item in by_unit
        ]

        trend_data = [
            {
                "date": (
                    item["day"].isoformat()
                    if item["day"]
                    else None
                ),
                "count": item["count"],
            }
            for item in daily_trend
        ]

        return Response(
            {
                "metrics": {
                    "total": metrics["total"],
                    "open": metrics["open"],
                    "resolved": metrics["resolved"],
                    "critical": metrics["critical"],
                    "critical_open": metrics[
                        "critical_open"
                    ],
                    "average_resolution_seconds": (
                        average_resolution_seconds
                    ),
                    "open_over_24_hours": (
                        older_than_24_hours
                    ),
                },
                "by_status": status_data,
                "by_priority": priority_data,
                "by_equipment": equipment_data,
                "by_unit": unit_data,
                "daily_trend": trend_data,
            },
            status=status.HTTP_200_OK,
        )


class IncidentDetailView(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = IncidentSerializer

    def get_queryset(self):
        return Incident.objects.select_related(
            "technician"
        )

    def patch(self, request, *args, **kwargs):
        incident = self.get_object()

        serializer = IncidentStatusUpdateSerializer(
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)

        new_status = serializer.validated_data["status"]

        incident.status = new_status

        if new_status == Incident.Status.RESOLVED:
            if incident.resolved_at is None:
                incident.resolved_at = timezone.now()
        else:
            incident.resolved_at = None

        incident.save(
            update_fields=[
                "status",
                "resolved_at",
                "updated_at",
            ]
        )

        response_serializer = IncidentSerializer(
            incident,
        )

        return Response(
            response_serializer.data,
            status=status.HTTP_200_OK,
        )


class IncomingTextMessageTestView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = IncomingTextMessageSerializer(
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)

        validated_data = serializer.validated_data

        try:
            result = IncomingMessageService().process_text(
                provider=validated_data["provider"],
                external_user_id=validated_data[
                    "external_user_id"
                ],
                external_chat_id=validated_data[
                    "external_chat_id"
                ],
                external_message_id=validated_data[
                    "external_message_id"
                ],
                text=validated_data["text"],
                raw_payload=request.data,
            )
        except ChannelNotAuthorizedError as error:
            return Response(
                {
                    "detail": str(error),
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as error:
            return Response(
                {
                    "detail": (
                        "No fue posible procesar el mensaje."
                    ),
                    "error": str(error),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        message = result["message"]

        if result["duplicate"]:
            return Response(
                {
                    "message": (
                        "El mensaje ya había sido recibido."
                    ),
                    "duplicate": True,
                    "message_id": message.pk,
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            {
                "message": "Mensaje recibido y procesado.",
                "duplicate": False,
                "message_id": message.pk,
                "provider": message.provider,
                "processing": result["processing"],
            },
            status=status.HTTP_201_CREATED,
        )


class TelegramWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            result = TelegramUpdateHandler().handle(
                request.data,
            )
        except Exception:
            return Response(
                {
                    "detail": (
                        "No fue posible procesar la actualización "
                        "de Telegram."
                    ),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "ok": True,
                "result": result,
            },
            status=status.HTTP_200_OK,
        )