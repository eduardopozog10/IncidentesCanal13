from rest_framework import serializers

from incidents.models import Incident, IncidentChange, MANAGEMENT_NOTE_MAX_LENGTH, TechnicianChannel


class IncomingTextMessageSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(
        choices=TechnicianChannel.Provider.choices,
    )
    external_user_id = serializers.CharField(
        max_length=255,
        trim_whitespace=True,
    )
    external_chat_id = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    external_message_id = serializers.CharField(
        max_length=255,
        trim_whitespace=True,
    )
    text = serializers.CharField(
        max_length=4000,
        trim_whitespace=True,
    )

    def validate_external_user_id(self, value):
        cleaned_value = value.strip()

        if not cleaned_value:
            raise serializers.ValidationError(
                "El identificador del usuario no puede estar vacío."
            )

        return cleaned_value

    def validate_external_message_id(self, value):
        cleaned_value = value.strip()

        if not cleaned_value:
            raise serializers.ValidationError(
                "El identificador del mensaje no puede estar vacío."
            )

        return cleaned_value

    def validate_text(self, value):
        cleaned_text = value.strip()

        if not cleaned_text:
            raise serializers.ValidationError(
                "El mensaje no puede estar vacío."
            )

        return cleaned_text

    def validate(self, data):
        if not data.get("external_chat_id"):
            data["external_chat_id"] = data["external_user_id"]

        return data


class IncidentSerializer(serializers.ModelSerializer):
    code = serializers.CharField(
        read_only=True,
    )
    reporter_name = serializers.CharField(
        source="technician.full_name",
        read_only=True,
    )
    technician_name = serializers.CharField(
        source="technician.full_name",
        read_only=True,
    )
    asset_category_display = serializers.CharField(
        source="get_asset_category_display",
        read_only=True,
    )
    priority_display = serializers.CharField(
        source="get_priority_display",
        read_only=True,
    )
    status_display = serializers.CharField(
        source="get_status_display",
        read_only=True,
    )

    class Meta:
        model = Incident
        fields = (
            "id",
            "code",
            "reporter_name",
            "technician_name",
            "area",
            "location_context",
            "unit",
            "asset_category",
            "asset_category_display",
            "equipment",
            "failure_type",
            "description",
            "operational_impact",
            "priority",
            "priority_display",
            "status",
            "status_display",
            "original_message",
            "created_at",
            "updated_at",
            "resolved_at",
        )
        read_only_fields = fields


class IncidentStatusUpdateSerializer(serializers.Serializer):
    observation = serializers.CharField(max_length=MANAGEMENT_NOTE_MAX_LENGTH, required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=Incident.Status.choices,
    )

    def validate(self, attrs):
        if set(self.initial_data) - {"status", "observation"}:
            raise serializers.ValidationError("Esta operación solo permite actualizar el estado.")
        return attrs


class IncidentChangeSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    field_display = serializers.CharField(read_only=True)
    old_display = serializers.SerializerMethodField()
    new_display = serializers.SerializerMethodField()

    class Meta:
        model = IncidentChange
        fields = ("id", "incident_id", "created_at", "actor_label", "action", "action_display", "field",
                  "field_display", "old_value", "new_value", "old_display", "new_display",
                  "observation", "source", "source_display")
        read_only_fields = fields

    def get_old_display(self, obj):
        return obj.display_value(obj.old_value)

    def get_new_display(self, obj):
        return obj.display_value(obj.new_value)
