from django.views import generic
from api.models import Dataset
from django.shortcuts import redirect
from django.views.generic.base import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator


class DatasetListView(LoginRequiredMixin, generic.ListView):
    template_name = "dataset_list.html"
    model = Dataset
    login_url = '/accounts/login/'

    def get_queryset(self):
        """Filter datasets to only show those belonging to the authenticated user"""
        return Dataset.objects.filter(user=self.request.user)


class DatasetDetailView(LoginRequiredMixin, generic.DetailView):
    template_name = "chat.html"
    model = Dataset
    login_url = '/accounts/login/'

    def get_queryset(self):
        """Filter datasets to only show those belonging to the authenticated user"""
        return Dataset.objects.filter(user=self.request.user)


class Chat(LoginRequiredMixin, TemplateView):
    template_name = "chat.html"
    login_url = '/accounts/login/'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['user_orcid'] = self.request.user.orcid_id
        return context
