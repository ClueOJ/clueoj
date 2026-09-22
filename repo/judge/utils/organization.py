from datetime import timedelta, timezone as datetime_timezone

from django.utils import timezone


ORGANIZATION_TIMEZONE = datetime_timezone(timedelta(hours=7))


def organization_today():
    return timezone.localdate(timezone=ORGANIZATION_TIMEZONE)


def default_paid_until():
    # New organizations start on the free plan.
    return organization_today() - timedelta(days=1)


def get_organization_code_prefix(organization_slug):
    return ''.join(x for x in organization_slug.lower() if x.isalpha()) + '_'


def has_organization_code_prefix(code, organization_slug):
    return code.startswith(get_organization_code_prefix(organization_slug))
