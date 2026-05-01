import os
import zipfile

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext as _

from judge.models import Problem, ProblemData, ProblemTestCase, problem_data_storage
from judge.utils.problem_data import ProblemDataCompiler


def get_problem_single_organization(problem):
    if not problem.is_organization_private:
        return None

    org_ids = list(problem.organizations.values_list('id', flat=True))
    if len(org_ids) != 1:
        return None

    return problem.organizations.get(id=org_ids[0])


def is_organization_admin(user, organization):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.has_perm('judge.edit_all_organization'):
        return True
    return organization.admins.filter(pk=user.profile.pk).exists()


def validate_mirror_source_for_target(user, source, target_problem=None, target_org=None):
    if source is None:
        return

    if target_problem is not None and target_problem.pk and source.pk == target_problem.pk:
        raise ValidationError(_('A problem cannot mirror itself.'))

    if target_org is None and target_problem is not None:
        target_org = get_problem_single_organization(target_problem)
        if target_problem.is_organization_private and target_org is None:
            raise ValidationError(_('Organization-private problems must belong to exactly one organization to use mirroring.'))

    if target_org is not None and not is_organization_admin(user, target_org):
        raise ValidationError(_('Only organization admins can configure mirroring for organization-private problems.'))

    if source.is_organization_private:
        source_org = get_problem_single_organization(source)
        if source_org is None:
            raise ValidationError(_('Only organization-private problems belonging to exactly one organization can be mirrored.'))

        if target_org is None:
            raise ValidationError(_('Organization-private source problems can only be mirrored inside the same organization.'))

        if source_org.id != target_org.id:
            raise ValidationError(_('Mirroring across organizations is not allowed.'))

        if not is_organization_admin(user, target_org):
            raise ValidationError(_('Only organization admins can mirror organization-private problems.'))

        return

    if source.is_public and not source.is_organization_private:
        return

    raise ValidationError(_('Only public problems or same-organization private problems can be mirrored.'))


def resolve_mirror_root_id(mirror_of_id, current_problem_id=None):
    if not mirror_of_id:
        return None

    seen = set()
    if current_problem_id is not None:
        seen.add(current_problem_id)

    current_id = mirror_of_id
    while current_id is not None:
        if current_id in seen:
            raise ValidationError(_('Mirror relationship cannot contain cycles.'))
        seen.add(current_id)

        row = Problem.objects.filter(pk=current_id).values('id', 'mirror_of_id', 'mirror_root_id').first()
        if row is None:
            raise ValidationError(_('Mirror source problem does not exist.'))

        if row['mirror_of_id'] is None:
            return row['id']

        if row['mirror_root_id'] and row['mirror_root_id'] != row['id']:
            if row['mirror_root_id'] in seen:
                raise ValidationError(_('Mirror relationship cannot contain cycles.'))
            return row['mirror_root_id']

        current_id = row['mirror_of_id']

    return None


def rebuild_mirror_descendants(problem_id):
    queue = [problem_id]
    visited = set()

    while queue:
        parent_id = queue.pop(0)
        if parent_id in visited:
            continue
        visited.add(parent_id)

        children = list(Problem.objects.filter(mirror_of_id=parent_id))
        for child in children:
            root_id = resolve_mirror_root_id(child.mirror_of_id, current_problem_id=child.id)
            if child.mirror_root_id != root_id:
                Problem.objects.filter(pk=child.id).update(mirror_root_id=root_id)
            queue.append(child.id)


def _ensure_hardlink_or_copy(src_abs, dst_abs):
    # Legacy mirror-archive sync helper.
    # Kept for rollback/reference while sync_mirror_archive_for_problem() is intentionally disabled.
    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)

    if os.path.exists(dst_abs):
        if os.path.samefile(src_abs, dst_abs):
            return
        os.remove(dst_abs)

    try:
        os.link(src_abs, dst_abs)
    except OSError:
        with open(src_abs, 'rb') as src_file, open(dst_abs, 'wb') as dst_file:
            dst_file.write(src_file.read())


def _zip_file_names(data):
    # Legacy mirror-archive sync helper.
    # Kept for rollback/reference while sync_mirror_archive_for_problem() is intentionally disabled.
    if not data.zipfile:
        return []
    try:
        with zipfile.ZipFile(data.zipfile.path) as archive:
            return archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return []


def _copy_cases_if_missing(problem, root_problem):
    # Legacy mirror-archive sync helper.
    # Kept for rollback/reference while sync_mirror_archive_for_problem() is intentionally disabled.
    if ProblemTestCase.objects.filter(dataset_id=problem.id).exists():
        return False
    rows = []
    for case in root_problem.cases.order_by('order'):
        rows.append(ProblemTestCase(
            dataset=problem,
            order=case.order,
            type=case.type,
            input_file=case.input_file,
            output_file=case.output_file,
            generator_args=case.generator_args,
            points=case.points,
            is_pretest=case.is_pretest,
            output_prefix=case.output_prefix,
            output_limit=case.output_limit,
            checker=case.checker,
            checker_args=case.checker_args,
        ))
    if rows:
        ProblemTestCase.objects.bulk_create(rows)
        return True
    return False


def _input_candidates(valid_files):
    # Legacy mirror-archive sync helper.
    # Kept for rollback/reference while sync_mirror_archive_for_problem() is intentionally disabled.
    candidates = [f for f in valid_files if f.endswith('.in') or '.in.' in f or 'input' in f.lower()]
    if candidates:
        return sorted(candidates)
    return sorted(valid_files)


def _output_candidates(valid_files):
    # Legacy mirror-archive sync helper.
    # Kept for rollback/reference while sync_mirror_archive_for_problem() is intentionally disabled.
    candidates = [f for f in valid_files if f.endswith('.out') or '.out.' in f or 'output' in f.lower() or 'ans' in f.lower()]
    if candidates:
        return sorted(candidates)
    return sorted(valid_files)


def _repair_case_files_from_root(problem, root_problem, valid_files):
    # Legacy mirror-archive sync helper.
    # Kept for rollback/reference while sync_mirror_archive_for_problem() is intentionally disabled.
    if not valid_files:
        return False

    valid_file_set = set(valid_files)
    mirror_cases = list(problem.cases.order_by('order'))
    root_cases = list(root_problem.cases.order_by('order'))
    input_candidates = _input_candidates(valid_files)
    output_candidates = _output_candidates(valid_files)
    changed = False

    for index, mirror_case in enumerate(mirror_cases):
        root_case = root_cases[index] if index < len(root_cases) else None
        update_fields = []
        if mirror_case.type != 'C':
            continue
        if mirror_case.input_file not in valid_file_set:
            replacement = None
            if root_case is not None and root_case.input_file in valid_file_set:
                replacement = root_case.input_file
            elif input_candidates:
                replacement = input_candidates[min(index, len(input_candidates) - 1)]
            if replacement is not None:
                mirror_case.input_file = replacement
                update_fields.append('input_file')
        if mirror_case.output_file not in valid_file_set:
            replacement = None
            if root_case is not None and root_case.output_file in valid_file_set:
                replacement = root_case.output_file
            elif output_candidates:
                replacement = output_candidates[min(index, len(output_candidates) - 1)]
            if replacement is not None:
                mirror_case.output_file = replacement
                update_fields.append('output_file')
        if update_fields:
            mirror_case.save(update_fields=update_fields)
            changed = True
    return changed


def _copy_root_config_if_new(mirror_data, root_data, created):
    # Legacy mirror-archive sync helper.
    # Kept for rollback/reference while sync_mirror_archive_for_problem() is intentionally disabled.
    if not created or root_data is None:
        return

    fields = (
        'output_prefix', 'output_limit', 'checker', 'grader', 'unicode',
        'nobigmath', 'checker_args', 'grader_args',
    )

    for field in fields:
        setattr(mirror_data, field, getattr(root_data, field))


def sync_mirror_archive_for_problem(problem, bootstrap_cases_if_empty=False, heal_missing_files=False,
                                    force_regenerate=False):
    # Current behavior (intentionally active): mirror problems do not run archive sync/generate flow here.
    # Judge-side routing now uses mirror_root code directly, so this hook is a no-op by design.
    #
    # NOTE:
    # - Legacy implementation was intentionally converted to comments to avoid unreachable runtime code.
    # - Do not remove lightly unless all callers are refactored in one change-set.
    #
    # Legacy flow (disabled):
    # 1) Resolve mirror root id from mirror_of/mirror_root chain.
    # 2) Load root ProblemData and mirror ProblemData.
    # 3) Optionally bootstrap mirror testcases from root when mirror has no cases.
    # 4) Link/copy root zip archive into mirror path and mark archive_source_problem.
    # 5) Repair invalid testcase input/output mappings against archive file list.
    # 6) Regenerate mirror init.yml via ProblemDataCompiler.
    return False


def sync_mirror_archives_for_root(root_problem):
    changed = 0
    mirrors = Problem.objects.filter(mirror_root_id=root_problem.id).exclude(pk=root_problem.id)
    for mirror in mirrors:
        if sync_mirror_archive_for_problem(
            mirror, bootstrap_cases_if_empty=False, heal_missing_files=True, force_regenerate=True,
        ):
            changed += 1
    return changed


def get_mirrorable_source_queryset(user, target_problem=None, target_org=None):
    if user is None:
        return Problem.objects.none()

    queryset = Problem.get_visible_problems(user)

    if target_problem is not None and target_problem.pk:
        queryset = queryset.exclude(pk=target_problem.pk)

    public_q = Q(is_public=True, is_organization_private=False)

    if target_org is None and target_problem is not None:
        target_org = get_problem_single_organization(target_problem)

    if target_org is None:
        return queryset.filter(public_q)

    if not is_organization_admin(user, target_org):
        return Problem.objects.none()

    org_q = Q(is_organization_private=True, organizations=target_org)
    return queryset.filter(public_q | org_q).distinct()
