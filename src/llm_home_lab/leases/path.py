"""Carry a lease token in the request path so an unmodified client can pin a node."""

import re

_LEASE_PREFIX = re.compile(r"^/lease/(?P<token>[^/]+)(?P<rest>/.*)?$")
_LEASE_HEADER = b"x-llm-node-lease"


def rewrite_lease_path(scope: dict[str, object]) -> None:
    """Strip ``/lease/{token}`` and pin that token when the request has no lease header.

    ``/lease/{token}/capacity`` is the capacity route. Every other path under the
    prefix is the same route it would be without the prefix, including
    ``/v1/chat/completions`` and ``/v1/capacity``.
    """
    path = scope.get("path")
    if not isinstance(path, str):
        return
    match = _LEASE_PREFIX.match(path)
    if match is None:
        return
    token = match.group("token")
    rest = match.group("rest") or "/"
    if rest == "/capacity" or rest.startswith("/capacity/"):
        rest = "/v1" + rest
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
