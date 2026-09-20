"""Public allowlist serialization and request-only practice score comparisons."""
from decimal import Decimal, ROUND_HALF_EVEN

from django.utils.formats import number_format


def decimal_string(value):
    text = format(Decimal(value), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def format_milestone_score(value):
    return number_format(Decimal(decimal_string(value)), use_l10n=True, force_grouping=False)


def serialize_score_reference(exam):
    milestones = exam.active_score_milestones
    if not milestones:
        return None
    return {
        'score_context': exam.milestone_score_context,
        'note': exam.milestone_note,
        'milestones': [{
            'id': row.pk,
            'label': row.label,
            'score': decimal_string(row.score),
            'compare_with_practice_score': row.compare_with_practice_score,
            'score_context': row.score_context,
            'note': row.note,
            'sort_order': row.sort_order,
        } for row in milestones],
    }


def evaluate_score_milestones(metadata, practice_score, is_authenticated):
    if not metadata:
        return None
    score = Decimal(str(practice_score)).quantize(Decimal('0.001'), rounding=ROUND_HALF_EVEN)
    rows = [{
        **row,
        'score_display': format_milestone_score(row['score']),
        'is_reached': (score >= Decimal(row['score']))
        if is_authenticated and row['compare_with_practice_score'] else None,
    } for row in metadata['milestones']]
    show_status = any(row['is_reached'] is not None for row in rows)
    return {
        **metadata,
        'milestones': rows,
        'show_status': show_status,
        # Equal thresholds keep the administrator's stable display order.
        'highest_reached': max(
            (row for row in rows if row['is_reached'] is True),
            key=lambda row: Decimal(row['score']), default=None,
        ),
        'practice_score_display': format_milestone_score(score) if show_status else None,
    }
