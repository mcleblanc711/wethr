"""
Polymarket CLOB API trading — live order placement.

Uses the py-clob-client SDK for authenticated trading on Polymarket.
All orders go through the CLOB (Central Limit Order Book) which operates
on Polygon (chain ID 137) with USDC.e settlement and zero fees on
conditional token markets.

SAFETY:
- Live trading is OFF by default (WETHR_LIVE=0)
- Requires explicit WETHR_LIVE=1 environment variable
- All orders are logged before placement
- Dry-run mode logs what WOULD be placed without touching the API
- Position tracking prevents double-entry on the same bracket

Setup:
    1. Export your Polymarket API credentials:
       export POLYMARKET_API_KEY="..."
       export POLYMARKET_API_SECRET="..."
       export POLYMARKET_PASSPHRASE="..."
    
    2. Enable live trading:
       export WETHR_LIVE=1

    3. Ensure your Polymarket account has USDC.e on Polygon.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from . import config
from .sizing import PositionSize

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LIVE_TRADING_ENABLED = os.getenv("WETHR_LIVE", "0") == "1"
CHAIN_ID = 137  # Polygon

# Polymarket API credentials (from environment)
API_KEY = os.getenv("POLYMARKET_API_KEY", "")
API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
API_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")

# On-chain redemption (for auto-close after market resolves)
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
# Polymarket's ConditionalTokens framework (UMA-CTF) on Polygon
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
# USDC.e — collateral token for Polymarket conditional markets
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Binary outcome index sets: 0b01 = NO, 0b10 = YES — passing both redeems
# whichever side won (the loser pays out 0).
BINARY_INDEX_SETS = [1, 2]
# Parent collection is the zero hash for top-level (non-nested) conditions.
ZERO_BYTES32 = b"\x00" * 32

# Minimal ABI for ConditionalTokens.redeemPositions
_CTF_REDEEM_ABI = [{
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "indexSets", "type": "uint256[]"},
    ],
    "name": "redeemPositions",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}]


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderResult:
    """Result of an order placement attempt."""
    success: bool
    order_id: str = ""
    token_id: str = ""
    side: str = ""
    price: float = 0.0
    size: float = 0.0
    error: str = ""
    dry_run: bool = False

    def __str__(self) -> str:
        if self.dry_run:
            return f"[DRY RUN] {self.side} {self.size:.2f} @ {self.price:.2f}"
        if self.success:
            return f"Order {self.order_id}: {self.side} {self.size:.2f} @ {self.price:.2f}"
        return f"FAILED: {self.error}"


# ---------------------------------------------------------------------------
# Client wrapper
# ---------------------------------------------------------------------------

class TradingClient:
    """
    Wrapper around py-clob-client for Polymarket trading.
    
    Handles client initialization, order construction, and placement.
    Falls back to dry-run mode if credentials aren't configured or
    WETHR_LIVE isn't set.
    """

    def __init__(self, live: bool | None = None):
        self._live = live if live is not None else LIVE_TRADING_ENABLED
        self._client = None
        self._initialized = False

    @property
    def is_live(self) -> bool:
        return self._live and self._initialized

    def initialize(self) -> bool:
        """
        Initialize the CLOB client. Returns True if successful.
        
        Requires py-clob-client to be installed and credentials to be set.
        """
        if not self._live:
            log.info("Trading client in DRY RUN mode (WETHR_LIVE != 1)")
            return True

        if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
            log.warning(
                "Polymarket API credentials not set. "
                "Export POLYMARKET_API_KEY, POLYMARKET_API_SECRET, "
                "POLYMARKET_PASSPHRASE to enable live trading. "
                "Falling back to dry-run mode."
            )
            self._live = False
            return True

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=API_KEY,
                api_secret=API_SECRET,
                api_passphrase=API_PASSPHRASE,
            )

            self._client = ClobClient(
                host=config.CLOB_API_BASE,
                chain_id=CHAIN_ID,
                creds=creds,
            )
            self._initialized = True
            log.info("Polymarket CLOB client initialized (LIVE mode)")
            return True

        except ImportError:
            log.warning(
                "py-clob-client not installed. "
                "Install with: pip install py-clob-client. "
                "Falling back to dry-run mode."
            )
            self._live = False
            return True

        except Exception as e:
            log.error(f"Failed to initialize CLOB client: {e}")
            self._live = False
            return False

    def get_balance(self) -> float | None:
        """Get USDC.e balance on Polymarket. Returns None if not live."""
        if not self.is_live or not self._client:
            return None

        try:
            # py-clob-client doesn't have a direct balance method;
            # we'd need to query the Polygon chain or Polymarket's API
            # For now, return None — balance checking comes from the
            # Polymarket account API
            log.debug("Balance check not implemented yet — use Polymarket UI")
            return None
        except Exception as e:
            log.error(f"Balance check failed: {e}")
            return None

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
    ) -> OrderResult:
        """
        Place a limit order on Polymarket CLOB.
        
        Args:
            token_id: The CLOB token ID (from Gamma API market data)
            side: "YES" or "NO" (maps to BUY on the appropriate token)
            price: Limit price (0.01 - 0.99)
            size_usd: Total USD to spend
        
        Returns:
            OrderResult with success status and order details.
        """
        # Calculate number of contracts
        if price <= 0 or price >= 1:
            return OrderResult(
                success=False,
                error=f"Invalid price: {price}",
                token_id=token_id,
            )

        # Contracts = size / price (each contract costs `price` USDC)
        n_contracts = size_usd / price

        log.info(
            f"{'📤 LIVE' if self.is_live else '📝 DRY RUN'}: "
            f"{side} {n_contracts:.1f} contracts @ {price:.3f} "
            f"(${size_usd:.2f}), token={token_id[:16]}..."
        )

        # Dry run — just log
        if not self.is_live:
            return OrderResult(
                success=True,
                order_id=f"dry-{datetime.now(timezone.utc).strftime('%H%M%S')}",
                token_id=token_id,
                side=side,
                price=price,
                size=size_usd,
                dry_run=True,
            )

        # Live order
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            # For YES: we BUY the YES token
            # For NO: we BUY the NO token (which is SELL on the YES token
            # in Polymarket's CLOB). But py-clob-client handles this —
            # we specify the token_id and BUY side.
            #
            # Actually, Polymarket's CLOB has two tokens per market:
            # YES token and NO token. When we "buy NO", we're buying the
            # NO token. The token_id from Gamma API is the YES token.
            # For buying NO, we'd need the complementary token ID.
            #
            # For Phase 2, we'll only buy YES on brackets where
            # model_prob > market_prob. This simplifies things.

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=n_contracts,
                side=BUY,
                order_type=OrderType.GTC,  # Good-til-cancelled
            )

            # Build and sign the order
            signed_order = self._client.create_order(order_args)

            # Post to CLOB
            result = self._client.post_order(signed_order)

            order_id = result.get("orderID", "unknown")
            log.info(f"✅ Order placed: {order_id}")

            return OrderResult(
                success=True,
                order_id=order_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size_usd,
            )

        except Exception as e:
            log.error(f"❌ Order failed: {e}", exc_info=True)
            return OrderResult(
                success=False,
                error=str(e),
                token_id=token_id,
                side=side,
                price=price,
                size=size_usd,
            )

    def redeem_position(self, condition_id: str) -> bool:
        """
        Redeem a resolved Polymarket position on-chain.

        Calls ConditionalTokens.redeemPositions with both binary index sets,
        which converts winning outcome shares back to USDC.e. Losing shares
        pay out 0, so passing both is safe and idempotent.

        Returns True on success (or in dry-run). Returns False on failure.
        Requires `web3` to be installed and POLYMARKET_PRIVATE_KEY to be set.
        """
        if not condition_id:
            log.warning("redeem_position called with empty condition_id")
            return False

        cid_short = condition_id[:16]

        if not self.is_live:
            log.info(f"📝 DRY RUN: would redeem condition {cid_short}...")
            return True

        if not PRIVATE_KEY:
            log.warning(
                "POLYMARKET_PRIVATE_KEY not set — cannot redeem on-chain. "
                "Redemption will need to be done manually via Polymarket UI."
            )
            return False

        try:
            from web3 import Web3
        except ImportError:
            log.warning(
                "web3 not installed — cannot redeem on-chain. "
                "Install with: pip install web3"
            )
            return False

        try:
            w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
            if not w3.is_connected():
                log.error(f"Polygon RPC unreachable: {POLYGON_RPC_URL}")
                return False

            account = w3.eth.account.from_key(PRIVATE_KEY)
            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=_CTF_REDEEM_ABI,
            )

            # conditionId is bytes32 — strip 0x and convert
            cid_bytes = bytes.fromhex(condition_id.removeprefix("0x"))
            if len(cid_bytes) != 32:
                log.error(f"Invalid condition_id length: {condition_id}")
                return False

            tx = ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                ZERO_BYTES32,
                cid_bytes,
                BINARY_INDEX_SETS,
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "chainId": CHAIN_ID,
            })

            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            log.info(f"📤 Redeem submitted for {cid_short}... tx={tx_hash.hex()}")

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt.status == 1:
                log.info(f"✅ Redeem confirmed for {cid_short}...")
                return True
            log.error(f"❌ Redeem reverted for {cid_short}... tx={tx_hash.hex()}")
            return False

        except Exception as e:
            log.error(f"❌ Redeem failed for {cid_short}...: {e}", exc_info=True)
            return False

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if not self.is_live or not self._client:
            log.info(f"[DRY RUN] Would cancel order {order_id}")
            return True

        try:
            self._client.cancel(order_id)
            log.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            log.error(f"Cancel failed for {order_id}: {e}")
            return False

    def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        if not self.is_live or not self._client:
            return []

        try:
            orders = self._client.get_orders()
            return orders if isinstance(orders, list) else []
        except Exception as e:
            log.error(f"Failed to fetch open orders: {e}")
            return []


# ---------------------------------------------------------------------------
# Trade execution from PositionSize
# ---------------------------------------------------------------------------

def execute_trade(
    client: TradingClient,
    city: str,
    target_date: str,
    ps: PositionSize,
) -> OrderResult:
    """
    Execute a trade from a PositionSize calculation.
    
    Maps internal PositionSize to CLOB order params:
    - YES side: buy the YES token at market_prob
    - NO side: buy the NO token at (1 - market_prob)
    """
    bp = ps.bracket_prob
    bracket = bp.bracket

    if not ps.is_valid:
        return OrderResult(
            success=False,
            error=f"Invalid position: {ps.reason_skipped}",
        )

    # Determine which token to buy
    if ps.side == "YES":
        token_id = bracket.token_id
        if not token_id:
            return OrderResult(
                success=False,
                error="No YES token_id — market discovery may have failed",
            )
    else:
        # NO side: buy the NO token (complementary outcome)
        token_id = bracket.no_token_id
        if not token_id:
            return OrderResult(
                success=False,
                error="No NO token_id — market data incomplete for NO-side trading",
                side="NO",
            )

    log.info(
        f"Executing: {city} {target_date} {bracket.label} "
        f"{ps.side} @ {ps.entry_price:.3f} ${ps.capped_size_usd:.2f}"
    )

    return client.place_order(
        token_id=token_id,
        side=ps.side,
        price=ps.entry_price,
        size_usd=ps.capped_size_usd,
    )
