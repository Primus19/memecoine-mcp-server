from __future__ import annotations

import os

CRYPTO_MODEL_VERSION = "CRYPTO_MULTI_HORIZON_4.0"
FOREX_MODEL_VERSION = "FOREX_MULTI_HORIZON_2.0"
SCHEMA_VERSION = "4.0"


def deployment_info() -> dict[str, str]:
    """Return non-secret Railway build identity for deployment verification."""
    return {
        "schema_version": SCHEMA_VERSION,
        "crypto_model_version": CRYPTO_MODEL_VERSION,
        "forex_model_version": FOREX_MODEL_VERSION,
        "git_commit_sha": os.getenv("RAILWAY_GIT_COMMIT_SHA", "").strip(),
        "git_branch": os.getenv("RAILWAY_GIT_BRANCH", "").strip(),
        "deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID", "").strip(),
        "service_name": os.getenv("RAILWAY_SERVICE_NAME", "").strip(),
    }
