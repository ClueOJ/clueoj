from django.db import connection

from judge.utils.batch_scoring import FULL_SCORE_REL_TOL


def contest_batch_results(participation, latest=False):
    """Use finalized batch scores, preserving legacy SUM results until rejudge.

    Ranking is applied after scoring each submission's batch. Never merge
    individual test results from different submissions into a min batch.
    """
    latest_filter = '''AND NOT EXISTS (
        SELECT 1 FROM judge_contestsubmission newer_cs
        JOIN judge_submission newer ON newer.id = newer_cs.submission_id
        WHERE newer_cs.participation_id = cs.participation_id
          AND newer_cs.problem_id = cs.problem_id AND newer.status = 'D'
          AND (newer.date > sub.date OR (newer.date = sub.date AND newer.id > sub.id))
    )''' if latest else ''
    # Both result sets are restricted to this participation before UNION, so
    # historical testcase aggregation never scans the entire submissions table.
    query = '''
        SELECT prob, prob_points, date, NULLIF(batch, 0), batch_points, total_batch_points, subid
        FROM (
            SELECT scored.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY prob, batch
                       ORDER BY batch_points DESC, date ASC, subid ASC
                   ) AS ranking
            FROM (
                SELECT cp.id AS prob, cp.points AS prob_points, sub.date, sub.id AS subid,
                       b.batch,
                       CASE WHEN sub.case_total <= 0 THEN 0
                            WHEN b.legacy = 0 AND (NOT cp.partial OR NOT problem.partial)
                                 AND ABS(sub.case_points - sub.case_total) > ABS(sub.case_total) * %s THEN 0
                            ELSE b.points * cp.points / sub.case_total END AS batch_points,
                       CASE WHEN sub.case_total <= 0 THEN 0
                            ELSE b.total * cp.points / sub.case_total END AS total_batch_points
                FROM judge_contestsubmission cs
                JOIN judge_contestproblem cp ON cp.id = cs.problem_id
                JOIN judge_problem problem ON problem.id = cp.problem_id
                JOIN judge_submission sub ON sub.id = cs.submission_id
                JOIN (
                    SELECT br.submission_id, br.batch, br.points, br.total, 0 AS legacy
                    FROM judge_submissionbatchresult br
                    JOIN judge_contestsubmission bcs ON bcs.submission_id = br.submission_id
                    WHERE bcs.participation_id = %s
                    UNION ALL
                    SELECT tc.submission_id, COALESCE(tc.batch, 0), SUM(tc.points), SUM(tc.total), 1 AS legacy
                    FROM judge_submissiontestcase tc
                    JOIN judge_contestsubmission lcs ON lcs.submission_id = tc.submission_id
                    JOIN judge_submission legacy_sub ON legacy_sub.id = tc.submission_id
                    WHERE lcs.participation_id = %s AND legacy_sub.batch_scoring IS NULL
                      AND NOT EXISTS (SELECT 1 FROM judge_submissionbatchresult br WHERE br.submission_id = tc.submission_id)
                    GROUP BY tc.submission_id, tc.batch
                ) b ON b.submission_id = sub.id
                WHERE cs.participation_id = %s AND sub.status = 'D'
                ''' + latest_filter + '''
            ) scored
        ) ranked
        WHERE ranking = 1
        ORDER BY prob, batch
    '''
    with connection.cursor() as cursor:
        cursor.execute(query, [FULL_SCORE_REL_TOL] + [participation.pk] * 3)
        rows = cursor.fetchall()
    # MariaDB can promote integer columns to Decimal across the legacy UNION.
    rows = [(int(prob), float(maximum), date, int(batch) if batch is not None else None,
             float(points), float(total), int(subid))
            for prob, maximum, date, batch, points, total, subid in rows]
    # Ultimate's existing resolver contract places subid last, but points/total
    # precede batch. IOI uses six columns and batch precedes points/total.
    if latest:
        return [(prob, maximum, date, points, total, batch, subid)
                for prob, maximum, date, batch, points, total, subid in rows]
    return [row[:6] for row in rows]
