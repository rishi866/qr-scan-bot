"""BNB Smart Chain (BEP-20) access through web3.py.

:class:`BscClient` is a thin *blocking* wrapper around ``Web3``; :class:`AsyncBsc` runs its methods
in worker threads so the bot's event loop never blocks on RPC calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import requests
from web3 import Web3
from web3.exceptions import ProviderConnectionError, RequestTimedOut, TimeExhausted, TransactionNotFound

from app.config import Settings, get_settings

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # Transfer(address,address,uint256)

ERC20_ABI: list[dict[str, Any]] = [
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "_owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]

DEFAULT_MAX_GAS_PRICE_WEI = 10 * 10**9  # BSC is normally well below 5 gwei; refuse to overpay by accident
ADDRESS_BATCH = 100  # addresses per eth_getLogs topic filter


class ChainError(Exception):
    pass


class PreBroadcastError(ChainError):
    """Raised when a transaction was definitely *not* broadcast (safe to retry later)."""


# Errors after which we cannot know whether the node accepted the transaction.
_AMBIGUOUS_SEND_ERRORS = (
    requests.exceptions.RequestException,
    RequestTimedOut,
    ProviderConnectionError,
    TimeExhausted,
    TimeoutError,
    ConnectionError,
    OSError,
)


@dataclass(frozen=True)
class TransferLog:
    tx_hash: str
    log_index: int
    block_number: int
    from_address: str
    to_address: str
    value_raw: int


@dataclass(frozen=True)
class Receipt:
    status: int
    block_number: int


def is_valid_address(address: str) -> bool:
    """A usable EVM address: 0x + 40 hex, and if it is mixed-case its EIP-55 checksum must be right."""
    if not isinstance(address, str) or not Web3.is_address(address):
        return False
    body = address[2:]
    if body.lower() == body or body.upper() == body:
        return True
    return Web3.is_checksum_address(address)


def to_checksum(address: str) -> str:
    if not is_valid_address(address):
        raise ChainError("invalid address")
    return Web3.to_checksum_address(address)


def _topic_address(address: str) -> str:
    return "0x" + "00" * 12 + address[2:].lower()


class BscClient:
    def __init__(
        self,
        w3: Web3,
        token_address: str,
        decimals: int,
        chain_id: int | None = None,
        max_gas_price_wei: int = DEFAULT_MAX_GAS_PRICE_WEI,
    ):
        self.w3 = w3
        self.token_address = Web3.to_checksum_address(token_address)
        self.decimals = decimals
        self.expected_chain_id = chain_id
        self.max_gas_price_wei = max_gas_price_wei
        self.token = w3.eth.contract(address=self.token_address, abi=ERC20_ABI)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> BscClient:
        settings = settings or get_settings()
        w3 = Web3(Web3.HTTPProvider(settings.bsc_rpc_url, request_kwargs={"timeout": 20}))
        return cls(w3, settings.token_contract, settings.token_decimals, chain_id=settings.bsc_chain_id)

    # ── reads ───────────────────────────────────────────────────────────────
    def check_chain(self) -> int:
        """Refuse to run against the wrong network (we would sign transactions for it)."""
        actual = self.w3.eth.chain_id
        if self.expected_chain_id is not None and actual != self.expected_chain_id:
            raise ChainError(f"RPC reports chain id {actual}, expected {self.expected_chain_id}")
        return actual

    def latest_block(self) -> int:
        return self.w3.eth.block_number

    def token_balance(self, address: str) -> int:
        return self.token.functions.balanceOf(Web3.to_checksum_address(address)).call()

    def native_balance(self, address: str) -> int:
        return self.w3.eth.get_balance(Web3.to_checksum_address(address))

    def get_transfers(self, from_block: int, to_block: int, to_addresses: list[str]) -> list[TransferLog]:
        """ERC-20 ``Transfer`` events of our token whose recipient is one of ``to_addresses``."""
        found: list[TransferLog] = []
        for i in range(0, len(to_addresses), ADDRESS_BATCH):
            batch = to_addresses[i : i + ADDRESS_BATCH]
            logs = self.w3.eth.get_logs(
                {
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": self.token_address,
                    "topics": [TRANSFER_TOPIC, None, [_topic_address(a) for a in batch]],
                }
            )
            for log in logs:
                topics = log["topics"]
                if len(topics) != 3 or log.get("removed"):  # 4 topics == ERC-721 Transfer
                    continue
                found.append(
                    TransferLog(
                        tx_hash=Web3.to_hex(log["transactionHash"]),
                        log_index=int(log["logIndex"]),
                        block_number=int(log["blockNumber"]),
                        from_address=Web3.to_checksum_address("0x" + Web3.to_hex(topics[1])[-40:]),
                        to_address=Web3.to_checksum_address("0x" + Web3.to_hex(topics[2])[-40:]),
                        value_raw=int.from_bytes(bytes(log["data"]), "big"),
                    )
                )
        found.sort(key=lambda t: (t.block_number, t.log_index))
        return found

    def receipt(self, tx_hash: str) -> Receipt | None:
        try:
            r = self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None
        return Receipt(status=int(r["status"]), block_number=int(r["blockNumber"]))

    # ── writes ──────────────────────────────────────────────────────────────
    def _gas_price(self) -> int:
        price = int(self.w3.eth.gas_price)
        if price > self.max_gas_price_wei:
            raise ChainError(f"gas price {price} wei is above the safety cap {self.max_gas_price_wei}")
        return price

    def _chain_id(self) -> int:
        return self.check_chain()

    def _broadcast(self, signed) -> str:
        try:
            return Web3.to_hex(self.w3.eth.send_raw_transaction(signed.raw_transaction))
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:  # rate limited => not processed
                raise PreBroadcastError("RPC rate limit hit") from exc
            raise
        except _AMBIGUOUS_SEND_ERRORS:
            raise  # unknown whether the node got it - the caller must NOT blindly retry
        except Exception as exc:  # the node answered with an error => nothing was broadcast
            raise PreBroadcastError(f"node rejected the transaction: {exc}") from exc

    def send_token(self, private_key: str, to: str, amount_raw: int, gas_limit: int | None = None) -> str:
        """Sign and broadcast ``token.transfer(to, amount_raw)``; returns the tx hash.

        ``gas_limit`` skips node-side estimation (used for sweeps, where the sender holds just enough BNB).
        :class:`PreBroadcastError` means nothing was sent; any other exception is ambiguous.
        """
        if amount_raw <= 0:
            raise PreBroadcastError("amount must be positive")
        try:
            account = self.w3.eth.account.from_key(private_key)
            params: dict[str, Any] = {
                "from": account.address,
                "nonce": self.w3.eth.get_transaction_count(account.address, "pending"),
                "chainId": self._chain_id(),
                "gasPrice": self._gas_price(),
            }
            if gas_limit is not None:
                params["gas"] = gas_limit
            tx = self.token.functions.transfer(to_checksum(to), amount_raw).build_transaction(params)
            if gas_limit is None:
                tx["gas"] = int(tx["gas"] * 1.25)
            signed = account.sign_transaction(tx)
        except _AMBIGUOUS_SEND_ERRORS:
            raise PreBroadcastError("RPC unreachable while preparing the transaction") from None
        except PreBroadcastError:
            raise
        except Exception as exc:
            raise PreBroadcastError(f"could not prepare the transaction: {exc}") from exc
        return self._broadcast(signed)

    def send_native(self, private_key: str, to: str, amount_wei: int) -> str:
        try:
            account = self.w3.eth.account.from_key(private_key)
            tx = {
                "from": account.address,
                "to": to_checksum(to),
                "value": amount_wei,
                "nonce": self.w3.eth.get_transaction_count(account.address, "pending"),
                "chainId": self._chain_id(),
                "gasPrice": self._gas_price(),
                "gas": 21000,
            }
            signed = account.sign_transaction(tx)
        except _AMBIGUOUS_SEND_ERRORS:
            raise PreBroadcastError("RPC unreachable while preparing the transaction") from None
        except PreBroadcastError:
            raise
        except Exception as exc:
            raise PreBroadcastError(f"could not prepare the transaction: {exc}") from exc
        return self._broadcast(signed)

    def gas_price(self) -> int:
        return self._gas_price()

    def wait_for_receipt(self, tx_hash: str, timeout: float = 120) -> Receipt:
        r = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        return Receipt(status=int(r["status"]), block_number=int(r["blockNumber"]))


class AsyncBsc:
    """Async facade: every call runs ``BscClient`` in a worker thread."""

    def __init__(self, client: BscClient):
        self.client = client

    @property
    def decimals(self) -> int:
        return self.client.decimals

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def check_chain(self) -> int:
        return await self._run(self.client.check_chain)

    async def latest_block(self) -> int:
        return await self._run(self.client.latest_block)

    async def token_balance(self, address: str) -> int:
        return await self._run(self.client.token_balance, address)

    async def native_balance(self, address: str) -> int:
        return await self._run(self.client.native_balance, address)

    async def get_transfers(self, from_block: int, to_block: int, to_addresses: list[str]) -> list[TransferLog]:
        return await self._run(self.client.get_transfers, from_block, to_block, to_addresses)

    async def receipt(self, tx_hash: str) -> Receipt | None:
        return await self._run(self.client.receipt, tx_hash)

    async def send_token(self, private_key: str, to: str, amount_raw: int, gas_limit: int | None = None) -> str:
        return await self._run(self.client.send_token, private_key, to, amount_raw, gas_limit)

    async def gas_price(self) -> int:
        return await self._run(self.client.gas_price)

    async def send_native(self, private_key: str, to: str, amount_wei: int) -> str:
        return await self._run(self.client.send_native, private_key, to, amount_wei)

    async def wait_for_receipt(self, tx_hash: str, wait_seconds: float = 120) -> Receipt:
        return await self._run(self.client.wait_for_receipt, tx_hash, wait_seconds)


def async_client_from_settings() -> AsyncBsc:
    return AsyncBsc(BscClient.from_settings())
