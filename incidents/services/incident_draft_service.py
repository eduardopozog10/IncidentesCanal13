from django.db import transaction

from incidents.models import IncidentDraft, Technician
from incidents.services.conversation_lock import conversation_lock


class IncidentDraftService:
    def cancel_active_draft(
        self,
        *,
        technician: Technician,
    ) -> IncidentDraft | None:
        with conversation_lock(f"reporter:{technician.pk}"):
            return self._cancel_atomic(technician)

    @transaction.atomic
    def _cancel_atomic(self, technician):
        Technician.objects.select_for_update().get(pk=technician.pk)
        draft = (
            IncidentDraft.objects.filter(
                technician=technician,
                status__in=[
                    IncidentDraft.Status.COLLECTING,
                    IncidentDraft.Status.READY,
                ],
            )
            .order_by("-updated_at")
            .first()
        )

        if draft is None:
            return None

        draft.status = IncidentDraft.Status.CANCELLED
        draft.missing_fields = []
        draft.last_question = ""
        draft.save(
            update_fields=[
                "status",
                "missing_fields",
                "last_question",
                "updated_at",
            ]
        )

        return draft
