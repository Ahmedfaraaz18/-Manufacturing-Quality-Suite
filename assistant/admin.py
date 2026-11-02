from functools import wraps

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied

from .models import AnswerSource, DocumentChunk, Machine, Manual, QuestionHistory, User


class ManufacturingAdminSite(admin.AdminSite):
    """Django Admin is reserved for users approved as application Administrators.

    ``is_staff`` is Django's admission flag and model permissions still control each
    operation.  The role check closes the otherwise separate Django Admin boundary,
    so a staff Engineer or Supervisor cannot gain access through accidental model
    permission assignment (or an incorrectly assigned superuser flag).
    """

    def has_permission(self, request):
        user = request.user
        return bool(user.is_active and user.is_staff and user.is_administrator)

    def admin_view(self, view, cacheable=False):
        wrapped_view = super().admin_view(view, cacheable)

        @wraps(wrapped_view)
        def inner(request, *args, **kwargs):
            user = request.user
            # Django's default response for a failed admin admission check is a
            # login redirect.  A logged-in staff user in the wrong role should get
            # an explicit denial instead, which also makes this boundary testable.
            if user.is_authenticated and user.is_active and user.is_staff and not user.is_administrator:
                raise PermissionDenied('Django Admin requires the Administrator role.')
            return wrapped_view(request, *args, **kwargs)

        return inner


manufacturing_admin_site = ManufacturingAdminSite(name='admin')


class RoleRestrictedAdminMixin:
    """Apply the application role boundary in addition to Django permissions."""

    def has_module_permission(self, request):
        return request.user.is_administrator and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return request.user.is_administrator and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return request.user.is_administrator and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return request.user.is_administrator and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_administrator and super().has_delete_permission(request, obj)


@admin.register(User, site=manufacturing_admin_site)
class AppUserAdmin(RoleRestrictedAdminMixin, UserAdmin):
    """Only system Administrators may alter roles or Django privilege flags."""

    list_display = ('username', 'email', 'role', 'is_active', 'is_staff')
    list_filter = ('role', 'is_active', 'is_staff')
    fieldsets = UserAdmin.fieldsets + (('Role', {'fields': ('role',)}),)
    add_fieldsets = UserAdmin.add_fieldsets + (('Role', {'fields': ('role',)}),)

    def has_module_permission(self, request):
        return request.user.is_superuser and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_delete_permission(request, obj)


@admin.register(Machine, site=manufacturing_admin_site)
class MachineAdmin(RoleRestrictedAdminMixin, admin.ModelAdmin):
    list_display = ('machine_id', 'model_name', 'name', 'is_active')


@admin.register(Manual, site=manufacturing_admin_site)
class ManualAdmin(RoleRestrictedAdminMixin, admin.ModelAdmin):
    """Manuals are created through the validated upload-and-indexing workflow."""

    list_display = ('document_id', 'revision', 'machine', 'status', 'page_count')
    list_filter = ('machine', 'status')
    readonly_fields = ('sha256', 'page_count', 'indexed_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(DocumentChunk, site=manufacturing_admin_site)
class DocumentChunkAdmin(RoleRestrictedAdminMixin, admin.ModelAdmin):
    list_display = ('document_id', 'original_page_number', 'chunk_index', 'machine')
    list_filter = ('machine', 'document_id')
    exclude = ('embedding',)


class AnswerSourceInline(admin.TabularInline):
    model = AnswerSource
    extra = 0
    can_delete = False
    readonly_fields = ('document_id', 'revision', 'page_number', 'verified_passage', 'score')
    exclude = ('chunk', 'manual', 'document_name', 'passage')

    @admin.display(description='Passage')
    def verified_passage(self, obj):
        return obj.passage if obj.is_verified else 'Evidence cannot be verified.'


@admin.register(QuestionHistory, site=manufacturing_admin_site)
class QuestionHistoryAdmin(RoleRestrictedAdminMixin, admin.ModelAdmin):
    list_display = ('created_at', 'user', 'machine', 'status', 'question')
    list_filter = ('machine', 'status')
    inlines = [AnswerSourceInline]
    exclude = ('answer',)
    readonly_fields = ('display_answer',)
