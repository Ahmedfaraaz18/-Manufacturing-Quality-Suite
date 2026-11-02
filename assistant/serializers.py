from django.conf import settings
from django.urls import reverse
from rest_framework import serializers

from .models import AnswerSource, Machine, Manual, QuestionHistory
from .services.ingestion import IngestionError, register_manual


class MachineSerializer(serializers.ModelSerializer):
    machine_id = serializers.RegexField(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$', max_length=32)

    class Meta:
        model = Machine
        fields = ['id', 'machine_id', 'model_name', 'name', 'is_active']
        read_only_fields = ['id']


class ManualSerializer(serializers.ModelSerializer):
    machine = serializers.SlugRelatedField(slug_field='machine_id', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    viewer_url = serializers.SerializerMethodField()
    chunk_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Manual
        fields = [
            'id', 'machine', 'document_id', 'document_name', 'revision', 'original_filename',
            'page_count', 'status', 'status_display', 'error_message', 'uploaded_at', 'indexed_at',
            'viewer_url', 'chunk_count',
        ]
        read_only_fields = fields

    def get_viewer_url(self, obj):
        return reverse('manual-page', args=[obj.pk, 1])


class ManualUploadSerializer(serializers.Serializer):
    machine = serializers.SlugRelatedField(slug_field='machine_id', queryset=Machine.objects.filter(is_active=True))
    document_id = serializers.CharField(max_length=64)
    document_name = serializers.CharField(max_length=255)
    revision = serializers.CharField(max_length=32)
    file = serializers.FileField()

    def create(self, validated_data):
        try:
            return register_manual(user=self.context['request'].user, uploaded_file=validated_data.pop('file'),
                                   **validated_data)
        except IngestionError as exc:
            raise serializers.ValidationError({'file': [str(exc)]}) from exc


class AskSerializer(serializers.Serializer):
    machine = serializers.SlugRelatedField(slug_field='machine_id', queryset=Machine.objects.filter(is_active=True))
    question = serializers.CharField(max_length=settings.MAX_QUESTION_LENGTH, trim_whitespace=True)


class AnswerSourceSerializer(serializers.ModelSerializer):
    source_url = serializers.SerializerMethodField()

    class Meta:
        model = AnswerSource
        fields = ['id', 'document_id', 'document_name', 'revision', 'page_number', 'passage', 'source_url']

    def get_source_url(self, obj):
        if obj.manual_id is None:
            return None
        return reverse('manual-page', args=[obj.manual_id, obj.page_number]) + f'?source={obj.pk}'


class QuestionHistorySerializer(serializers.ModelSerializer):
    machine = serializers.SlugRelatedField(slug_field='machine_id', read_only=True)
    machine_name = serializers.CharField(source='machine.model_name', read_only=True)
    user = serializers.CharField(source='user.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    answer = serializers.CharField(source='display_answer', read_only=True)
    sources = AnswerSourceSerializer(source='verified_sources', many=True, read_only=True)

    class Meta:
        model = QuestionHistory
        fields = ['id', 'user', 'machine', 'machine_name', 'question', 'answer', 'status', 'status_display',
                  'created_at', 'sources']
        read_only_fields = fields
