"""Conversation states stored in ``users.state`` (the bot's next expected input)."""

from __future__ import annotations

ONB_COUNTRY_TEXT = "onb_country"  # typing a country name (onboarding / time-zone change)
ONB_CONFIRM = "onb_confirm"  # detected time zone waiting for "Is this correct?"; data: {tz, country, purpose}
ONB_PICK = "onb_pick"  # choosing a country / time zone from buttons; data: {options, purpose, country}
AWAITING_URL = "awaiting_url"  # seller after /send (short TTL)
CONFIRM_SEND = "confirm_send"  # seller sees "Active user: user1 - Send URL?"
AWAITING_PROOF = "awaiting_proof"  # scanner must upload a screenshot
DISPUTE_NOTE = "dispute_note"  # seller may add one optional note
AWAITING_ADDRESS = "awaiting_address"  # data: {"method": "bep20"|"binance"}
AWAITING_WD_AMOUNT = "awaiting_wd_amount"  # data: {"method", "address"}
CONFIRM_WD = "confirm_wd"  # data: {"method", "address", "amount"}
AWAITING_DEP_AMOUNT = "awaiting_dep_amount"
AWAITING_DEP_REF = "awaiting_dep_ref"  # data: {"amount"}

# states whose timeout should be announced to the user
ANNOUNCE_TIMEOUT = {AWAITING_URL, CONFIRM_SEND}
