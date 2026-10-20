from django.views.generic import TemplateView
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from incidents.models import Incident
from incidents.access import can, require_management


@method_decorator(never_cache, name="dispatch")
@method_decorator(require_management(), name="dispatch")
class DashboardView(TemplateView):
    template_name = "incidents/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.GET.get("view") == "analytics" and not can(self.request.user, "view_incident_analytics"):
            raise PermissionDenied
        context["on_dashboard"] = True
        if "pk" in self.kwargs:
            context["incident_id"] = get_object_or_404(Incident, pk=self.kwargs["pk"]).pk
        return context
