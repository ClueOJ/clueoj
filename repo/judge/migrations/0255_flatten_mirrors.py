from django.db import migrations


SOURCES = {
    'clue_problem_02': 'dn_tst_24_catdayso',
    'nvnamson_bobon': 'qn_hsg_20_b',
    'nvnamson_chonbi': 'nd_hsg_22_chonbi',
    'nvnamson_chondoituyen': 'nd_hsg_22_chondoi',
    'nvnamson_daydephsg12namdinh2022': 'nd_hsg_22_daydep',
    'nvnamson_ontap': 'lc_ts10_25_e',
    'nvnamson_phatqua': 'qt_tst_21_phatqua',
    'nvnamson_tangquahsg12namdinh2022': 'nd_hsg_22_qua',
}


def flatten_mirrors(apps, schema_editor):
    Problem = apps.get_model('judge', 'Problem')
    problems = Problem.objects.using(schema_editor.connection.alias)
    for code, root_code in SOURCES.items():
        target = problems.filter(code=code).first()
        if target is None:
            continue
        root = problems.get(code=root_code, mirror_of__isnull=True)
        if target.mirror_root_id != root.pk:
            raise RuntimeError('Unexpected mirror root for %s' % code)
        problems.filter(pk=target.pk).update(mirror_of_id=root.pk, mirror_root_id=root.pk)


class Migration(migrations.Migration):
    dependencies = [('judge', '0254_mirror_complete_dataset_help')]
    operations = [migrations.RunPython(flatten_mirrors, migrations.RunPython.noop)]
