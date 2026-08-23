from __future__ import annotations

import os
import secrets
from pathlib import Path
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


def load_or_create_signing_key(data_dir: str, configured_key: str | None) -> tuple[str, str]:
    """Use the host secret when present, otherwise persist a generated key."""
    if configured_key and len(configured_key.strip()) >= 32:
        return configured_key.strip(), "environment"
    if configured_key:
        raise ValueError("JWT_SIGNING_KEY must contain at least 32 characters")

    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    key_path = directory / "oauth_jwt_signing_key"
    try:
        key = key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        key = secrets.token_urlsafe(48)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(key_path, flags, 0o600)
        except FileExistsError:
            key = key_path.read_text(encoding="utf-8").strip()
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(key)
    if len(key) < 32:
        raise RuntimeError("persisted OAuth signing key is invalid")
    return key, "persistent_file"
