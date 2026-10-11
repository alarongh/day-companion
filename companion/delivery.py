"""Metadata for rechecking scheduled cloud messages immediately before delivery."""

from contextvars import ContextVar

NOTICE_CONTEXT = ContextVar("notice_context", default=None)
