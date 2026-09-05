"""Explicitly approved EVM transactions with durable, single-wallet recovery.

Not a strategy admission policy: approvals and target allowlists must be supplied
by a trusted operator/policy. Never accept these inputs from a public endpoint.
"""
import json
import os
import stat
import sqlite3
from eth_account import Account
from eth_utils import keccak
from .robinhood_signer import SigningRejected, transaction_digest


class ExecutionJournal:
    def __init__(self, path):
        fd=os.open(path, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077:
                raise SigningRejected('journal must be owner-only')
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS transactions (
            intent TEXT PRIMARY KEY, wallet TEXT NOT NULL, digest TEXT NOT NULL,
            nonce INTEGER NOT NULL, hash TEXT NOT NULL, raw TEXT NOT NULL,
            state TEXT NOT NULL, receipt TEXT,
            UNIQUE(wallet, nonce))''')
        self.db.commit()

    def get(self, intent):
        r=self.db.execute('SELECT * FROM transactions WHERE intent=?',(intent,)).fetchone()
        return dict(r) if r else None


class ApprovedExecutor:
    def __init__(self, rpc, signer, journal, wallet, allowed_targets):
        self.rpc, self.signer, self.journal = rpc, signer, journal
        self.wallet = wallet.lower()
        self.allowed_targets = {a.lower() for a in allowed_targets}

    def _chain(self):
        if int(self.rpc('eth_chainId',[]),16) != 4663:
            raise SigningRejected('wrong RPC chain')

    def execute(self, intent, tx, approval):
        """Persist before submission; a timeout never authorizes a new nonce."""
        self._chain()
        existing=self.journal.get(intent)
        if existing:
            if existing['digest'] != transaction_digest(tx) or existing['wallet'] != self.wallet:
                raise SigningRejected('intent reuse with different transaction')
            return self.reconcile(intent)
        if tx.get('to','').lower() not in self.allowed_targets:
            raise SigningRejected('target not allowed')
        if self.rpc('eth_getCode',[tx['to'],'latest']) in ('0x','0x0',None):
            raise SigningRejected('target has no code')
        call={k:(hex(v) if type(v) is int else v) for k,v in tx.items()}
        call['from']=self.wallet
        self.rpc('eth_call',[call,'pending'])
        estimate=int(self.rpc('eth_estimateGas',[call]),16)
        if estimate > tx['gas']:
            raise SigningRejected('gas estimate exceeds approved limit')
        balance=int(self.rpc('eth_getBalance',[self.wallet,'pending']),16)
        if balance < tx['value']+tx['gas']*tx['maxFeePerGas']:
            raise SigningRejected('insufficient native balance including fee cap')
        db=self.journal.db
        try:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM transactions WHERE wallet=? AND state NOT IN ('CONFIRMED','REVERTED')",(self.wallet,)).fetchone():
                raise SigningRejected('unresolved transaction blocks new nonce')
            nonce=int(self.rpc('eth_getTransactionCount',[self.wallet,'pending']),16)
            if nonce != tx['nonce']:
                raise SigningRejected('approved nonce is not current')
            signed=self.signer.sign(tx,approval)
            raw='0x'+bytes(signed.raw_transaction).hex()
            if Account.recover_transaction(raw).lower() != self.wallet:
                raise SigningRejected('recovered signing wallet mismatch')
            txhash='0x'+keccak(bytes(signed.raw_transaction)).hex()
            db.execute('INSERT INTO transactions VALUES (?,?,?,?,?,?,?,NULL)',
                       (intent,self.wallet,transaction_digest(tx),nonce,txhash,raw,'SIGNED'))
            db.commit()
        except Exception:
            db.rollback()
            raise
        try:
            result=self.rpc('eth_sendRawTransaction',[raw])
            state='SUBMITTED' if str(result).lower()==txhash else 'UNKNOWN'
        except Exception:
            state='UNKNOWN'
        with db:
            db.execute('UPDATE transactions SET state=? WHERE intent=?',(state,intent))
        return self.reconcile(intent)

    def reconcile(self, intent):
        self._chain()
        row=self.journal.get(intent)
        if not row or row['wallet'] != self.wallet:
            raise SigningRejected('unknown intent')
        receipt=self.rpc('eth_getTransactionReceipt',[row['hash']])
        state=row['state']
        if receipt:
            if str(receipt.get('transactionHash','')).lower()!=row['hash'] or str(receipt.get('from','')).lower()!=self.wallet:
                raise SigningRejected('receipt identity mismatch')
            block=self.rpc('eth_getBlockByNumber',[receipt['blockNumber'],False])
            if block['hash'].lower()!=receipt['blockHash'].lower():
                raise SigningRejected('receipt block not canonical')
            head=int(self.rpc('eth_blockNumber',[]),16)
            if head-int(receipt['blockNumber'],16)+1 >= 12:
                status=int(receipt['status'],16)
                if status not in (0,1): raise SigningRejected('invalid receipt status')
                state='CONFIRMED' if status==1 else 'REVERTED'
                with self.journal.db:
                    self.journal.db.execute('UPDATE transactions SET state=?, receipt=? WHERE intent=?',
                                           (state,json.dumps(receipt),intent))
        # Never leak signed raw transactions through status/reporting.
        return {'intent':intent,'transaction_hash':row['hash'],'state':state,
                'receipt':receipt, 'trade_fill_verified':False}
