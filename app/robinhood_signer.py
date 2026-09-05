"""Offline signing primitive. No HTTP endpoint, key import UI, or broadcast.

The approval digest must come from a trusted policy/owner approval channel,
not from the same untrusted request supplying a transaction. This module is
not a complete live executor and is intentionally not called by the worker.
"""
import hashlib
import hmac
import json
import math
import os
import stat
import time
from dataclasses import dataclass
from eth_account import Account


class SigningRejected(ValueError):
    pass


def transaction_digest(transaction):
    return hashlib.sha256(json.dumps(transaction, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Approval:
    digest: str
    expires_at: float
    max_network_fee_wei: int


class KeystoreSigner:
    def __init__(self, keystore_path, password_path, expected_address):
        self.keystore_path = keystore_path
        self.password_path = password_path
        self.expected_address = expected_address

    @staticmethod
    def _read_private_file(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                raise SigningRejected('credential file permissions must be owner-only')
            if info.st_size > 65536:
                raise SigningRejected('credential file too large')
            return file.read()

    def sign(self, transaction, approval):
        tx = dict(transaction)
        fields = {'type', 'chainId', 'nonce', 'to', 'value', 'data', 'gas',
                  'maxFeePerGas', 'maxPriorityFeePerGas'}
        if set(tx) != fields:
            raise SigningRejected('explicit EIP-1559 transaction fields required')
        if tx['type'] != 2 or tx['chainId'] != 4663:
            raise SigningRejected('Robinhood mainnet type-2 transactions only')
        for key in fields - {'to', 'data'}:
            if type(tx[key]) is not int or tx[key] < 0:
                raise SigningRejected('invalid integer transaction field')
        if (tx['gas'] <= 0 or tx['maxFeePerGas'] <= 0 or
                tx['maxPriorityFeePerGas'] > tx['maxFeePerGas']):
            raise SigningRejected('invalid gas parameters')
        if (type(approval.max_network_fee_wei) is not int or approval.max_network_fee_wei <= 0 or
                tx['gas'] * tx['maxFeePerGas'] > approval.max_network_fee_wei):
            raise SigningRejected('network fee exceeds approval')
        if not math.isfinite(approval.expires_at) or time.time() >= approval.expires_at:
            raise SigningRejected('approval expired')
        if not hmac.compare_digest(transaction_digest(tx), approval.digest):
            raise SigningRejected('transaction differs from approval')
        try:
            encrypted = self._read_private_file(self.keystore_path)
            password = self._read_private_file(self.password_path).rstrip('\r\n')
            account = Account.from_key(Account.decrypt(encrypted, password))
            if account.address.lower() != self.expected_address.lower():
                raise SigningRejected('signing wallet mismatch')
            return account.sign_transaction(tx)
        except SigningRejected:
            raise
        except Exception:
            # Do not propagate filenames, passwords, key data or provider errors.
            raise SigningRejected('keystore signing failed') from None
