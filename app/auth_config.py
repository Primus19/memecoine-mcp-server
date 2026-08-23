from __future__ import annotations

from urllib.parse import urlparse

OAUTH_CALLBACK_PATH = "/auth/callback"


def validate_public_base_url(value: str) -> str:
    """Return a canonical HTTPS origin or fail before starting OAuth."""
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "PUBLIC_BASE_URL must be the HTTPS origin only "
            "(for example https://service.example), without /mcp or another path"
        )
    return base_url


def oauth_callback_url(base_url: str) -> str:
    return validate_public_base_url(base_url) + OAUTH_CALLBACK_PATH
