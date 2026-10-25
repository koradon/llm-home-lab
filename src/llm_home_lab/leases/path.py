"""Carry a lease token in the request path so an unmodified client can pin a node."""

import re

_LEASE_PREFIX = re.compile(r"^/lease/(?P<token>[^/]+)(?P<rest>/.*)?$")
_LEASE_HEADER = b"x-llm-node-lease"


def rewrite_lease_path(scope: dict[str, object]) -> None:
    """Strip ``/lease/{token}`` and pin that token when the request has no lease header.

    Only POST chat completions and GET capacity routes accept the prefix.
    """
    path = scope.get("path")
    if not isinstance(path, str):
        return
    match = _LEASE_PREFIX.match(path)
    if match is None:
        return
    token = match.group("token")
    rest = match.group("rest") or "/"
    if (scope.get("method"), rest) not in {
        ("POST", "/v1/chat/completions"),
        ("GET", "/v1/capacity"),
        ("GET", "/capacity"),
    }:
        return
    if rest == "/capacity":
        rest = "/v1/capacity"
    scope["path"] = rest
    scope["raw_path"] = rest.encode("utf-8")
    raw_headers = scope.get("headers")
    if not isinstance(raw_headers, list):
        return
    for item in raw_headers:
        if (
            isinstance(item, tuple)
            and len(item) >= 1
            and isinstance(item[0], bytes)
            and item[0].lower() == _LEASE_HEADER
        ):
            return
    raw_headers.append((_LEASE_HEADER, token.encode("utf-8")))
