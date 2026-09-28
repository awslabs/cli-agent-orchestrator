"""Single builder for the ``Authorization`` bearer header (#745).

Every client hop that authenticates to a cao-server presents an OAuth 2.1
bearer token. Building the header in one place keeps the scheme and its
separating space in a named constant instead of inline in each caller, so the
scheme never appears joined to a token as one literal in the source.

This decides only HOW the header reads. Callers that must scope a credential
to a particular destination (see ``utils.orchestration._auth_headers``) decide
WHETHER to build one at all.
"""

from typing import Dict, Optional

#: RFC 6750 auth scheme. A constant, so no caller writes the scheme immediately
#: followed by a value in a single string literal.
AUTH_SCHEME = "Bearer"


def authorization_header(token: Optional[str]) -> Dict[str, str]:
    """Return the ``Authorization`` header for *token*, or ``{}`` when empty.

    The value is the scheme, a single space, and the token, assembled from
    :data:`AUTH_SCHEME` so the two never sit adjacent as one source literal.
    """
    if not token:
        return {}
    return {"Authorization": f"{AUTH_SCHEME} {token}"}
