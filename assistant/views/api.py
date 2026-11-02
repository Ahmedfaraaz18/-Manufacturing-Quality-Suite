import logging

from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Machine, Manual, QuestionHistory
from ..permissions import IsAdministrator, IsEngineer, IsEngineerOrSupervisor
from ..serializers import (
    AskSerializer,
    MachineSerializer,
    ManualSerializer,
    ManualUploadSerializer,
    QuestionHistorySerializer,
)
from ..services import rag
from ..services.ingestion import dispatch_indexing
from rest_framework.permissions import IsAuthenticated

logger = logging.getLogger(__name__)


class MachineListCreateView(generics.ListCreateAPIView):
    """Any signed-in user may list active machines; only administrators may register one."""

    serializer_class = MachineSerializer
    pagination_class = None

    def get_permissions(self):
        return [IsAdministrator()] if self.request.method == 'POST' else [IsAuthenticated()]

    def get_queryset(self):
        qs = Machine.objects.all()
        return qs if self.request.user.is_administrator else qs.filter(is_active=True)


class ManualListCreateView(APIView):
    permission_classes = [IsAdministrator]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        manuals = Manual.objects.select_related('machine').annotate(chunk_count=Count('chunks'))
        return Response(ManualSerializer(manuals, many=True).data)

    def post(self, request):
        serializer = ManualUploadSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        manual = serializer.save()
        return Response(ManualSerializer(manual).data, status=status.HTTP_201_CREATED)


class ManualReindexView(APIView):
    permission_classes = [IsAdministrator]

    def post(self, request, pk):
        manual = get_object_or_404(Manual, pk=pk)
        dispatch_indexing(manual)
        manual.refresh_from_db()
        data = ManualSerializer(manual).data
        if manual.status == Manual.Status.FAILED:
            return Response(data, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        return Response(data)


class AskView(APIView):
    """Engineers only. Supervisors are read-only and administrators manage content."""

    permission_classes = [IsEngineer]
    parser_classes = [JSONParser, FormParser]

    def post(self, request):
        serializer = AskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record = rag.answer_question(
            request.user, serializer.validated_data['machine'], serializer.validated_data['question']
        )
        record = QuestionHistory.objects.select_related('user', 'machine').prefetch_related('sources__chunk').get(pk=record.pk)
        return Response(QuestionHistorySerializer(record).data, status=status.HTTP_201_CREATED)


def scoped_history(user, params):
    """Engineers see only their own history; supervisors see everyone's (read-only)."""
    qs = QuestionHistory.objects.select_related('user', 'machine').prefetch_related('sources__chunk', 'sources__manual')
    if user.is_engineer:
        qs = qs.filter(user=user)
    if params.get('machine'):
        qs = qs.filter(machine__machine_id=params['machine'])
    if params.get('status') in QuestionHistory.Status.values:
        qs = qs.filter(status=params['status'])
    if params.get('unanswered') in ('1', 'true'):
        qs = qs.exclude(status=QuestionHistory.Status.ANSWERED)
    return qs


class HistoryListView(generics.ListAPIView):
    permission_classes = [IsEngineerOrSupervisor]
    serializer_class = QuestionHistorySerializer

    def get_queryset(self):
        return scoped_history(self.request.user, self.request.query_params)


class HistoryDetailView(generics.RetrieveAPIView):
    permission_classes = [IsEngineerOrSupervisor]
    serializer_class = QuestionHistorySerializer

    def get_queryset(self):
        return scoped_history(self.request.user, {})
