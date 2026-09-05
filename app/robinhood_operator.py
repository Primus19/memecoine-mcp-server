"""Trusted-terminal execution only. No web endpoint or autonomous strategy hook."""
import argparse
import json
import os
from .robinhood_rpc import rpc_call, DEFAULT_WALLET
from .robinhood_signer import Approval, KeystoreSigner
from .robinhood_execution import ApprovedExecutor, ExecutionJournal


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['execute','reconcile'])
    p.add_argument('--intent',required=True)
    p.add_argument('--transaction')
    p.add_argument('--approval')
    args=p.parse_args()
    try:
        wallet=os.getenv('ROBINHOOD_WALLET_ADDRESS',DEFAULT_WALLET)
        rpc=lambda method,params: rpc_call(os.environ['ROBINHOOD_RPC_URL'],method,params)
        signer=KeystoreSigner(os.getenv('ROBINHOOD_KEYSTORE_PATH',''),
                              os.getenv('ROBINHOOD_PASSWORD_PATH',''),wallet)
        targets=os.getenv('ROBINHOOD_ALLOWED_TARGETS','').split(',')
        executor=ApprovedExecutor(rpc,signer,ExecutionJournal(os.environ['ROBINHOOD_JOURNAL_PATH']),wallet,targets)
        if args.action=='execute':
            if os.getenv('ROBINHOOD_OPERATOR_EXECUTION_ENABLED')!='true':
                raise ValueError('operator execution disabled')
            with open(args.transaction) as f: tx=json.load(f)
            with open(args.approval) as f: approval=Approval(**json.load(f))
            result=executor.execute(args.intent,tx,approval)
        else:
            result=executor.reconcile(args.intent)
        print(json.dumps(result))
    except Exception:
        raise SystemExit('Operation failed; inspect configuration and reconcile the intent before retrying. Secrets are not logged.') from None

if __name__=='__main__': main()
