from django.db import migrations, models


def migrate_existing_whatsapp_data(apps, schema_editor):
    Technician = apps.get_model(
        "incidents",
        "Technician",
    )
    TechnicianChannel = apps.get_model(
        "incidents",
        "TechnicianChannel",
    )
    ChannelMessage = apps.get_model(
        "incidents",
        "ChannelMessage",
    )

    ChannelMessage.objects.all().update(
        provider="whatsapp",
        external_chat_id=models.F("external_sender_id"),
    )

    technicians = (
        Technician.objects.exclude(whatsapp_number__isnull=True)
        .exclude(whatsapp_number="")
    )

    for technician in technicians:
        channel_account, _ = TechnicianChannel.objects.get_or_create(
            provider="whatsapp",
            external_user_id=technician.whatsapp_number,
            defaults={
                "technician_id": technician.pk,
                "external_chat_id": technician.whatsapp_number,
                "is_active": technician.is_active,
            },
        )

        ChannelMessage.objects.filter(
            technician_id=technician.pk,
        ).update(
            channel_account_id=channel_account.pk,
        )


class Migration(migrations.Migration):

    dependencies = [
        (
            "incidents",
            "0005_alter_technician_whatsapp_number_channelmessage_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(
            migrate_existing_whatsapp_data,
            migrations.RunPython.noop,
        ),
    ]