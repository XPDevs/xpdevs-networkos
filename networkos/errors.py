"""Exception types shared across the backend."""

from __future__ import annotations

from typing import Any


class NetworkOSError(Exception):
    """Base class for expected, user-facing failures."""

    status = 400
    code = "error"

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigurationError(NetworkOSError):
    status = 400
    code = "configuration_error"


class CommandError(NetworkOSError):
    """A privileged command failed. Never contains user-supplied shell text."""

    status = 500
    code = "command_failed"

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "", command: str = ""):
        super().__init__(message, details={"command": command} if command else None)
        self.returncode = returncode
        self.stderr = stderr
        self.command = command


class NotFoundError(NetworkOSError):
    status = 404
    code = "not_found"


class ForbiddenError(NetworkOSError):
    status = 403
    code = "forbidden"


class UnauthorizedError(NetworkOSError):
    status = 401
    code = "unauthorized"


class RateLimitError(NetworkOSError):
    status = 429
    code = "rate_limited"


class PiholeError(NetworkOSError):
    status = 502
    code = "pihole_unavailable"


class UnsupportedFeature(NetworkOSError):
    status = 409
    code = "unsupported"


class ApiError(NetworkOSError):
    """Raised by API endpoints with an explicit HTTP status."""

    def __init__(self, message: str, *, status: int = 400, code: str = "bad_request", details: Any = None):
        super().__init__(message, details=details)
        self.status = status
        self.code = code
