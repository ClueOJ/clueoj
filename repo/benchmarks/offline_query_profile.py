from collections import Counter
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
import re
with override_settings(ALLOWED_HOSTS=['testserver'], VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver']):
    client=Client()
    client.get('/submissions/')
    with CaptureQueriesContext(connection) as queries:
        client.get('/submissions/')
    counts=Counter(re.sub(r'\b\d+\b','?',q['sql']) for q in queries)
    for sql,count in counts.most_common(8):
        print(count,sql[:650])
