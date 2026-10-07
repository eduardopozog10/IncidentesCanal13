from incidents.models import ChannelMessage, TechnicianChannel
from incidents.services.incident_processor import IncidentProcessor


class ChannelNotAuthorizedError(Exception):
    """El usuario no está asociado a un técnico activo."""


class IncomingMessageService:
    def __init__(self, incident_processor=None):
        self.incident_processor = (
            incident_processor
            or IncidentProcessor()
        )

    def process_text(
        self,
        *,
        provider: str,
        external_user_id: str,
        external_chat_id: str,
        external_message_id: str,
        text: str,
        raw_payload: dict,
    ) -> dict:
        channel_account = (
            TechnicianChannel.objects.select_related(
                "technician"
            )
            .filter(
                provider=provider,
                external_user_id=external_user_id,
                is_active=True,
                technician__is_active=True,
            )
            .first()
        )

        if channel_account is None:
            raise ChannelNotAuthorizedError(
                "No existe un técnico activo asociado "
                "a este usuario y proveedor."
            )

        existing_message = ChannelMessage.objects.filter(
            provider=provider,
            external_message_id=external_message_id,
        ).first()

        if existing_message is not None:
            return {
                "duplicate": True,
                "message": existing_message,
                "processing": None,
            }

        message = ChannelMessage.objects.create(
            provider=provider,
            external_message_id=external_message_id,
            external_sender_id=external_user_id,
            external_chat_id=external_chat_id,
            channel_account=channel_account,
            technician=channel_account.technician,
            direction=ChannelMessage.Direction.INBOUND,
            message_type=ChannelMessage.MessageType.TEXT,
            content=text,
            raw_payload=raw_payload,
            status=ChannelMessage.Status.RECEIVED,
        )

        try:
            processing_result = (
                self.incident_processor.process(message)
            )
        except Exception as error:
            message.status = ChannelMessage.Status.FAILED
            message.error_message = str(error)
            message.save(
                update_fields=[
                    "status",
                    "error_message",
                ]
            )
            raise

        return {
            "duplicate": False,
            "message": message,
            "processing": processing_result,
        }