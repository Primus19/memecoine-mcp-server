import tempfile
import time
import unittest
from eth_account import Account
from eth_utils import keccak
from app.robinhood_execution import ApprovedExecutor, ExecutionJournal
from app.robinhood_signer import Approval, SigningRejected, transaction_digest
from tests.test_robinhood_signer import SignerTests

class ExecutionTests(SignerTests):
    def setUp(self):
        super().setUp()
        self.journal=ExecutionJournal(self.temp.name+'/journal.sqlite')
        self.addCleanup(self.journal.db.close)
        self.sends=0; self.receipt=None; self.timeout=False
        self.executor=ApprovedExecutor(self.rpc,self.signer,self.journal,self.address,[self.address])

    def rpc(self,method,params):
        if method=='eth_chainId': return hex(4663)
        if method=='eth_getCode': return '0x6000'
        if method=='eth_call': return '0x'
        if method=='eth_estimateGas': return hex(21000)
        if method=='eth_getBalance': return hex(10**18)
        if method=='eth_getTransactionCount': return '0x0'
        if method=='eth_sendRawTransaction':
            self.sends+=1
            if self.timeout: raise TimeoutError()
            return '0x'+keccak(bytes.fromhex(params[0][2:])).hex()
        if method=='eth_getTransactionReceipt': return self.receipt
        if method=='eth_getBlockByNumber': return {'hash':'0xabc'}
        if method=='eth_blockNumber': return hex(30)
        raise AssertionError(method)

    def test_success_then_restart_reconcile(self):
        result=self.executor.execute('buy',self.tx,self.approval)
        self.assertEqual(result['state'],'SUBMITTED')
        self.receipt=dict(transactionHash=result['transaction_hash'],
                          **{'from':self.address},blockNumber='0x1',blockHash='0xabc',status='0x1')
        reopened=ExecutionJournal(self.temp.name+'/journal.sqlite')
        try:
            executor=ApprovedExecutor(self.rpc,self.signer,reopened,self.address,[self.address])
            self.assertEqual(executor.execute('buy',self.tx,self.approval)['state'],'CONFIRMED')
            self.assertEqual(self.sends,1)
        finally: reopened.db.close()

    def test_timeout_blocks_duplicate_nonce(self):
        self.timeout=True
        self.assertEqual(self.executor.execute('buy',self.tx,self.approval)['state'],'UNKNOWN')
        self.executor.execute('buy',self.tx,self.approval)
        self.assertEqual(self.sends,1)
        with self.assertRaises(SigningRejected): self.executor.execute('other',self.tx,self.approval)

    def test_revert(self):
        result=self.executor.execute('buy',self.tx,self.approval)
        self.receipt=dict(transactionHash=result['transaction_hash'],
                          **{'from':self.address},blockNumber='0x1',blockHash='0xabc',status='0x0')
        self.assertEqual(self.executor.reconcile('buy')['state'],'REVERTED')

    def test_wrong_receipt(self):
        self.executor.execute('buy',self.tx,self.approval)
        self.receipt={'transactionHash':'wrong'}
        with self.assertRaises(SigningRejected): self.executor.reconcile('buy')

    def test_target_rejected(self):
        self.executor.allowed_targets=set()
        with self.assertRaises(SigningRejected): self.executor.execute('buy',self.tx,self.approval)
        self.assertEqual(self.sends,0)
