import os
import tempfile
import time
import unittest
import json
from eth_account import Account
from app.robinhood_signer import Approval, KeystoreSigner, SigningRejected, transaction_digest

class SignerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        account=Account.create()  # Disposable test key; never funded or printed.
        self.address=account.address
        key=self.temp.name+'/key.json'; password=self.temp.name+'/password'
        for path, value in [(key,json.dumps(Account.encrypt(account.key,'test-password',kdf='pbkdf2',iterations=1000))), (password,'test-password')]:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f: f.write(value)
        self.key=key
        self.signer=KeystoreSigner(key,password,self.address)
        self.tx=dict(type=2,chainId=4663,nonce=0,to=self.address,value=0,data='0x',gas=21000,maxFeePerGas=100,maxPriorityFeePerGas=1)
        self.approval=Approval(transaction_digest(self.tx),time.time()+60,2100000)

    def test_sign_and_recover(self):
        signed=self.signer.sign(self.tx,self.approval)
        self.assertEqual(Account.recover_transaction(signed.raw_transaction),self.address)

    def test_tampering(self):
        for field,value in [('value',1),('nonce',1),('data','0x12'),('chainId',1)]:
            with self.subTest(field=field),self.assertRaises(SigningRejected):
                self.signer.sign({**self.tx,field:value},self.approval)

    def test_expired_and_fee_cap(self):
        for approval in [Approval(self.approval.digest,0,2100000),Approval(self.approval.digest,time.time()+60,1)]:
            with self.assertRaises(SigningRejected): self.signer.sign(self.tx,approval)

    def test_identity(self):
        self.signer.expected_address='0x'+'1'*40
        with self.assertRaisesRegex(SigningRejected,'mismatch'): self.signer.sign(self.tx,self.approval)

    def test_permissions(self):
        os.chmod(self.key,0o644)
        with self.assertRaisesRegex(SigningRejected,'permissions'): self.signer.sign(self.tx,self.approval)
