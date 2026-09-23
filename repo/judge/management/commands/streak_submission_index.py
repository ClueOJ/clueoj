from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from judge.models import Submission


class Command(BaseCommand):
    help = 'Prepare the streak submission index online on MariaDB/MySQL; dry-run by default.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        if connection.vendor != 'mysql':
            raise CommandError('This online index operation requires MariaDB/MySQL.')
        table = Submission._meta.db_table
        columns = ['user_id', 'problem_id', 'date', 'id']
        with connection.cursor() as cursor:
            indexes = connection.introspection.get_constraints(cursor, table)
            if any(value.get('index') and value['columns'][:4] == columns for value in indexes.values()):
                self.stdout.write('A suitable index already exists; nothing changed.')
                return
            sql = ('ALTER TABLE %s ADD INDEX streak_sub_pair_date_idx (user_id, problem_id, date, id), '
                   'ALGORITHM=INPLACE, LOCK=NONE' % connection.ops.quote_name(table))
            self.stdout.write(sql)
            if not options['apply']:
                self.stdout.write('Dry run. Check disk space and database load before --apply.')
                return
            cursor.execute('SELECT @@SESSION.lock_wait_timeout')
            original_timeout = cursor.fetchone()[0]
            try:
                cursor.execute('SET SESSION lock_wait_timeout=5')
                # Explicit LOCK=NONE: fail rather than silently copy/lock the table.
                cursor.execute(sql)
            finally:
                cursor.execute('SET SESSION lock_wait_timeout=%s', [original_timeout])
        self.stdout.write('Index created. No submission rows or scores were modified.')
