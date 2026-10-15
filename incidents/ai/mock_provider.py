import re
import unicodedata

from .base import AIProvider
from .schemas import IncidentExtractionResult


class MockAIProvider(AIProvider):
    EQUIPMENT_NAMES = (
        "cámara",
        "transmisor",
        "router",
        "antena",
        "monitor",
        "micrófono",
        "consola",
        "decodificador",
    )

    AREA_NAMES = {
        "produccion": "Producción",
        "audio": "Audio",
        "redes": "Redes",
        "sistemas": "Sistemas",
        "transmision": "Transmisión",
        "digital": "Digital",
        "infraestructura": "Infraestructura",
        "tecnologia": "Tecnología",
        "operaciones": "Operaciones",
    }  

    ASSET_CATEGORY_BY_EQUIPMENT = {
        "camara": "video_cameras",
        "monitor": "video_cameras",
        "microfono": "audio",
        "consola": "audio",
        "transmisor": "signal_transmission",
        "antena": "signal_transmission",
        "decodificador": "signal_transmission",
        "router": "networks",
    }

    FAILURE_PATTERNS = (
        "no enciende",
        "sin señal",
        "se apaga",
        "no funciona",
        "imagen congelada",
        "audio cortado",
        "sobrecalentamiento",
    )

    OPERATIONAL_IMPACT_EXPRESSIONS = {
        "detiene la produccion": "Detiene la producción",
        "afecta la señal": "Afecta la señal",
        "obliga a usar un respaldo": "Obliga a usar un respaldo",
        "permite continuar operando": "Permite continuar operando",
        "sin impacto operacional": "Sin impacto operacional",
    }

    def extract_incident(
        self,
        message: str,
        previous_data: dict | None = None,
    ) -> IncidentExtractionResult:
        previous_data = previous_data or {}
        normalized_message = self._normalize_text(message)

        location_context = (
            previous_data.get("location_context")
            or previous_data.get("unit")
            or self._extract_location_context(message)
        )

        unit = (
            previous_data.get("unit")
            or location_context
        )

        area = (
            previous_data.get("area")
            or self._extract_area(normalized_message)
        )

        current_equipment = self._extract_equipment(
            normalized_message
        )

        equipment = (
            previous_data.get("equipment")
            or current_equipment
        )

        asset_category = (
            previous_data.get("asset_category")
            or self._extract_asset_category(equipment)
        )

        current_failure_type = self._extract_failure_type(
            normalized_message
        )

        failure_type = (
            previous_data.get("failure_type")
            or current_failure_type
        )

        description = (
            previous_data.get("description")
            or self._extract_description(
                message=message,
                current_equipment=current_equipment,
                current_failure_type=current_failure_type,
                previous_equipment=previous_data.get("equipment"),
            )
        )

        operational_impact = (
            previous_data.get("operational_impact")
            or self._extract_operational_impact(
                normalized_message
            )
        )
        priority = (
            previous_data.get("priority")
            or self._extract_priority(normalized_message)
        )

        extracted_data = {
            "unit": unit,
            "area": area,
            "location_context": location_context,
            "asset_category": asset_category,
            "equipment": equipment,
            "failure_type": failure_type,
            "description": description,
            "operational_impact": operational_impact,
            "priority": priority,
        }

        required_fields = (
            "area",
            "location_context",
            "equipment",
            "failure_type",
            "description",
            "operational_impact",
            "priority",
        )

        missing_fields = [
            field_name
            for field_name in required_fields
            if not extracted_data.get(field_name)
        ]

        clarification_question = self._build_question(
            missing_fields
        )

        return IncidentExtractionResult(
            unit=unit,
            area=area,
            location_context=location_context,
            asset_category=asset_category,
            equipment=equipment,
            failure_type=failure_type,
            description=description,
            operational_impact=operational_impact,
            priority=priority,
            missing_fields=missing_fields,
            clarification_question=clarification_question,
            raw_response={
                "provider": "mock",
                "message": message,
            },
        )

    def _normalize_text(self, text: str):
        normalized_text = unicodedata.normalize(
            "NFD",
            text.lower(),
        )

        return "".join(
            character
            for character in normalized_text
            if unicodedata.category(character) != "Mn"
        )

    def _extract_area(self, normalized_message: str):
        area_match = re.search(
            r"\barea(?:\s+de)?\s+"
            r"(produccion|audio|redes|sistemas|transmision|"
            r"digital|infraestructura|tecnologia|operaciones)\b",
            normalized_message,
        )

        if area_match is None:
            return None

        area_name = area_match.group(1)

        return self.AREA_NAMES.get(area_name)

    def _extract_location_context(self, message: str):
        normalized_message = self._normalize_text(message)

        patterns = (
            r"\bunidad(?:\s+movil)?\s+[a-zA-Z0-9-]+",
            r"\bmovil\s+[a-zA-Z0-9-]+",
            r"\bestudio\s+[a-zA-Z0-9-]+",
            r"\bsala\s+[a-zA-Z0-9-]+",
            r"\bcontrol central\b",
            r"\bplataforma digital\b",
        )

        for pattern in patterns:
            match = re.search(
                pattern,
                normalized_message,
                flags=re.IGNORECASE,
            )

            if match is not None:
                return match.group(0).strip().capitalize()

        return None

    def _extract_equipment(self, normalized_message: str):
        for equipment_name in self.EQUIPMENT_NAMES:
            normalized_equipment = self._normalize_text(
                equipment_name
            )

            specific_patterns = (
                (
                    rf"\b{re.escape(normalized_equipment)}\s+"
                    r"([0-9]+(?:-[a-zA-Z0-9]+)?)\b"
                ),
                (
                    rf"\b{re.escape(normalized_equipment)}\s+"
                    r"(principal|secundario|secundaria|respaldo)\b"
                ),
            )

            for pattern in specific_patterns:
                match = re.search(
                    pattern,
                    normalized_message,
                    flags=re.IGNORECASE,
                )

                if match is not None:
                    identifier = match.group(1)

                    return (
                        f"{equipment_name.capitalize()} "
                        f"{identifier}"
                    )

            if normalized_equipment == "consola":
                console_match = re.search(
                    r"\bconsola\s+de\s+audio\b",
                    normalized_message,
                    flags=re.IGNORECASE,
                )

                if console_match is not None:
                    return "Consola de audio"

            if re.search(
                rf"\b{re.escape(normalized_equipment)}\b",
                normalized_message,
                flags=re.IGNORECASE,
            ):
                return equipment_name.capitalize()

        return None
    
    def _extract_asset_category(
        self,
        equipment: str | None,
    ):
        if not equipment:
            return None

        normalized_equipment = self._normalize_text(
            equipment
        )

        for (
            equipment_name,
            asset_category,
        ) in self.ASSET_CATEGORY_BY_EQUIPMENT.items():
            if (
                normalized_equipment == equipment_name
                or normalized_equipment.startswith(
                    f"{equipment_name} "
                )
            ):
                return asset_category

        return None
    def _extract_failure_type(self, normalized_message: str):
        for failure_pattern in self.FAILURE_PATTERNS:
            normalized_failure = self._normalize_text(
                failure_pattern
            )

            if normalized_failure in normalized_message:
                return failure_pattern.capitalize()

        return None

    def _extract_operational_impact(
        self,
        normalized_message: str,
    ):
        for (
            expression,
            operational_impact,
        ) in self.OPERATIONAL_IMPACT_EXPRESSIONS.items():
            if expression in normalized_message:
                return operational_impact

        return None

    def _extract_description(
        self,
        message: str,
        current_equipment: str | None,
        current_failure_type: str | None,
        previous_equipment: str | None,
    ):
        if current_failure_type is None:
            return None

        if current_equipment is None and previous_equipment is None:
            return None

        clean_message = message.strip()

        if not clean_message:
            return None

        return clean_message

    def _extract_priority(self, normalized_message: str):
        normalized_message = " ".join(
            normalized_message.strip().split()
        )

        priorities = {
            "low": (
                "baja",
                "low",
                "prioridad baja",
                "prioridad low",
                "la prioridad es baja",
                "la prioridad es low",
            ),
            "medium": (
                "media",
                "medium",
                "prioridad media",
                "prioridad medium",
                "la prioridad es media",
                "la prioridad es medium",
            ),
            "high": (
                "alta",
                "high",
                "prioridad alta",
                "prioridad high",
                "la prioridad es alta",
                "la prioridad es high",
            ),
            "critical": (
                "critica",
                "critical",
                "prioridad critica",
                "prioridad critical",
                "la prioridad es critica",
                "la prioridad es critical",
            ),
        }

        for priority, expressions in priorities.items():
            if normalized_message in expressions:
                return priority

        explicit_priority_match = re.search(
            r"\bprioridad\s+(baja|media|alta|critica|"
            r"low|medium|high|critical)\b",
            normalized_message,
        )

        if explicit_priority_match is None:
            return None

        priority_value = explicit_priority_match.group(1)

        priority_mapping = {
            "baja": "low",
            "low": "low",
            "media": "medium",
            "medium": "medium",
            "alta": "high",
            "high": "high",
            "critica": "critical",
            "critical": "critical",
        }

        return priority_mapping.get(priority_value)

    def _build_question(
        self,
        missing_fields: list[str],
    ):
        if not missing_fields:
            return None

        questions = {
            "area": (
                "¿A qué área corresponde el incidente? Indica el área "
                "operacional o técnica asociada, por ejemplo: Producción, "
                "Audio, Redes o Sistemas."
            ),
            "location_context": (
                "¿Dónde está ocurriendo la falla? Indica la ubicación "
                "o contexto operacional, por ejemplo: Estudio 2, "
                "Control Central, Móvil 7 o plataforma digital."
            ),
            "equipment": (
                "¿Qué equipo presenta la falla?"
            ),
            "failure_type": (
                "¿Qué tipo de falla presenta el equipo?"
            ),
            "description": (
                "¿Puedes describir con más detalle lo ocurrido?"
            ),
            "operational_impact": (
                "¿Qué impacto operacional está generando la falla? "
                "Por ejemplo: detiene una producción, afecta la señal, "
                "obliga a usar un respaldo o permite continuar operando."
            ),
            "priority": (
                "Para completar el reporte, ¿qué prioridad le asignas: "
                "Baja, Media, Alta o Crítica?"
            ),
        }

        return questions[missing_fields[0]]