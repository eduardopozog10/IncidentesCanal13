import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "incidents",
            "0004_incidentdraft_whatsappmessage_draft_and_more",
        ),
    ]

    operations = [
        migrations.AlterField(
            model_name="technician",
            name="whatsapp_number",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Campo temporal para conservar los datos existentes. "
                    "Las nuevas integraciones usarán canales de mensajería."
                ),
                max_length=20,
                null=True,
                unique=True,
                verbose_name="número de WhatsApp",
            ),
        ),
        migrations.RenameModel(
            old_name="WhatsAppMessage",
            new_name="ChannelMessage",
        ),
        migrations.RenameField(
            model_name="channelmessage",
            old_name="phone_number",
            new_name="external_sender_id",
        ),
        migrations.AlterField(
            model_name="channelmessage",
            name="external_sender_id",
            field=models.CharField(
                max_length=255,
                verbose_name=(
                    "identificador externo del remitente"
                ),
            ),
        ),
        migrations.AlterField(
            model_name="channelmessage",
            name="external_message_id",
            field=models.CharField(
                blank=True,
                max_length=255,
                null=True,
                verbose_name="identificador externo del mensaje",
            ),
        ),
        migrations.AddField(
            model_name="channelmessage",
            name="provider",
            field=models.CharField(
                choices=[
                    ("telegram", "Telegram"),
                    ("whatsapp", "WhatsApp"),
                    ("test", "Prueba"),
                ],
                default="whatsapp",
                max_length=20,
                verbose_name="proveedor",
            ),
        ),
        migrations.AddField(
            model_name="channelmessage",
            name="external_chat_id",
            field=models.CharField(
                blank=True,
                default="",
                max_length=255,
                verbose_name="identificador externo del chat",
            ),
            preserve_default=False,
        ),
        migrations.AlterModelOptions(
            name="channelmessage",
            options={
                "ordering": ["created_at"],
                "verbose_name": "mensaje de canal",
                "verbose_name_plural": "mensajes de canales",
            },
        ),
        migrations.AlterModelTable(
            name="channelmessage",
            table="channel_messages",
        ),
        migrations.CreateModel(
            name="TechnicianChannel",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("telegram", "Telegram"),
                            ("whatsapp", "WhatsApp"),
                        ],
                        max_length=20,
                        verbose_name="proveedor",
                    ),
                ),
                (
                    "external_user_id",
                    models.CharField(
                        max_length=255,
                        verbose_name=(
                            "identificador externo del usuario"
                        ),
                    ),
                ),
                (
                    "external_chat_id",
                    models.CharField(
                        blank=True,
                        max_length=255,
                        verbose_name=(
                            "identificador externo del chat"
                        ),
                    ),
                ),
                (
                    "username",
                    models.CharField(
                        blank=True,
                        max_length=150,
                        verbose_name="nombre de usuario externo",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        verbose_name="activo",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        verbose_name="fecha de creación",
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True,
                        verbose_name="última actualización",
                    ),
                ),
                (
                    "technician",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="channels",
                        to="incidents.technician",
                        verbose_name="técnico",
                    ),
                ),
            ],
            options={
                "verbose_name": "canal de técnico",
                "verbose_name_plural": "canales de técnicos",
                "db_table": "technician_channels",
                "ordering": [
                    "technician__full_name",
                    "provider",
                ],
            },
        ),
        migrations.AddField(
            model_name="channelmessage",
            name="channel_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="messages",
                to="incidents.technicianchannel",
                verbose_name="canal del técnico",
            ),
        ),
        migrations.AddConstraint(
            model_name="technicianchannel",
            constraint=models.UniqueConstraint(
                fields=(
                    "provider",
                    "external_user_id",
                ),
                name="unique_user_per_messaging_provider",
            ),
        ),
        migrations.AddConstraint(
            model_name="channelmessage",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    external_message_id__isnull=False,
                ),
                fields=(
                    "provider",
                    "external_message_id",
                ),
                name="unique_message_per_provider",
            ),
        ),
    ]