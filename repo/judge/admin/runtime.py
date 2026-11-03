from django.contrib import messages
from django.db.models import TextField
from django.forms import ModelForm, TextInput
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from reversion.admin import VersionAdmin

from django_ace import AceWidget
from judge.models import Judge, Problem
from judge.widgets import AdminMartorWidget


class LanguageForm(ModelForm):
    class Meta:
        widgets = {'description': AdminMartorWidget}


class LanguageAdmin(VersionAdmin):
    fields = ('key', 'name', 'short_name', 'common_name', 'file_only', 'file_size_limit',
              'include_in_problem', 'ace', 'pygments', 'info', 'extension',
              'description', 'template')
    list_display = ('key', 'name', 'common_name', 'info')
    form = LanguageForm

    def save_model(self, request, obj, form, change):
        super(LanguageAdmin, self).save_model(request, obj, form, change)
        if not change and obj.include_in_problem:
            # If this lang has just been created
            # and it should include in problems
            obj.problem_set.set(Problem.objects.all())

    def get_form(self, request, obj=None, **kwargs):
        form = super(LanguageAdmin, self).get_form(request, obj, **kwargs)
        if obj is not None:
            form.base_fields['template'].widget = AceWidget(
                mode=obj.ace, theme=request.profile.resolved_ace_theme,
            )
        return form


class GenerateKeyTextInput(TextInput):
    def render(self, name, value, attrs=None, renderer=None):
        text = super(TextInput, self).render(name, value, attrs)
        return mark_safe(text + format_html(
            """\
<a href="#" onclick="return false;" class="button" id="id_{0}_regen">{1}</a>
<script type="text/javascript">
django.jQuery(document).ready(function ($) {{
    $('#id_{0}_regen').click(function () {{
        var rand = new Uint8Array(75);
        window.crypto.getRandomValues(rand);
        var key = btoa(String.fromCharCode.apply(null, rand));
        $('#id_{0}').val(key);
    }});
}});
</script>
""", name, _('Regenerate')))


class JudgeAdminForm(ModelForm):
    class Meta:
        widgets = {'auth_key': GenerateKeyTextInput, 'description': AdminMartorWidget}


class JudgeAdmin(VersionAdmin):
    form = JudgeAdminForm
    readonly_fields = ('created', 'online', 'start_time', 'ping', 'load', 'last_ip', 'runtimes', 'problems',
                       'is_disabled')
    fieldsets = (
        (None, {'fields': ('name', 'auth_key', 'is_blocked', 'is_disabled', 'is_remote')}),
        (_('Description'), {'fields': ('description',)}),
        (_('Information'), {'fields': ('created', 'online', 'last_ip', 'start_time', 'ping', 'load')}),
        (_('Capabilities'), {'fields': ('runtimes',)}),
    )
    list_display = ('name', 'online', 'is_remote', 'is_disabled', 'start_time', 'ping', 'load', 'last_ip')
    list_filter = ('is_remote', 'online', 'is_disabled')
    actions = ('mark_local', 'mark_remote')
    ordering = ['-online', 'name']
    formfield_overrides = {
        TextField: {'widget': AdminMartorWidget},
    }

    def _set_remote(self, request, queryset, is_remote):
        updated = queryset.filter(online=False).update(is_remote=is_remote)
        self.message_user(request, _('Updated %(count)d offline judge(s).') % {'count': updated})
        if queryset.filter(online=True).exists():
            self.message_user(request, _('Online judges were skipped. Disconnect them before changing their type.'),
                              level=messages.WARNING)

    def mark_local(self, request, queryset):
        self._set_remote(request, queryset, False)

    mark_local.short_description = _('Mark selected offline judges as local')
    mark_local.allowed_permissions = ('change',)

    def mark_remote(self, request, queryset):
        self._set_remote(request, queryset, True)

    mark_remote.short_description = _('Mark selected offline judges as remote')
    mark_remote.allowed_permissions = ('change',)

    def get_urls(self):
        return ([path('<int:id>/disconnect/', self.disconnect_view, name='judge_judge_disconnect'),
                 path('<int:id>/terminate/', self.terminate_view, name='judge_judge_terminate'),
                 path('<int:id>/disable/', self.disable_view, name='judge_judge_disable')] +
                super(JudgeAdmin, self).get_urls())

    def disconnect_judge(self, id, force=False):
        judge = get_object_or_404(Judge, id=id)
        judge.disconnect(force=force)
        return HttpResponseRedirect(reverse('admin:judge_judge_changelist'))

    def disconnect_view(self, request, id):
        return self.disconnect_judge(id)

    def terminate_view(self, request, id):
        return self.disconnect_judge(id, force=True)

    def disable_view(self, request, id):
        judge = get_object_or_404(Judge, id=id)
        judge.toggle_disabled()
        return HttpResponseRedirect(reverse('admin:judge_judge_change', args=(judge.id,)))

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.online:
            return self.readonly_fields + ('name', 'is_remote')
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        result = super(JudgeAdmin, self).has_delete_permission(request, obj)
        if result and obj is not None:
            return not obj.online
        return result
