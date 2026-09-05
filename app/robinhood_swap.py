"""0x firm-quote adapter. Returns a draft for trusted approval, never executes.

Source: https://docs.0x.org/docs/introduction/quickstart/swap-tokens-with-0x-swap-api
"""
import json
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def quote(api_key, wallet, sell_token, buy_token, sell_amount, slippage_bps=50):
    for address in (wallet,sell_token,buy_token):
        if not re.fullmatch(r'0x[0-9a-fA-F]{40}',address):
            raise ValueError('invalid address')
    if type(sell_amount) is not int or sell_amount<=0 or type(slippage_bps) is not int or not 0<=slippage_bps<=100:
        raise ValueError('invalid amount or slippage')
    if not api_key: raise ValueError('0x API credential missing')
    params=dict(chainId=4663,taker=wallet,recipient=wallet,sellToken=sell_token,
                buyToken=buy_token,sellAmount=str(sell_amount),slippageBps=slippage_bps)
    request=Request('https://api.0x.org/swap/allowance-holder/quote?'+urlencode(params),
                    headers={'0x-api-key':api_key,'0x-version':'v2'})
    try:
        with urlopen(request,timeout=10) as response: data=json.load(response)
    except Exception:
        raise ValueError('swap provider request failed') from None
    issues=data.get('issues') or {}
    if data.get('liquidityAvailable') is not True or issues.get('simulationIncomplete') is not False:
        raise ValueError('quote liquidity or simulation unverified')
    if issues.get('balance') or issues.get('allowance') or issues.get('invalidSourcesPassed'):
        raise ValueError('quote has unresolved balance, allowance or source issues')
    if (str(data.get('sellToken','')).lower()!=sell_token.lower() or
        str(data.get('buyToken','')).lower()!=buy_token.lower() or
        int(data.get('sellAmount',0))!=sell_amount or int(data.get('minBuyAmount',0))<=0):
        raise ValueError('quote identity or amount mismatch')
    return {'provider':'0x','chain_id':4663,'taker':wallet,'sell_token':sell_token,
            'buy_token':buy_token,'sell_amount':str(sell_amount),
            'minimum_buy_amount':str(data['minBuyAmount']),
            'transaction':data['transaction'],'received_at':time.time(),
            'requires_trusted_approval':True}
