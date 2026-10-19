from django.views.generic import TemplateView
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from incidents.models import Incident


@method_decorator(never_cache, name="dispatch")
@method_decorator(staff_member_required, name="dispatch")
class DashboardView(TemplateView):
    template_name = "incidents/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if "pk" in self.kwargs:
            context["incident_id"] = get_object_or_404(Incident, pk=self.kwargs["pk"]).pk
        return context
