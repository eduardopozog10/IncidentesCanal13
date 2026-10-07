from django.test import TestCase, override_settings

from django.utils import timezone



from incidents.models import (

    ChannelMessage,

    Incident,

    IncidentDraft,

    Technician,

    TechnicianChannel,

)

from incidents.services.incoming_message_service import (

    IncomingMessageService,

)





@override_settings(AI_PROVIDER="mock")
class IncomingMessageServiceTests(TestCase):
    def setUp(self):
        self.technician = Technician.objects.create(
            full_name="Técnico de prueba",
            is_active=True,
        )

        self.channel = TechnicianChannel.objects.create(
            technician=self.technician,
            provider=TechnicianChannel.Provider.TELEGRAM,
            external_user_id="123456789",
            external_chat_id="123456789",
            is_active=True,
        )

        self.service = IncomingMessageService()

    def test_creates_incident_from_complete_message_with_explicit_priority(self):
        result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:1",
            text=(
                "La cámara de la unidad móvil 9 no enciende, "
                "prioridad alta"
            ),
            raw_payload={
                "test": True,
            },
        )

        self.assertFalse(result["duplicate"])

        processing = result["processing"]

        self.assertEqual(
            processing["action"],
            "incident_created",
        )
        self.assertTrue(
            processing["incident_created"],
        )

        incident = Incident.objects.get()

        self.assertEqual(
            incident.technician,
            self.technician,
        )
        self.assertEqual(
            incident.unit,
            "Unidad movil 9",
        )
        self.assertEqual(
            incident.equipment,
            "Cámara",
        )
        self.assertEqual(
            incident.failure_type,
            "No enciende",
        )
        self.assertEqual(
            incident.priority,
            Incident.Priority.HIGH,
        )

        message = ChannelMessage.objects.get(
            direction=ChannelMessage.Direction.INBOUND,
        )

        self.assertEqual(
            message.status,
            ChannelMessage.Status.PROCESSED,
        )
        self.assertEqual(
            message.incident,
            incident,
        )

    def test_requires_explicit_priority_before_creating_incident(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:priority-1",
            text="La cámara de la unidad móvil 9 no enciende",
            raw_payload={
                "test": True,
            },
        )

        first_processing = first_result["processing"]

        self.assertEqual(
            first_processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            first_processing["incident_created"],
        )
        self.assertIn(
            "priority",
            first_processing["missing_fields"],
        )
        self.assertIsNone(
            first_processing["extracted_data"]["priority"],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:priority-2",
            text="Alta",
            raw_payload={
                "test": True,
            },
        )

        second_processing = second_result["processing"]

        self.assertEqual(
            second_processing["action"],
            "incident_created",
        )
        self.assertTrue(
            second_processing["incident_created"],
        )

        incident = Incident.objects.get()

        self.assertEqual(
            incident.priority,
            Incident.Priority.HIGH,
        )

    def test_duplicate_message_is_not_processed_twice(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:2",
            text=(
                "La cámara de la unidad móvil 10 no enciende, "
                "prioridad alta"
            ),
            raw_payload={
                "test": True,
            },
        )

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:2",
            text=(
                "La cámara de la unidad móvil 10 no enciende, "
                "prioridad alta"
            ),
            raw_payload={
                "test": True,
            },
        )

        self.assertFalse(
            first_result["duplicate"],
        )
        self.assertTrue(
            second_result["duplicate"],
        )
        self.assertIsNone(
            second_result["processing"],
        )

        self.assertEqual(
            Incident.objects.count(),
            1,
        )
        self.assertEqual(
            ChannelMessage.objects.filter(
                provider="telegram",
                external_message_id="123456789:2",
            ).count(),
            1,
        )

    def test_creates_draft_when_information_is_missing(self):
        result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:3",
            text="La consola no funciona",
            raw_payload={
                "test": True,
            },
        )

        processing = result["processing"]

        self.assertEqual(
            processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            processing["incident_created"],
        )
        self.assertIn(
            "unit",
            processing["missing_fields"],
        )
        self.assertIn(
            "priority",
            processing["missing_fields"],
        )

        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        draft = IncidentDraft.objects.get()

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COLLECTING,
        )
        self.assertIn(
            "unit",
            draft.missing_fields,
        )
        self.assertIn(
            "priority",
            draft.missing_fields,
        )

    def test_completes_existing_draft_after_priority_confirmation(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:4",
            text="La consola no funciona",
            raw_payload={
                "test": True,
            },
        )

        draft_id = first_result["processing"]["draft_id"]

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:5",
            text="Unidad móvil 12",
            raw_payload={
                "test": True,
            },
        )

        second_processing = second_result["processing"]

        self.assertEqual(
            second_processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            second_processing["incident_created"],
        )
        self.assertEqual(
            second_processing["missing_fields"],
            ["priority"],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        third_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:6",
            text="Media",
            raw_payload={
                "test": True,
            },
        )

        third_processing = third_result["processing"]

        self.assertEqual(
            third_processing["action"],
            "incident_created",
        )
        self.assertTrue(
            third_processing["incident_created"],
        )
        self.assertEqual(
            Incident.objects.count(),
            1,
        )

        incident = Incident.objects.get()

        self.assertEqual(
            incident.unit,
            "Unidad movil 12",
        )
        self.assertEqual(
            incident.equipment,
            "Consola",
        )
        self.assertEqual(
            incident.failure_type,
            "No funciona",
        )
        self.assertEqual(
            incident.priority,
            Incident.Priority.MEDIUM,
        )

        draft = IncidentDraft.objects.get(
            pk=draft_id,
        )

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COMPLETED,
        )
        self.assertEqual(
            draft.incident,
            incident,
        )
        self.assertEqual(
            draft.missing_fields,
            [],
        )

    def test_urgent_message_does_not_assign_priority_automatically(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:urgent-1",
            text="La cámara de la unidad móvil 7 no enciende",
            raw_payload={
                "test": True,
            },
        )

        self.assertEqual(
            first_result["processing"]["action"],
            "ask_clarification",
        )

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:urgent-2",
            text="Es muy urgente",
            raw_payload={
                "test": True,
            },
        )

        processing = second_result["processing"]

        self.assertEqual(
            processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            processing["incident_created"],
        )
        self.assertEqual(
            processing["missing_fields"],
            ["priority"],
        )
        self.assertIsNone(
            processing["extracted_data"]["priority"],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )

class IncidentListViewTests(TestCase):

    def setUp(self):

        self.technician = Technician.objects.create(

            full_name="Técnico Dashboard",

            is_active=True,

        )



        self.high_incident = Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 20",

            equipment="Cámara principal",

            failure_type="No enciende",

            description=(

                "La cámara principal de la unidad móvil 20 "

                "no enciende."

            ),

            priority=Incident.Priority.HIGH,

            status=Incident.Status.REPORTED,

            original_message=(

                "La cámara principal de la unidad móvil 20 "

                "no enciende."

            ),

        )



        self.low_incident = Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 40",

            equipment="Cámara auxiliar",

            failure_type="Imagen intermitente",

            description=(

                "La cámara auxiliar de la unidad móvil 40 "

                "presenta una falla intermitente."

            ),

            priority=Incident.Priority.LOW,

            status=Incident.Status.REPORTED,

            original_message=(

                "La cámara auxiliar de la unidad móvil 40 "

                "presenta una falla intermitente."

            ),

        )



    def test_lists_incidents(self):

        response = self.client.get(

            "/api/v1/incidents/",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            2,

        )

        self.assertIsNone(

            data["next"],

        )

        self.assertIsNone(

            data["previous"],

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            2,

        )



        ids = {

            item["id"]

            for item in results

        }



        self.assertIn(

            self.high_incident.pk,

            ids,

        )

        self.assertIn(

            self.low_incident.pk,

            ids,

        )



    def test_filters_by_priority(self):

        response = self.client.get(

            "/api/v1/incidents/?priority=high",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            1,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            1,

        )

        self.assertEqual(

            results[0]["id"],

            self.high_incident.pk,

        )



    def test_filters_by_status(self):

        response = self.client.get(

            "/api/v1/incidents/?status=reported",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            2,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            2,

        )



    def test_filters_by_unit(self):

        response = self.client.get(

            "/api/v1/incidents/?unit=40",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            1,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            1,

        )

        self.assertEqual(

            results[0]["id"],

            self.low_incident.pk,

        )



    def test_filters_by_equipment(self):

        response = self.client.get(

            "/api/v1/incidents/?equipment=auxiliar",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            1,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            1,

        )

        self.assertEqual(

            results[0]["id"],

            self.low_incident.pk,

        )



    def test_combines_status_and_priority_filters(self):

        response = self.client.get(

            "/api/v1/incidents/"

            "?status=reported&priority=low",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            1,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            1,

        )

        self.assertEqual(

            results[0]["id"],

            self.low_incident.pk,

        )



    def test_searches_incidents_by_text(self):

        response = self.client.get(

            "/api/v1/incidents/?search=intermitente",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            1,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            1,

        )

        self.assertEqual(

            results[0]["id"],

            self.low_incident.pk,

        )

        self.assertEqual(

            results[0]["failure_type"],

            "Imagen intermitente",

        )



    def test_orders_incidents_by_created_at_ascending(self):

        response = self.client.get(

            "/api/v1/incidents/?ordering=created_at",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()

        results = data["results"]



        self.assertEqual(

            results[0]["id"],

            self.high_incident.pk,

        )

        self.assertEqual(

            results[1]["id"],

            self.low_incident.pk,

        )



    def test_orders_incidents_by_created_at_descending(self):

        response = self.client.get(

            "/api/v1/incidents/?ordering=-created_at",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()

        results = data["results"]



        self.assertEqual(

            results[0]["id"],

            self.low_incident.pk,

        )

        self.assertEqual(

            results[1]["id"],

            self.high_incident.pk,

        )





class IncidentDetailViewTests(TestCase):

    def setUp(self):

        self.technician = Technician.objects.create(

            full_name="Técnico Detalle",

            is_active=True,

        )



        self.incident = Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 50",

            equipment="Router",

            failure_type="Sin conexión",

            description=(

                "El router de la unidad móvil 50 "

                "no tiene conexión."

            ),

            priority=Incident.Priority.HIGH,

            status=Incident.Status.REPORTED,

            original_message=(

                "El router de la unidad móvil 50 "

                "no tiene conexión."

            ),

        )



    def test_retrieves_incident_detail(self):

        response = self.client.get(

            f"/api/v1/incidents/{self.incident.pk}/",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["id"],

            self.incident.pk,

        )

        self.assertEqual(

            data["code"],

            self.incident.code,

        )

        self.assertEqual(

            data["technician_name"],

            "Técnico Detalle",

        )

        self.assertEqual(

            data["unit"],

            "Unidad móvil 50",

        )

        self.assertEqual(

            data["equipment"],

            "Router",

        )

        self.assertEqual(

            data["failure_type"],

            "Sin conexión",

        )



    def test_returns_404_for_missing_incident(self):

        response = self.client.get(

            "/api/v1/incidents/999999/",

        )



        self.assertEqual(

            response.status_code,

            404,

        )



    def test_updates_incident_status(self):

        response = self.client.patch(

            f"/api/v1/incidents/{self.incident.pk}/",

            data={

                "status": Incident.Status.IN_PROGRESS,

            },

            content_type="application/json",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        self.incident.refresh_from_db()



        self.assertEqual(

            self.incident.status,

            Incident.Status.IN_PROGRESS,

        )

        self.assertIsNone(

            self.incident.resolved_at,

        )



        data = response.json()



        self.assertEqual(

            data["status"],

            Incident.Status.IN_PROGRESS,

        )

        self.assertEqual(

            data["status_display"],

            "En progreso",

        )



    def test_sets_resolved_at_when_incident_is_resolved(self):

        response = self.client.patch(

            f"/api/v1/incidents/{self.incident.pk}/",

            data={

                "status": Incident.Status.RESOLVED,

            },

            content_type="application/json",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        self.incident.refresh_from_db()



        self.assertEqual(

            self.incident.status,

            Incident.Status.RESOLVED,

        )

        self.assertIsNotNone(

            self.incident.resolved_at,

        )



        data = response.json()



        self.assertEqual(

            data["status"],

            Incident.Status.RESOLVED,

        )

        self.assertIsNotNone(

            data["resolved_at"],

        )



    def test_clears_resolved_at_when_leaving_resolved_status(self):

        self.incident.status = Incident.Status.RESOLVED

        self.incident.resolved_at = timezone.now()

        self.incident.save(

            update_fields=[

                "status",

                "resolved_at",

                "updated_at",

            ]

        )



        response = self.client.patch(

            f"/api/v1/incidents/{self.incident.pk}/",

            data={

                "status": Incident.Status.UNDER_REVIEW,

            },

            content_type="application/json",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        self.incident.refresh_from_db()



        self.assertEqual(

            self.incident.status,

            Incident.Status.UNDER_REVIEW,

        )

        self.assertIsNone(

            self.incident.resolved_at,

        )



    def test_rejects_invalid_incident_status(self):

        response = self.client.patch(

            f"/api/v1/incidents/{self.incident.pk}/",

            data={

                "status": "estado_inventado",

            },

            content_type="application/json",

        )



        self.assertEqual(

            response.status_code,

            400,

        )



        self.incident.refresh_from_db()



        self.assertEqual(

            self.incident.status,

            Incident.Status.REPORTED,

        )

        self.assertIsNone(

            self.incident.resolved_at,

        )





class IncidentSummaryViewTests(TestCase):

    def setUp(self):

        self.technician = Technician.objects.create(

            full_name="Técnico Resumen",

            is_active=True,

        )



        Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 1",

            equipment="Cámara",

            failure_type="No enciende",

            description="La cámara no enciende.",

            priority=Incident.Priority.HIGH,

            status=Incident.Status.REPORTED,

            original_message="La cámara no enciende.",

        )



        Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 2",

            equipment="Router",

            failure_type="Sin conexión",

            description="El router no tiene conexión.",

            priority=Incident.Priority.LOW,

            status=Incident.Status.UNDER_REVIEW,

            original_message="El router no tiene conexión.",

        )



        Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 3",

            equipment="Consola",

            failure_type="Sin audio",

            description="La consola no entrega audio.",

            priority=Incident.Priority.CRITICAL,

            status=Incident.Status.RESOLVED,

            original_message="La consola no entrega audio.",

            resolved_at=timezone.now(),

        )



        Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 4",

            equipment="Transmisor",

            failure_type="Falla intermitente",

            description="El transmisor presenta una falla.",

            priority=Incident.Priority.MEDIUM,

            status=Incident.Status.CLOSED,

            original_message="El transmisor presenta una falla.",

        )



    def test_returns_incident_summary(self):

        response = self.client.get(

            "/api/v1/incidents/summary/",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["total"],

            4,

        )



        self.assertEqual(

            data["by_status"]["reported"],

            1,

        )

        self.assertEqual(

            data["by_status"]["under_review"],

            1,

        )

        self.assertEqual(

            data["by_status"]["in_progress"],

            0,

        )

        self.assertEqual(

            data["by_status"]["resolved"],

            1,

        )

        self.assertEqual(

            data["by_status"]["closed"],

            1,

        )

        self.assertEqual(

            data["by_status"]["cancelled"],

            0,

        )



        self.assertEqual(

            data["by_priority"]["low"],

            1,

        )

        self.assertEqual(

            data["by_priority"]["medium"],

            1,

        )

        self.assertEqual(

            data["by_priority"]["high"],

            1,

        )

        self.assertEqual(

            data["by_priority"]["critical"],

            1,

        )