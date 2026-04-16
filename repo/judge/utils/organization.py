def get_organization_code_prefix(organization_slug):
    return ''.join(x for x in organization_slug.lower() if x.isalpha()) + '_'


def has_organization_code_prefix(code, organization_slug):
    return code.startswith(get_organization_code_prefix(organization_slug))
