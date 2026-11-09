import logging

from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import services
from ..models import Inspection, Product
from ..permissions import IsAnyRole, IsEngineer, IsSupervisor, ReadOnlyOrAdministrator
from ..serializers import (
    BatchSerializer,
    InspectionSerializer,
    ProductSerializer,
    ReviewInputSerializer,
    UploadSerializer,
)

logger = logging.getLogger(__name__)

# 10 files of 5 MiB plus generous multipart overhead; larger bodies are refused before parsing.
MAX_REQUEST_BYTES = 10 * 5 * 1024 * 1024 + 2 * 1024 * 1024


class InspectionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class ProductListCreateView(generics.ListCreateAPIView):
    """Any role may list active products; only Administrators may register one."""

    permission_classes = [ReadOnlyOrAdministrator]
    serializer_class = ProductSerializer
    pagination_class = None

    def get_queryset(self):
        qs = Product.objects.annotate(inspection_count=Count('inspections'))
        return qs if self.request.user.is_administrator else qs.filter(is_active=True)


class ProductDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [ReadOnlyOrAdministrator]
    serializer_class = ProductSerializer
    lookup_field = 'product_id'

    def get_queryset(self):
        qs = Product.objects.annotate(inspection_count=Count('inspections'))
        return qs if self.request.user.is_administrator else qs.filter(is_active=True)


class _UploadBase(APIView):
    permission_classes = [IsEngineer]  # Engineers submit images; no other role may
    parser_classes = [MultiPartParser, FormParser]
    file_field = 'image'

    def post(self, request):
        length = request.META.get('CONTENT_LENGTH')
        if length and length.isdigit() and int(length) > MAX_REQUEST_BYTES:
            return Response({'detail': 'The upload is too large.'}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        serializer = UploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        files = request.FILES.getlist(self.file_field)
        try:
            result = services.create_batch(
                request.user, serializer.validated_data['product'], self.check_files(files),
                serializer.validated_data.get('client_token', ''),
            )
        except services.BatchSizeError as exc:
            return Response({self.file_field: [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        return self.respond(request, result)

    def check_files(self, files):
        return files

    @staticmethod
    def batch_data(request, batch):
        batch = services.visible_batches(request.user).get(pk=batch.pk)
        return BatchSerializer(batch, context={'request': request}).data


class SingleUploadView(_UploadBase):
    """POST /api/inspections/upload/ - one image (field ``image``)."""

    file_field = 'image'

    def check_files(self, files):
        if len(files) != 1:
            raise ValidationError({'image': ['Submit exactly one image in the "image" field.']})
        return files

    def respond(self, request, result):
        inspection = services.visible_inspections(request.user).get(pk=result.inspections[0].pk)
        body = {
            'inspection': InspectionSerializer(inspection, context={'request': request}).data,
            'batch_id': result.batch.pk, 'duplicate': result.duplicate,
        }
        if result.duplicate:
            return Response(body, status=status.HTTP_200_OK)
        code = status.HTTP_422_UNPROCESSABLE_ENTITY if inspection.status == Inspection.Status.FAILED else status.HTTP_202_ACCEPTED
        return Response(body, status=code)


class BatchUploadView(_UploadBase):
    """POST /api/inspections/batch/ - 1 to 10 images (repeated field ``files``)."""

    file_field = 'files'

    def respond(self, request, result):
        body = {'batch': self.batch_data(request, result.batch), 'duplicate': result.duplicate}
        if result.duplicate:
            return Response(body, status=status.HTTP_200_OK)
        queued = any(i.status == Inspection.Status.QUEUED for i in result.inspections)
        return Response(body, status=status.HTTP_202_ACCEPTED if queued else status.HTTP_422_UNPROCESSABLE_ENTITY)


class InspectionListView(generics.ListAPIView):
    """Engineers see only their own inspections; Supervisors and Administrators see all."""

    permission_classes = [IsAnyRole]
    serializer_class = InspectionSerializer
    pagination_class = InspectionPagination

    def get_queryset(self):
        return services.filter_inspections(services.visible_inspections(self.request.user), self.request.query_params)


class SupervisorInspectionListView(InspectionListView):
    permission_classes = [IsSupervisor]


class InspectionDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAnyRole]
    serializer_class = InspectionSerializer

    def get_queryset(self):
        return services.visible_inspections(self.request.user)


class BatchDetailView(APIView):
    permission_classes = [IsAnyRole]

    def get(self, request, pk):
        batch = get_object_or_404(services.visible_batches(request.user), pk=pk)
        return Response(BatchSerializer(batch, context={'request': request}).data)


class ReviewView(APIView):
    """POST /api/inspections/{id}/review/ - Supervisors only.

    The permission check runs before the inspection is even looked up, so an unauthorised caller
    learns nothing and cannot change either the prediction or a review.
    """

    permission_classes = [IsSupervisor]
    parser_classes = [JSONParser, FormParser]

    def post(self, request, pk):
        serializer = ReviewInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.submit_review(pk, request.user, **serializer.validated_data)
        except services.ReviewError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        inspection = services.visible_inspections(request.user).get(pk=pk)
        return Response(InspectionSerializer(inspection, context={'request': request}).data, status=status.HTTP_201_CREATED)


from ..serializers import QualityIncidentSerializer, IncidentReviewInputSerializer


class CreateIncidentAPIView(APIView):
    """POST /api/inspections/{id}/incident/ - Engineers only."""

    permission_classes = [IsEngineer]

    def post(self, request, pk):
        try:
            incident, created = services.create_quality_incident(pk, request.user)
        except services.EligibilityError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        except PermissionDenied as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        
        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(QualityIncidentSerializer(incident, context={'request': request}).data, status=status_code)


class IncidentListAPIView(generics.ListAPIView):
    """GET /api/incidents/ - Scoped by role (Engineers see own, Supervisors/Admins see all)."""

    permission_classes = [IsAnyRole]
    serializer_class = QualityIncidentSerializer

    def get_queryset(self):
        qs = services.visible_incidents(self.request.user)
        review_status = self.request.query_params.get('review_status')
        if review_status:
            qs = qs.filter(review_status=review_status)
        evidence_status = self.request.query_params.get('evidence_status')
        if evidence_status:
            qs = qs.filter(evidence_status=evidence_status)
        product_id = self.request.query_params.get('product')
        if product_id:
            qs = qs.filter(product_id=product_id)
        return qs


class IncidentDetailAPIView(APIView):
    """GET /api/incidents/{id}/ - Scoped by role."""

    permission_classes = [IsAnyRole]

    def get(self, request, pk):
        incident = get_object_or_404(services.visible_incidents(request.user), pk=pk)
        return Response(QualityIncidentSerializer(incident, context={'request': request}).data)


class IncidentReviewAPIView(APIView):
    """POST /api/incidents/{id}/review/ - Supervisors only. Administrator status alone is refused."""

    permission_classes = [IsSupervisor]
    parser_classes = [JSONParser, FormParser]

    def post(self, request, pk):
        serializer = IncidentReviewInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            incident = services.submit_incident_review(
                pk, request.user, serializer.validated_data['decision'], serializer.validated_data['comment']
            )
        except services.ReviewError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        except PermissionDenied as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        return Response(QualityIncidentSerializer(incident, context={'request': request}).data, status=status.HTTP_200_OK)
