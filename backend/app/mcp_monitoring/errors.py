"""Store catalogued failure codes only. Messages and envelope details can contain user data."""

from contextvars import ContextVar
from dataclasses import dataclass

from app.util import errors

REASONS = {
    errors.NOT_FOUND: "The requested resource could not be found.",
    errors.GONE: "The resource is no longer available.",
    errors.PERMISSION_DENIED: "The caller lacks permission for this resource.",
    errors.INSUFFICIENT_SCOPE: "The access token lacks the required scope.",
    errors.INVALID_ARGUMENT: "The tool rejected an argument.",
    errors.UNKNOWN_ARGUMENT: "The request contains an unsupported argument.",
    errors.UNKNOWN_TOOL: "The requested tool is not in the catalog.",
    errors.INVALID_URI: "The resource URI is invalid.",
    errors.INVALID_PATH: "The resource path is invalid.",
    errors.RESERVED_SYSTEM_PATH_CODE: "The path is reserved for system use.",
    errors.VAULT_ARCHIVED: "The vault is archived.",
    errors.VAULT_WRITE_MANAGED: "The vault write policy rejected the request.",
    errors.VAULT_SKILL_REQUIRED: "The caller must acknowledge the vault guide before writing.",
    errors.CONFLICT: "The resource state conflicts with the requested change.",
    errors.WRITE_BUSY: "The writer could not acquire the resource in time.",
    errors.UNIQUE_VIOLATION: "A value violates a uniqueness constraint.",
    errors.VAULT_NAME_UNAVAILABLE: "The requested vault name is unavailable.",
    errors.DOCUMENT_TITLE_CONFLICT: "The document title conflicts with an existing title.",
    errors.EDIT_FAILED: "The requested edit could not be applied.",
    errors.DOCUMENT_IDEMPOTENCY_UNSUPPORTED: "This document operation does not support idempotency.",
    errors.NATIVE_REVISION_SELECTOR_AMBIGUOUS: "The revision selector matches multiple revisions.",
    errors.NATIVE_REVISION_SURFACE_UNSUPPORTED: "The revision backend does not support this operation.",
    errors.SERVICE_UNAVAILABLE: "A required service was unavailable.",
    errors.OPERATION_TIMEOUT: "The operation timed out.",
    errors.INTERNAL: "The tool encountered an internal error.",
    errors.GREP_SCOPE_TOO_LARGE: "The search scope exceeds the allowed size.",
    errors.INVALID_COLUMN_TYPE: "The column type is invalid.",
    errors.MULTI_STATEMENT: "The request contains multiple SQL statements.",
    errors.METHOD_NOT_ALLOWED: "The requested operation is not allowed here.",
    errors.SQL_ERROR: "The database rejected the SQL request.",
    errors.UNDEFINED_COLUMN: "The referenced column does not exist.",
    errors.UNDEFINED_TABLE: "The referenced table does not exist.",
    errors.UNFILTERED_MUTATION: "The write requires a filter.",
    errors.BULK_TOO_LARGE: "The batch exceeds the size limit.",
    errors.NO_UNIQUE_CONSTRAINT: "The operation requires a uniqueness constraint.",
    errors.INVALID_FILTER: "The filter is malformed.",
    errors.INVALID_OPERATOR: "The filter operator is unsupported.",
    errors.INVALID_CAST: "The requested type conversion is invalid.",
    errors.FILTER_TOO_DEEP: "The filter exceeds the nesting limit.",
    errors.NOT_IMPLEMENTED: "The requested operation is not implemented.",
    errors.SELF_LINK: "A resource cannot link to itself.",
    errors.GIT_HISTORY_FAILED: "Revision history could not be read.",
    errors.GIT_HISTORY_TIMEOUT: "Reading revision history timed out.",
    errors.GIT_HISTORY_OUTPUT_CAPPED: "Revision history exceeded the output limit.",
    errors.GIT_HISTORY_ENTRY_CAPPED: "Revision history exceeded the entry limit.",
    "-32700": "The RPC message could not be parsed.",
    "-32600": "The RPC request is invalid.",
    "-32601": "The RPC method does not exist.",
    "-32602": "The RPC parameters are invalid.",
    "-32603": "The RPC handler encountered an internal error.",
}


@dataclass(slots=True)
class CallFailure:
    code: str | None = None


# The SDK may serialize the handler result or delegate to a child task before
# middleware resumes. Keep the bounded projection in the context of this call.
current_failure = ContextVar[CallFailure | None]("mcp_monitoring_failure", default=None)


def capture_error_code(code: object) -> None:
    failure = current_failure.get()
    if failure is not None and isinstance(code, str) and len(code) <= 64 and code in REASONS:
        failure.code = code
