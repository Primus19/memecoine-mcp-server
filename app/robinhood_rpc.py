"""Credential-safe, read-only Robinhood mainnet connectivity verification."""
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

CHAIN_ID = 4663


def rpc_call(endpoint, method, params):
    request = Request(endpoint, data=json.dumps({"jsonrpc": "2.0", "id": 1,
                      "method": method, "params": params}).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=5) as response:
        value = json.load(response)
    if value.get("error") or "result" not in value or value.get("id") != 1:
        raise ValueError("invalid RPC response")
    return value["result"]


class RobinhoodConnection:
    def __init__(self, call=rpc_call):
        self.call = call
        self.last_check = None
        self.report = {}

    def check(self):
        clock = time.monotonic()
        if self.last_check is not None and clock - self.last_check < 300:
            return dict(self.report)
        endpoint = os.getenv("ROBINHOOD_RPC_URL", "").strip()
        report = {"configured": bool(endpoint), "status": "NOT_CONFIGURED",
                  "checked_at": datetime.now(timezone.utc).isoformat(),
                  "expected_chain_id": CHAIN_ID, "live_execution_ready": False,
                  "execution_blockers": ["EVM_SIGNER_NOT_IMPLEMENTED",
                                         "SWAP_EXECUTOR_NOT_IMPLEMENTED",
                                         "LIVE_PROMOTION_NOT_VERIFIED"]}
        if endpoint:
            try:
                url = urlsplit(endpoint)
                if url.scheme != "https" or not url.hostname or url.username or url.password:
                    raise ValueError("invalid endpoint")
                chain = int(self.call(endpoint, "eth_chainId", []), 16)
                report["chain_id"] = chain
                if chain != CHAIN_ID:
                    report["status"] = "WRONG_CHAIN"
                else:
                    block = self.call(endpoint, "eth_getBlockByNumber", ["latest", False])
                    stamp = int(block["timestamp"], 16)
                    age = time.time() - stamp
                    report.update(block_number=int(block["number"], 16),
                                  block_age_seconds=round(age, 1),
                                  status="READY" if -30 <= age <= 120 else "STALE_BLOCK")
            except Exception as exc:
                # Provider errors often contain the complete credential-bearing URL.
                report.update(status="RPC_ERROR", error_type=type(exc).__name__)
        self.last_check, self.report = clock, report
        return dict(report)
