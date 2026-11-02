"""Presentación de consola, sin datos de conversación ni identificadores."""
import logging


class PilotDetailFilter(logging.Filter):
    def __init__(self, name='', mode='compact'):
        super().__init__(name)
        self.mode = mode

    def filter(self, record):
        if self.mode == 'detailed':
            return True
        # Éxitos/reintentos/recuperación se resumen una vez por grupo. Los
        # errores de Gemini ya omiten payload y credenciales.
        return record.levelno >= logging.WARNING and record.name != 'incidents.services.telegram_message_groups'


def completed(action, seconds, count, *, recovery=False):
    labels = {
        'ask_clarification':'Borrador revisado', 'draft_query':'Consulta respondida',
        'draft_help':'Orientación enviada', 'draft_cancelled':'Borrador cancelado',
        'incident_created':'Incidencia registrada', 'incident_already_created':'Incidencia ya registrada',
        'processing_pending':'Reintento pendiente', 'reply_failed':'Respuesta pendiente de entrega',
        'processing_failed':'Procesamiento sin completar',
        'group_superseded':'Respuesta reemplazada por información posterior',
        'draft_rewritten':'Descripción ordenada', 'welcome_sent':'Bienvenida enviada',
    }
    label = labels.get(action, 'Procesamiento completado')
    if recovery: label = 'Recuperación · ' + label.lower()
    logging.getLogger('secuoya.pilot').info('%s | %.2f s | %s mensaje(s)', label, seconds, count)
