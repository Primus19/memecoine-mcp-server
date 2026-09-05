import io
import json
import unittest
from unittest.mock import patch
from app.robinhood_swap import quote

class QuoteTests(unittest.TestCase):
    def test_provider_response_and_fail_closed(self):
        wallet='0x'+'1'*40; sell='0x'+'2'*40; buy='0x'+'3'*40
        valid=dict(liquidityAvailable=True,issues=dict(simulationIncomplete=False),
                   sellToken=sell,buyToken=buy,sellAmount='5',minBuyAmount='2',transaction={})
        with patch('app.robinhood_swap.urlopen',return_value=io.StringIO(json.dumps(valid))):
            self.assertTrue(quote('test-api-key',wallet,sell,buy,5)['requires_trusted_approval'])
        for change in [dict(liquidityAvailable=False),dict(issues={}),dict(sellAmount='10'),dict(buyToken=sell),dict(issues=dict(simulationIncomplete=False,allowance={'actual':'0'}))]:
            with patch('app.robinhood_swap.urlopen',return_value=io.StringIO(json.dumps({**valid,**change}))):
                with self.assertRaises(ValueError): quote('test-api-key',wallet,sell,buy,5)
