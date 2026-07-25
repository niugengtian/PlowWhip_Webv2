from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


OPAQUE_SECRET_REF = re.compile(
    r"(?i)^(?:secret|credential)-ref://"
    r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$"
)
_KNOWN_SECRET = re.compile(
    r"(?i)\b(?:sk-|ghp_|github_pat_|glpat-|xox[baprs]-)"
    r"[A-Za-z0-9_-]{10,}\b"
)
_BEARER = re.compile(r"(?i)\bbearer\s+([^\s,;]{8,})")
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|"
    r"password|passwd|pwd|密码)\s*[=:]\s*"
    r"(\"[^\"]+\"|'[^']+'|[^\s,;]+)"
)
_SIGNED_QUERY = re.compile(
    r"(?i)(?:[?&](?:x-amz-signature|x-goog-signature|signature|access_token)"
    r"=)([^&#\s]{8,})"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


def is_opaque_secret_ref(value: object) -> bool:
    return isinstance(value, str) and bool(OPAQUE_SECRET_REF.fullmatch(value))


def plaintext_secret_kind(value: object) -> str | None:
    for text in _strings(value):
        if _PRIVATE_KEY.search(text):
            return "private_key"
        if _KNOWN_SECRET.search(text):
            return "known_token_prefix"
        signed = _SIGNED_QUERY.search(text)
        if signed and not is_opaque_secret_ref(_unquote(signed.group(1))):
            return "signed_url"
        bearer = _BEARER.search(text)
        if bearer and not is_opaque_secret_ref(_unquote(bearer.group(1))):
            return "bearer_token"
        for assignment in _ASSIGNMENT.finditer(text):
            assigned = _unquote(assignment.group(2))
            if not is_opaque_secret_ref(assigned):
                return assignment.group(1).lower()
    return None


def require_secret_safe(value: object, field: str) -> None:
    kind = plaintext_secret_kind(value)
    if kind:
        raise ValueError(
            f"{field} contains plaintext Secret ({kind}); "
            "store the value outside PlowWhip and pass only "
            "secret-ref://... or credential-ref://..."
        )


def redact_secret(value: str) -> str:
    redacted = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", value)
    redacted = _KNOWN_SECRET.sub("[REDACTED]", redacted)
    redacted = _SIGNED_QUERY.sub(
        lambda match: match.group(0)[: match.group(0).rfind("=") + 1]
        + (
            match.group(1)
            if is_opaque_secret_ref(_unquote(match.group(1)))
            else "[REDACTED]"
        ),
        redacted,
    )
    redacted = _BEARER.sub(
        lambda match: (
            match.group(0)
            if is_opaque_secret_ref(_unquote(match.group(1)))
            else "Bearer [REDACTED]"
        ),
        redacted,
    )
    return _ASSIGNMENT.sub(_redact_assignment, redacted)


def _redact_assignment(match: re.Match[str]) -> str:
    assigned = _unquote(match.group(2))
    if is_opaque_secret_ref(assigned):
        return match.group(0)
    return f"{match.group(1)}=[REDACTED]"


def _unquote(value: str) -> str:
    return (
        value[1:-1]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}
        else value
    )


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _strings(item)
    elif isinstance(value, Sequence) and not isinstance(
        value, (bytes, bytearray)
    ):
        for item in value:
            yield from _strings(item)
