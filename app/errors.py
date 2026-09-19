"""OpenAI-shaped error responses and MiMo status mapping."""
from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class AdapterError(HTTPException):
    """HTTPException carrying an OpenAI-style error envelope."""

    def __init__(self, status_code: int, message: str, err_type: str, code: str | None = None, param: str | None = None):
        super().__init__(status_code=status_code, detail=message)
        self.err_type = err_type
        self.code = code
        self.param = param

    def body(self) -> dict:
        return error_body(self.detail, self.err_type, self.code, self.param)


def error_body(message: str, err_type: str, code: str | None = None, param: str | None = None) -> dict:
    err: dict = {"message": message, "type": err_type}
    err["param"] = param
    err["code"] = code
    return {"error": err}


def error_response(status: int, message: str, err_type: str, code: str | None = None, param: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(message, err_type, code, param))


def invalid_request(message: str, code: str | None = None, param: str | None = None) -> AdapterError:
    return AdapterError(400, message, "invalid_request_error", code, param)


# MiMo/upstream status -> (adapter status, error type, code)
_UPSTREAM_MAP: dict[int, tuple[int, str, str]] = {
    400: (400, "invalid_request_error", "upstream_invalid_request"),
    401: (502, "api_error", "upstream_auth_failed"),
    403: (502, "api_error", "upstream_forbidden"),
    404: (502, "api_error", "upstream_not_found"),
    413: (413, "invalid_request_error", "audio_too_large"),
    422: (400, "invalid_request_error", "upstream_unprocessable"),
    429: (429, "rate_limit_error", "upstream_rate_limited"),
    500: (502, "api_error", "upstream_error"),
    502: (502, "api_error", "upstream_bad_gateway"),
    503: (503, "api_error", "upstream_unavailable"),
    504: (504, "api_error", "upstream_timeout"),
}


def map_upstream_status(status: int) -> tuple[int, str, str]:
    """Translate an upstream status into our own.

    401/403 become 502: the client's own credentials were already accepted, so a
    rejection here is a misconfiguration of the adapter's upstream key, not the
    caller's fault. Reflecting 401 would wrongly tell the caller to re-auth.
    """
    if status in _UPSTREAM_MAP:
        return _UPSTREAM_MAP[status]
    if 500 <= status:
        return (502, "api_error", "upstream_error")
    return (502, "api_error", "upstream_unexpected_status")
