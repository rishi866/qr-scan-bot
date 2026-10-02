"""An in-memory EVM (eth-tester) with a real ERC-20 contract compiled from Vyper.

Used to test the deposit watcher, payouts and the sweeper against actual EVM semantics instead of mocks.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import vyper
from eth_account import Account
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider

from app.chain.bsc import AsyncBsc, BscClient

TOKEN_SOURCE = """
# pragma version ^0.4.0
event Transfer:
    sender: indexed(address)
    receiver: indexed(address)
    value: uint256

decimals: public(uint8)
balanceOf: public(HashMap[address, uint256])

@deploy
def __init__(_decimals: uint8, _supply: uint256):
    self.decimals = _decimals
    self.balanceOf[msg.sender] = _supply
    log Transfer(sender=empty(address), receiver=msg.sender, value=_supply)

@external
def transfer(_to: address, _value: uint256) -> bool:
    self.balanceOf[msg.sender] -= _value
    self.balanceOf[_to] += _value
    log Transfer(sender=msg.sender, receiver=_to, value=_value)
    return True
"""

ONE = 10**18


@functools.lru_cache(maxsize=1)
def _compiled() -> dict:
    return vyper.compile_code(TOKEN_SOURCE, output_formats=["abi", "bytecode"])


@dataclass
class Env:
    w3: Web3
    token: object
    deployer: str
    client: BscClient
    chain: AsyncBsc

    def mine(self, n: int = 1) -> None:
        self.w3.provider.ethereum_tester.mine_blocks(n)

    def send_token(self, to: str, tokens: float | int, *, raw: int | None = None) -> str:
        amount = raw if raw is not None else int(tokens * ONE)
        h = self.token.functions.transfer(Web3.to_checksum_address(to), amount).transact({"from": self.deployer})
        self.w3.eth.wait_for_transaction_receipt(h)
        return Web3.to_hex(h)

    def send_bnb(self, to: str, wei: int) -> None:
        h = self.w3.eth.send_transaction({"from": self.deployer, "to": Web3.to_checksum_address(to), "value": wei})
        self.w3.eth.wait_for_transaction_receipt(h)

    def token_balance(self, address: str) -> float:
        return self.token.functions.balanceOf(Web3.to_checksum_address(address)).call() / ONE

    def new_funded_account(self, tokens: float = 0, bnb_wei: int = 10**18) -> Account:
        account = Account.create()
        if bnb_wei:
            self.send_bnb(account.address, bnb_wei)
        if tokens:
            self.send_token(account.address, tokens)
        return account


def make_env() -> Env:
    w3 = Web3(EthereumTesterProvider())
    deployer = w3.eth.accounts[0]
    out = _compiled()
    contract = w3.eth.contract(abi=out["abi"], bytecode=out["bytecode"])
    receipt = w3.eth.wait_for_transaction_receipt(contract.constructor(18, 10**9 * ONE).transact({"from": deployer}))
    token = w3.eth.contract(address=receipt.contractAddress, abi=out["abi"])
    client = BscClient(w3, token.address, 18, chain_id=None)
    env = Env(w3=w3, token=token, deployer=deployer, client=client, chain=AsyncBsc(client))
    env.mine(5)  # a realistic chain is longer than the confirmation depth
    return env
