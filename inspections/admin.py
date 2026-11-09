from django.contrib import admin

from assistant.admin import RoleRestrictedAdminMixin, manufacturing_admin_site

from .models import DatasetSample, Inspection, InspectionBatch, InspectionReview, Product, ValidationAsset


class ReadOnlyAdminMixin(RoleRestrictedAdminMixin):
    """Inspection history is evidence: visible to administrators, never editable or deletable here."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Product, site=manufacturing_admin_site)
class ProductAdmin(RoleRestrictedAdminMixin, admin.ModelAdmin):
    list_display = ('product_id', 'name', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('product_id', 'name')

    def has_delete_permission(self, request, obj=None):
        return False  # deactivate instead; inspection records must keep their product


@admin.register(InspectionBatch, site=manufacturing_admin_site)
class InspectionBatchAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('id', 'product', 'submitted_by', 'total_files', 'created_at')
    list_filter = ('product',)


@admin.register(Inspection, site=manufacturing_admin_site)
class InspectionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('id', 'product', 'submitting_user', 'status', 'predicted_class', 'confidence', 'created_at')
    list_filter = ('status', 'predicted_class', 'product')
    search_fields = ('original_filename', 'submitting_user__username')


@admin.register(InspectionReview, site=manufacturing_admin_site)
class InspectionReviewAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('inspection', 'original_predicted_class', 'reviewed_class', 'reviewer', 'reviewed_at')
    list_filter = ('reviewed_class',)


@admin.register(DatasetSample, site=manufacturing_admin_site)
class DatasetSampleAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Inventory only: organizer labels must not be edited or fed into live inference."""

    list_display = ('image_id', 'product', 'filename', 'image_width', 'image_height', 'validation_status', 'imported_at')
    list_filter = ('product', 'validation_status')
    search_fields = ('image_id', 'filename', 'relative_path')


@admin.register(ValidationAsset, site=manufacturing_admin_site)
class ValidationAssetAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('filename', 'validation_status', 'validation_reason', 'imported_at')
    list_filter = ('validation_status',)
    search_fields = ('filename', 'relative_path')
