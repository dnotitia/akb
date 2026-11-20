"""The isolated service-key authority for SSO operation requests.

This is deliberately not a read, write, or admin scope. Route-specific
authentication must opt into it; the generic token-scope helper must never
turn an administrator or broad PAT into this capability.
"""

SSO_OPERATION_REQUEST_SCOPE = "sso_operation_requests"
SSO_OPERATION_REQUEST_SCOPES = frozenset({SSO_OPERATION_REQUEST_SCOPE})
