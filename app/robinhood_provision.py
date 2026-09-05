"""Run interactively in a trusted terminal; never supply keys on command line.

Creates only an encrypted keystore. The unlock password must be provisioned
separately through the deployment secret mechanism, never source control/chat.
"""
import argparse
import getpass
import json
import os
from eth_account import Account


def provision(path, expected_address):
    key=getpass.getpass('Wallet private key (hidden; never a recovery phrase): ')
    password=getpass.getpass('New keystore password (at least 16 characters): ')
    if len(password)<16 or password!=getpass.getpass('Confirm keystore password: '):
        raise ValueError('password requirements not met')
    account=Account.from_key(key)
    if account.address.lower()!=expected_address.lower():
        raise ValueError('wallet address mismatch; nothing saved')
    encrypted=json.dumps(Account.encrypt(account.key,password))
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:
        f.write(encrypted); f.flush(); os.fsync(f.fileno())
    return account.address


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--keystore',required=True)
    p.add_argument('--expected-address',required=True)
    args=p.parse_args()
    try:
        address=provision(args.keystore,args.expected_address)
        print('Encrypted keystore created for '+address)
    except Exception:
        raise SystemExit('Provisioning failed. No secret details are logged.') from None

if __name__=='__main__': main()
