from __future__ import annotations


class BusinessError(Exception):
    """A safe, public business error. Never carry SQL, credentials or tracebacks."""

    status_code = 400
    default_code = "BUSINESS_ERROR"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code

    def to_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class InvalidRequest(BusinessError):
    status_code = 422
    default_code = "INVALID_REQUEST"


class Forbidden(BusinessError):
    status_code = 403
    default_code = "FORBIDDEN"


class NotFound(BusinessError):
    """Also used for resources owned by somebody else: do not leak their existence."""

    status_code = 404
    default_code = "NOT_FOUND"


class Conflict(BusinessError):
    status_code = 409
    default_code = "CONFLICT"


class Unavailable(BusinessError):
    status_code = 503
    default_code = "UNAVAILABLE"


class OperationExpired(Conflict):
    default_code = "OPERATION_EXPIRED"


class OperationMismatch(Conflict):
    default_code = "OPERATION_MISMATCH"


class OperationUnknown(BusinessError):
    """The commit outcome is unknown; the caller must reconcile using the operation key."""

    status_code = 503
    default_code = "OPERATION_RESULT_UNKNOWN"


class ConcurrentUpdate(Conflict):
    default_code = "VERSION_CONFLICT"
