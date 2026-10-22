"""Shared, non-collected support code for guard tests.

Modules here are helpers, not tests. The leading underscore keeps pytest's
default ``test_*`` collection away from the directory while still letting guard
tests import it as ``test._support.<module>``.
"""
