"""Removed: the previous POST /files/upload was an unauthenticated placeholder
that accepted uploads and did nothing.

Invoice document upload (with auth, tenant RLS, size/magic-byte validation, OCR,
storage and hashing) lives at POST /api/v1/invoices/upload. This module is kept
only to avoid breaking stale imports and intentionally exposes no router.
"""
