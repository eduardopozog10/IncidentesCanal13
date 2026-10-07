from incidents.models import IncidentDraft, Technician


class IncidentDraftService:
    def cancel_active_draft(
        self,
        *,
        technician: Technician,
    ) -> IncidentDraft | None:
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