"""Test package root.

This file exists so that ``tests/erp_pipeline/`` is imported as
``tests.erp_pipeline`` rather than as a top-level ``erp_pipeline`` package.
Without it, pytest would put ``tests/`` on ``sys.path`` and the test package
would shadow the real ``src/erp_pipeline`` package under test.
"""
