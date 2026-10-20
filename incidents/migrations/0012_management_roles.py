from django.db import migrations


def initialize_roles(apps, schema_editor):
    alias = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    content_type, _ = ContentType.objects.using(alias).get_or_create(app_label="incidents", model="incident")
    names = dict(PERMISSIONS, view_incident="Puede consultar incidentes", change_incident="Puede cambiar incidentes")
    permissions = {code: Permission.objects.using(alias).get_or_create(content_type=content_type, codename=code,
        defaults={"name": name})[0] for code, name in names.items()}
    common = {"access_dashboard", "view_incident"}
    roles = {
        "Administrador": set(names),
        "Supervisor": common | {"view_incident_analytics", "change_incident_status", "change_incident_priority", "change_incident"},
        "Operador": common | {"change_incident_status"},
        "Visualizador": common | {"view_incident_analytics"},
    }
    for name, codes in roles.items():
        group, created = Group.objects.using(alias).get_or_create(name=f"Secuoya - {name}")
        actual = {f"{permission.content_type.app_label}.{permission.codename}" for permission in group.permissions.select_related("content_type")}
        if not created and group.user_set.exists() and actual != {f"incidents.{code}" for code in codes}:
            raise RuntimeError("Existe un grupo Secuoya con usuarios y permisos distintos. Revisar antes de migrar; no se modificaron cuentas.")
        group.permissions.set([permissions[code] for code in codes])
    # No se modifica ninguna cuenta ni se vinculan reportantes de Telegram.


PERMISSIONS = [
    ("access_dashboard", "Acceder al centro de control"),
    ("view_incident_analytics", "Consultar analítica de incidencias"),
    ("change_incident_status", "Cambiar estados de incidencias"),
    ("change_incident_priority", "Cambiar prioridades de incidencias"),
    ("manage_dashboard_users", "Administrar usuarios y perfiles del dashboard"),
]


class Migration(migrations.Migration):
    dependencies = [("incidents", "0011_pilot_recovery"), ("auth", "0012_alter_user_first_name_max_length")]
    operations = [
        migrations.AlterModelOptions(name="incident", options={"ordering": ["-created_at"],
            "verbose_name": "incidente", "verbose_name_plural": "incidentes", "permissions": PERMISSIONS}),
        migrations.RunPython(initialize_roles, migrations.RunPython.noop),
    ]
