import os
import unittest
from unittest.mock import patch

from app.version import CRYPTO_MODEL_VERSION, FOREX_MODEL_VERSION, SCHEMA_VERSION, deployment_info


class VersionTests(unittest.TestCase):
    def test_deployment_info_exposes_model_and_railway_identity(self):
        env = {
            "RAILWAY_GIT_COMMIT_SHA": "abc123",
            "RAILWAY_GIT_BRANCH": "main",
            "RAILWAY_DEPLOYMENT_ID": "deployment-1",
            "RAILWAY_SERVICE_NAME": "crypto",
        }
        with patch.dict(os.environ, env, clear=False):
            info = deployment_info()
        self.assertEqual(SCHEMA_VERSION, info["schema_version"])
        self.assertEqual(CRYPTO_MODEL_VERSION, info["crypto_model_version"])
        self.assertEqual(FOREX_MODEL_VERSION, info["forex_model_version"])
        self.assertEqual("abc123", info["git_commit_sha"])
        self.assertEqual("deployment-1", info["deployment_id"])


if __name__ == "__main__":
    unittest.main()
