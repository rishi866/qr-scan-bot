"""Hierarchical-deterministic deposit addresses (BIP-44, ``m/44'/60'/<account>'/0/<index>``).

Every seller gets a unique BSC address whose derivation index is the seller's ``user_id``, so the
whole mapping can be recovered from the seed alone even if the database is lost.

Two ways to run (see docs/PAYMENTS.md):
* **watch-only (recommended)** - the server only knows the *account xpub*: it can derive deposit
  addresses but can never move funds. Sweep with ``python -m app.cli sweep`` from a machine that
  holds the mnemonic.
* **mnemonic on the server** - additionally allows signing (sweeping) on the server.
"""

from __future__ import annotations

from bip_utils import (
    Bip39MnemonicGenerator,
    Bip39MnemonicValidator,
    Bip39SeedGenerator,
    Bip39WordsNum,
    Bip44,
    Bip44Changes,
    Bip44Coins,
)

from app.config import get_settings


class HDError(Exception):
    pass


class HDWallet:
    def __init__(self, *, xpub: str = "", mnemonic: str = "", account: int = 0):
        if not xpub and not mnemonic:
            raise HDError("set HD_XPUB (watch-only, recommended) or HD_MNEMONIC")
        self._mnemonic = mnemonic.strip()
        self._account_index = account
        if self._mnemonic:
            if not Bip39MnemonicValidator().IsValid(self._mnemonic):
                raise HDError("HD_MNEMONIC is not a valid BIP-39 mnemonic")
            seed = Bip39SeedGenerator(self._mnemonic).Generate()
            self._account = Bip44.FromSeed(seed, Bip44Coins.ETHEREUM).Purpose().Coin().Account(account)
            derived_xpub = self._account.PublicKey().ToExtended()
            if xpub and xpub != derived_xpub:
                raise HDError("HD_XPUB does not belong to HD_MNEMONIC (check HD_ACCOUNT)")
            self._xpub = derived_xpub
        else:
            try:
                self._account = Bip44.FromExtendedKey(xpub, Bip44Coins.ETHEREUM)
            except Exception as exc:
                raise HDError("HD_XPUB is not a valid extended public key") from exc
            if not self._account.IsPublicOnly():
                raise HDError("HD_XPUB must be a PUBLIC key (xpub...). Never put an xprv on the server.")
            self._xpub = xpub

    @property
    def xpub(self) -> str:
        return self._xpub

    @property
    def can_sign(self) -> bool:
        return bool(self._mnemonic)

    def _node(self, index: int):
        if index < 0 or index >= 2**31:
            raise HDError("derivation index out of range")
        return self._account.Change(Bip44Changes.CHAIN_EXT).AddressIndex(index)

    def address(self, index: int) -> str:
        return self._node(index).PublicKey().ToAddress()

    def private_key(self, index: int) -> str:
        if not self.can_sign:
            raise HDError("this wallet is watch-only (no mnemonic available)")
        return "0x" + self._node(index).PrivateKey().Raw().ToHex()


def generate_mnemonic(words: int = 12) -> str:
    count = {12: Bip39WordsNum.WORDS_NUM_12, 24: Bip39WordsNum.WORDS_NUM_24}.get(words)
    if count is None:
        raise HDError("words must be 12 or 24")
    return str(Bip39MnemonicGenerator().FromWordsNumber(count))


def from_settings() -> HDWallet | None:
    """The wallet configured through the environment, or ``None`` when deposits are not set up."""
    settings = get_settings()
    xpub = settings.hd_xpub.strip()
    mnemonic = settings.hd_mnemonic.get_secret_value().strip()
    if not xpub and not mnemonic:
        return None
    return HDWallet(xpub=xpub, mnemonic=mnemonic)
