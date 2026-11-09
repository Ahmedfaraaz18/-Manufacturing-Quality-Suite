from django.urls import reverse
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from .models import Inspection, InspectionBatch, InspectionReview, Product


class ProductSerializer(serializers.ModelSerializer):
    product_id = serializers.RegexField(
        r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$', max_length=32,
        validators=[UniqueValidator(queryset=Product.objects.all(), message='A product with this ID already exists.')],
    )
    inspection_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Product
        fields = ['id', 'product_id', 'name', 'description', 'is_active', 'created_at', 'updated_at', 'inspection_count']
        read_only_fields = ['id', 'created_at', 'updated_at', 'inspection_count']

    def validate_product_id(self, value):
        if self.instance is not None and value != self.instance.product_id:
            raise serializers.ValidationError('The product ID cannot be changed.')
        return value


class ReviewSerializer(serializers.ModelSerializer):
    reviewer = serializers.CharField(source='reviewer.username', read_only=True)
    is_correction = serializers.BooleanField(read_only=True)

    class Meta:
        model = InspectionReview
        fields = ['id', 'original_predicted_class', 'reviewed_class', 'is_correction', 'reviewer', 'reviewed_at', 'comment']
        read_only_fields = fields


class ReviewInputSerializer(serializers.Serializer):
    """Only the decision and comment are accepted; reviewer and role always come from the session."""

    reviewed_class = serializers.ChoiceField(choices=Inspection.PredictedClass.values)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=1000, trim_whitespace=True)


class InspectionSerializer(serializers.ModelSerializer):
    product = serializers.CharField(source='product.product_id', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    submitting_user = serializers.CharField(source='submitting_user.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    image_url = serializers.SerializerMethodField()
    review = serializers.SerializerMethodField()
    is_reviewed = serializers.SerializerMethodField()

    class Meta:
        model = Inspection
        fields = [
            'id', 'batch', 'product', 'product_name', 'original_filename', 'submitting_user', 'status',
            'status_display', 'predicted_class', 'confidence', 'defect_probability', 'model_version',
            'failure_reason', 'image_url', 'image_format', 'image_width', 'image_height', 'file_size',
            'created_at', 'started_at', 'completed_at', 'is_reviewed', 'review',
        ]
        read_only_fields = fields

    def get_image_url(self, obj):
        return reverse('inspection-image', args=[obj.pk]) if obj.image else None

    def _review(self, obj):
        # reverse one-to-one raises when absent; select_related makes this free for list views
        try:
            return obj.review
        except InspectionReview.DoesNotExist:
            return None

    def get_review(self, obj):
        review = self._review(obj)
        return ReviewSerializer(review).data if review else None

    def get_is_reviewed(self, obj):
        return self._review(obj) is not None


class BatchSerializer(serializers.ModelSerializer):
    product = serializers.CharField(source='product.product_id', read_only=True)
    submitted_by = serializers.CharField(source='submitted_by.username', read_only=True)
    queued_count = serializers.IntegerField(read_only=True)
    processing_count = serializers.IntegerField(read_only=True)
    completed_count = serializers.IntegerField(read_only=True)
    failed_count = serializers.IntegerField(read_only=True)
    is_finished = serializers.SerializerMethodField()
    progress_percent = serializers.SerializerMethodField()
    inspections = serializers.SerializerMethodField()

    class Meta:
        model = InspectionBatch
        fields = [
            'id', 'product', 'submitted_by', 'total_files', 'queued_count', 'processing_count',
            'completed_count', 'failed_count', 'is_finished', 'progress_percent', 'created_at', 'updated_at',
            'inspections',
        ]
        read_only_fields = fields

    def get_is_finished(self, obj):
        return obj.queued_count + obj.processing_count == 0

    def get_progress_percent(self, obj):
        done = obj.completed_count + obj.failed_count
        return round(100 * done / obj.total_files) if obj.total_files else 100

    def get_inspections(self, obj):
        items = obj.inspections.select_related('product', 'submitting_user', 'review', 'review__reviewer').order_by('id')
        return InspectionSerializer(items, many=True, context=self.context).data


class UploadSerializer(serializers.Serializer):
    product = serializers.SlugRelatedField(slug_field='product_id', queryset=Product.objects.filter(is_active=True))
    client_token = serializers.CharField(required=False, allow_blank=True, max_length=64)


from .models import QualityIncident


class QualityIncidentSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(source='created_by.username', read_only=True)
    reviewed_by = serializers.CharField(source='reviewed_by.username', read_only=True)
    evidence_status_display = serializers.CharField(source='get_evidence_status_display', read_only=True)
    review_status_display = serializers.CharField(source='get_review_status_display', read_only=True)
    inspection_id = serializers.IntegerField(source='inspection.pk', read_only=True)
    inspection_url = serializers.SerializerMethodField()

    class Meta:
        model = QualityIncident
        fields = [
            'id', 'inspection_id', 'product_id', 'original_predicted_class', 'original_model_version',
            'confidence', 'defect_probability', 'summary', 'suggested_actions', 'source_references',
            'evidence_status', 'evidence_status_display', 'evidence_failure_reason', 'created_by',
            'created_at', 'updated_at', 'review_status', 'review_status_display', 'reviewed_by',
            'reviewed_at', 'supervisor_comment', 'inspection_url',
        ]
        read_only_fields = fields

    def get_inspection_url(self, obj):
        return reverse('inspection-detail', args=[obj.inspection_id])


class IncidentReviewInputSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=['approved', 'needs_info'])
    comment = serializers.CharField(required=True, min_length=1, max_length=1000, trim_whitespace=True)
