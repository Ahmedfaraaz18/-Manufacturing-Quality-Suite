from django.contrib.auth import views as auth_views
from django.urls import path

from .views import api, pages

urlpatterns = [
    path('', pages.home, name='home'),
    path('login/', auth_views.LoginView.as_view(template_name='assistant/login.html',
                                                redirect_authenticated_user=True), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),

    # Administrator pages
    path('manage/', pages.admin_dashboard, name='admin-dashboard'),
    path('manage/machines/', pages.machine_registration, name='machine-registration'),
    path('manage/manuals/upload/', pages.manual_upload, name='manual-upload'),

    # Engineer pages
    path('engineer/', pages.engineer_dashboard, name='engineer-dashboard'),
    path('engineer/assistant/<str:machine_id>/', pages.assistant_chat, name='assistant'),
    path('engineer/history/', pages.engineer_history, name='engineer-history'),

    # Supervisor page
    path('supervisor/', pages.supervisor_dashboard, name='supervisor-dashboard'),

    # Manual viewer / PDF (authenticated)
    path('manuals/<int:pk>/file/', pages.manual_file, name='manual-file'),
    path('manuals/<int:pk>/page/<int:page>/', pages.manual_page, name='manual-page'),

    # REST API
    path('api/machines/', api.MachineListCreateView.as_view(), name='api-machines'),
    path('api/manuals/', api.ManualListCreateView.as_view(), name='api-manuals'),
    path('api/manuals/<int:pk>/reindex/', api.ManualReindexView.as_view(), name='api-manual-reindex'),
    path('api/ask/', api.AskView.as_view(), name='api-ask'),
    path('api/history/', api.HistoryListView.as_view(), name='api-history'),
    path('api/history/<int:pk>/', api.HistoryDetailView.as_view(), name='api-history-detail'),
]
