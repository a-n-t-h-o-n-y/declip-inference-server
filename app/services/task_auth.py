from typing import Protocol

from google.auth.transport import requests
from google.oauth2 import id_token

from app.models.domain import VerifiedServiceToken


class TokenVerificationError(Exception):
    """Raised when an OIDC service token cannot be verified."""


class ServiceTokenVerifier(Protocol):
    def verify(self, token: str) -> VerifiedServiceToken:
        """Verify a bearer token and return trusted service identity claims."""


class GoogleServiceTokenVerifier:
    def __init__(self, audience: str | None) -> None:
        self._audience = audience
        self._request = requests.Request()

    def verify(self, token: str) -> VerifiedServiceToken:
        try:
            claims = id_token.verify_oauth2_token(token, self._request, audience=self._audience)
        except Exception as exc:
            raise TokenVerificationError("invalid token") from exc

        email = claims.get("email")
        if not email:
            raise TokenVerificationError("missing email claim")
        return VerifiedServiceToken(
            email=email,
            audience=claims.get("aud"),
            subject=claims.get("sub"),
        )


class FakeServiceTokenVerifier:
    """Local test/dev verifier. Token value is the caller service account email."""

    def verify(self, token: str) -> VerifiedServiceToken:
        if not token or token == "invalid":
            raise TokenVerificationError("invalid token")
        return VerifiedServiceToken(email=token)
