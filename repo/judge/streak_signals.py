"""Streaks deliberately have no model mutation signals.

Initial grading completion records an immutable event explicitly. Rejudging,
publication, deletions, organization changes and timezone edits cannot rewrite
previous achievements. Kept as an import-compatible module for old deployments.
"""
