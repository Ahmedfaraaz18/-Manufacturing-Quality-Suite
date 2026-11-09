from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render

from ..models import AnswerSource, DocumentChunk, Machine, Manual, QuestionHistory, Role
from ..permissions import role_required
from .api import scoped_history

HOME_BY_ROLE = {
    Role.ADMINISTRATOR: 'admin-dashboard',
    Role.ENGINEER: 'engineer-dashboard',
    Role.SUPERVISOR: 'supervisor-dashboard',
}


@login_required
def home(request):
    """Landing page after sign-in: the Round 2 visual-inspection workflow for the user's role.

    The Round 1 manual-assistant dashboards remain at their original URLs (see HOME_BY_ROLE).
    """
    return redirect('inspections-home')


# ---- Administrator -------------------------------------------------------------------------------

@role_required(Role.ADMINISTRATOR)
def admin_dashboard(request):
    manuals = Manual.objects.select_related('machine').annotate(chunk_count=Count('chunks'))
    return render(request, 'assistant/admin_dashboard.html', {
        'manuals': manuals,
        'machine_count': Machine.objects.count(),
        'chunk_count': DocumentChunk.objects.count(),
        'question_count': QuestionHistory.objects.count(),
    })


@role_required(Role.ADMINISTRATOR)
def machine_registration(request):
    return render(request, 'assistant/machine_registration.html', {'machines': Machine.objects.all()})


@role_required(Role.ADMINISTRATOR)
def manual_upload(request):
    return render(request, 'assistant/manual_upload.html', {
        'machines': Machine.objects.filter(is_active=True), 'max_mb': settings.MAX_UPLOAD_SIZE_MB,
    })


# ---- Engineer ------------------------------------------------------------------------------------

@role_required(Role.ENGINEER)
def engineer_dashboard(request):
    machines = Machine.objects.filter(is_active=True).annotate(
        manual_count=Count('manuals', distinct=True)
    )
    recent = scoped_history(request.user, {})[:5]
    return render(request, 'assistant/engineer_dashboard.html', {'machines': machines, 'recent': recent})


@role_required(Role.ENGINEER)
def assistant_chat(request, machine_id):
    machine = get_object_or_404(Machine, machine_id=machine_id, is_active=True)
    manuals = machine.manuals.filter(status=Manual.Status.INDEXED)
    return render(request, 'assistant/assistant.html', {'machine': machine, 'manuals': manuals})


@role_required(Role.ENGINEER)
def engineer_history(request):
    qs = scoped_history(request.user, request.GET)
    page = Paginator(qs, 15).get_page(request.GET.get('page'))
    return render(request, 'assistant/history.html', {
        'page': page, 'machines': Machine.objects.all(), 'statuses': QuestionHistory.Status.choices,
        'filters': request.GET, 'title': 'My question history', 'show_user': False,
    })


# ---- Supervisor ----------------------------------------------------------------------------------

@role_required(Role.SUPERVISOR)
def supervisor_dashboard(request):
    qs = scoped_history(request.user, request.GET)
    page = Paginator(qs, 20).get_page(request.GET.get('page'))
    return render(request, 'assistant/history.html', {
        'page': page, 'machines': Machine.objects.all(), 'statuses': QuestionHistory.Status.choices,
        'filters': request.GET, 'title': 'Question history (read-only)', 'show_user': True,
    })


# ---- Manuals (any signed-in role) -------------------------------------------------------------------

@login_required
def manual_file(request, pk):
    manual = get_object_or_404(Manual, pk=pk)
    try:
        handle = manual.file.open('rb')
    except FileNotFoundError:
        raise Http404('Manual file is missing') from None
    response = FileResponse(handle, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{manual.document_id}_rev{manual.revision}.pdf"'
    return response


@login_required
def manual_page(request, pk, page):
    manual = get_object_or_404(Manual.objects.select_related('machine'), pk=pk)
    if not 1 <= page <= max(manual.page_count, 1):
        raise Http404('No such page')

    cited = None
    source_id = request.GET.get('source', '')
    if source_id.isdigit():
        sources = AnswerSource.objects.select_related('chunk', 'question', 'manual').filter(
            pk=int(source_id), manual=manual, page_number=page
        )
        if request.user.is_engineer:  # engineers may only see passages from their own questions
            sources = sources.filter(question__user=request.user)
        cited = sources.first()
        if cited is not None and not cited.is_verified:
            cited = None

    page_chunks = manual.chunks.filter(original_page_number=page)
    return render(request, 'assistant/manual_page.html', {
        'manual': manual, 'page': page, 'cited': cited, 'page_chunks': page_chunks,
        'prev_page': page - 1 if page > 1 else None,
        'next_page': page + 1 if page < manual.page_count else None,
    })
