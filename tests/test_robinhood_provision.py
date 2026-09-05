import json
import os
import tempfile
import unittest
from unittest.mock import patch
from eth_account import Account
from app.robinhood_provision import provision

class ProvisionTests(unittest.TestCase):
    def test_provision_and_unlock(self):
        account=Account.create()
        password='a-test-only-password'
        with tempfile.TemporaryDirectory() as directory:
            path=directory+'/key.json'
            with patch('app.robinhood_provision.getpass.getpass',side_effect=[account.key.hex(),password,password]):
                self.assertEqual(provision(path,account.address),account.address)
            self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
            with open(path) as f: encrypted=json.load(f)
            self.assertEqual(Account.from_key(Account.decrypt(encrypted,password)).address,account.address)
            self.assertNotIn(account.key.hex(),json.dumps(encrypted))

    def test_wrong_owner_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path=directory+'/key.json'; account=Account.create()
            with patch('app.robinhood_provision.getpass.getpass',side_effect=[account.key.hex(),'long-test-password','long-test-password']):
                with self.assertRaises(ValueError): provision(path,'0x'+'1'*40)
            self.assertFalse(os.path.exists(path))
