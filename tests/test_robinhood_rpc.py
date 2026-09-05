import json
import time
import unittest
from unittest.mock import patch
from app.robinhood_rpc import RobinhoodConnection

class ConnectionTests(unittest.TestCase):
    @patch.dict('os.environ', {'ROBINHOOD_RPC_URL': 'https://rpc.example/secret'})
    def test_mainnet_and_cache(self):
        calls=[]
        def call(url, method, params):
            calls.append(method)
            return hex(4663) if method=='eth_chainId' else {'number':'0x10','timestamp':hex(int(time.time()))}
        c=RobinhoodConnection(call)
        self.assertEqual(c.check()['status'],'READY')
        self.assertFalse(c.check()['live_execution_ready'])
        self.assertEqual(len(calls),2)

    @patch.dict('os.environ', {'ROBINHOOD_RPC_URL': 'https://rpc.example/secret'})
    def test_wrong_network(self):
        self.assertEqual(RobinhoodConnection(lambda *args: '0x1').check()['status'],'WRONG_CHAIN')

    @patch.dict('os.environ', {'ROBINHOOD_RPC_URL': 'https://rpc.example/secret'})
    def test_secret_redacted(self):
        def fail(*args): raise RuntimeError('https://rpc.example/secret')
        report=RobinhoodConnection(fail).check()
        self.assertEqual(report['status'],'RPC_ERROR')
        self.assertNotIn('secret',json.dumps(report))

    @patch.dict('os.environ', {'ROBINHOOD_RPC_URL': 'https://rpc.example/secret'})
    def test_stale_block(self):
        def call(url, method, params):
            return hex(4663) if method=='eth_chainId' else {'number':'0x10','timestamp':'0x1'}
        self.assertEqual(RobinhoodConnection(call).check()['status'],'STALE_BLOCK')

    @patch.dict('os.environ', {'ROBINHOOD_RPC_URL': ''})
    def test_missing(self):
        self.assertEqual(RobinhoodConnection().check()['status'],'NOT_CONFIGURED')
