"""Connectors to a tenant's own accounting system.

Deliberately no factory function here, unlike `app/services/ai/__init__.py`'s
`get_ai_provider()`. That factory reads one global `settings.AI_PROVIDER` for
the whole deployment; this package's whole point is that the provider is a
property of a *connection* — one tenant may be on QuickBooks, another on
nothing at all. Resolution lives in `IntegrationService._connector_for`,
where a `connection.provider` value is on hand to resolve from.
"""
