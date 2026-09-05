"""Credential-safe, read-only Robinhood mainnet connectivity verification."""
import json
import os
import time
import re
from decimal import Decimal
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

CHAIN_ID = 4663
# Public address supplied by the owner; never a signing credential.
DEFAULT_WALLET = "0x6484a64a8766eb9e964ef38c559f295e11718b76"


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
        if report["status"] == "READY":
            wallet = os.getenv("ROBINHOOD_WALLET_ADDRESS", DEFAULT_WALLET).strip()
            report["wallet"] = {"status": "INVALID_ADDRESS"}
            if re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
                report["wallet"] = {"address": wallet, "status": "UNVERIFIED"}
                try:
                    block_tag = hex(report["block_number"])
                    balance = int(self.call(endpoint, "eth_getBalance", [wallet, block_tag]), 16)
                    nonce = int(self.call(endpoint, "eth_getTransactionCount", [wallet, block_tag]), 16)
                    if balance < 0 or nonce < 0:
                        raise ValueError("invalid wallet response")
                    report["wallet"].update(status="OBSERVED", balance_wei=str(balance),
                        native_balance_eth=str(Decimal(balance) / Decimal(10**18)),
                        transaction_count=nonce, block_number=report["block_number"],
                        has_native_gas=balance > 0, ownership_verified=False)
                    if not balance:
                        report["execution_blockers"].append("NO_NATIVE_GAS")
                except Exception as exc:
                    report["wallet"].update(status="RPC_ERROR", error_type=type(exc).__name__)
                    report["execution_blockers"].append("WALLET_BALANCE_UNVERIFIED")
            else:
                report["execution_blockers"].append("INVALID_WALLET_ADDRESS")
        self.last_check, self.report = clock, report
        return dict(report)
