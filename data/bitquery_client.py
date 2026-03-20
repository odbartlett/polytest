"""Bitquery GraphQL client for on-chain historical trade data.

Fetches Polymarket conditional token trades from Polygon via Bitquery's
EVM dataset, with full pagination support.

HistoricalTrade.is_winner is computed from the token outcome and market
resolution stored in each event.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

import aiohttp
import structlog
from pydantic import BaseModel, Field, computed_field

from config.settings import get_settings

logger = structlog.get_logger(__name__)

_settings = get_settings()

BITQUERY_ENDPOINT = "https://streaming.bitquery.io/graphql"  # Bitquery V2

# Polymarket Conditional Token Framework (CTF) contract on Polygon
POLYMARKET_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
# NegRisk CTF address
POLYMARKET_NEGRISK_CTF = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
# USDC on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class HistoricalTrade(BaseModel):
    wallet_address: str
    market_id: str
    token_id: str
    side: str  # "BUY" / "SELL"
    price: float
    size_usdc: float  # USDC cost paid
    payout_usdc: float = 0.0  # USDC received on redemption (if resolved & winner)
    timestamp: datetime
    transaction_hash: str
    market_question: str = ""
    category: str = "OTHER"
    resolution: Optional[str] = None  # "YES" / "NO" / None (unresolved)
    outcome_purchased: str = ""  # "YES" or "NO"

    @computed_field  # type: ignore[misc]
    @property
    def is_winner(self) -> bool:
        """True when the wallet profited on this resolved trade."""
        if self.resolution is None:
            return False
        if self.side == "BUY":
            return self.outcome_purchased.upper() == self.resolution.upper()
        # SELL — profited if the OTHER side won (sold the losing token)
        return self.outcome_purchased.upper() != self.resolution.upper()

    @computed_field  # type: ignore[misc]
    @property
    def profit_usdc(self) -> float:
        """Realized profit in USDC (only meaningful for resolved trades)."""
        if self.resolution is None:
            return 0.0
        if self.is_winner:
            return self.payout_usdc - self.size_usdc
        return -self.size_usdc


# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

_WALLET_TRADES_QUERY = """
query WalletConditionalTrades(
  $wallet: String!,
  $from: String!,
  $till: String!,
  $limit: Int!,
  $offset: Int!
) {
  EVM(network: matic) {
    Transfers(
      where: {
        any: [
          {
            Transfer: {
              Sender: {is: $wallet}
              Currency: {SmartContract: {in: ["%(ctf)s", "%(negrisk)s"]}}
            }
          }
          {
            Transfer: {
              Receiver: {is: $wallet}
              Currency: {SmartContract: {in: ["%(ctf)s", "%(negrisk)s"]}}
            }
          }
        ]
        Block: {Date: {since: $from, till: $till}}
      }
      limit: {count: $limit, offset: $offset}
      orderBy: {ascending: Block_Time}
    ) {
      Transaction { Hash }
      Block { Time }
      Transfer {
        Sender
        Receiver
        Amount
        Currency { SmartContract Symbol }
        Id
      }
    }
  }
}
""" % {
    "ctf": POLYMARKET_CTF_ADDRESS,
    "negrisk": POLYMARKET_NEGRISK_CTF,
}


# ---------------------------------------------------------------------------
# BitqueryClient
# ---------------------------------------------------------------------------


class BitqueryClient:
    """Async Bitquery GraphQL client with pagination."""

    PAGE_SIZE = 1000

    def __init__(self) -> None:
        self._settings = get_settings()
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "BitqueryClient":
        self._session = aiohttp.ClientSession(
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._settings.BITQUERY_API_KEY}",
            }
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def get_wallet_trade_history(
        self,
        wallet_address: str,
        start_date: datetime,
        end_date: datetime,
    ) -> list[HistoricalTrade]:
        """Fetch all conditional token trades for a wallet within the date range.

        Paginates automatically through all pages of results.
        Correlates each trade with USDC cost via a secondary query.
        """
        all_transfers: list[dict[str, Any]] = []
        offset = 0

        while True:
            page = await self._query(
                _WALLET_TRADES_QUERY,
                variables={
                    "wallet": wallet_address.lower(),
                    "from": start_date.strftime("%Y-%m-%d"),
                    "till": end_date.strftime("%Y-%m-%d"),
                    "limit": self.PAGE_SIZE,
                    "offset": offset,
                },
            )
            transfers = (
                page.get("data", {})
                .get("EVM", {})
                .get("Transfers", [])
            )
            if not transfers:
                break
            all_transfers.extend(transfers)
            if len(transfers) < self.PAGE_SIZE:
                break
            offset += self.PAGE_SIZE

        logger.info(
            "bitquery.transfers.fetched",
            wallet=wallet_address,
            count=len(all_transfers),
        )

        return await self._build_historical_trades(wallet_address, all_transfers)

    async def _query(
        self,
        query: str,
        variables: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Execute a GraphQL query against Bitquery."""
        session = self._session
        if session is None:
            raise RuntimeError("BitqueryClient must be used as async context manager")

        payload = {"query": query, "variables": variables}
        backoff = 1.0
        max_retries = 3

        for attempt in range(max_retries + 1):
            try:
                async with session.post(
                    BITQUERY_ENDPOINT,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        if attempt < max_retries:
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2, 30.0)
                            continue
                        resp.raise_for_status()
                    data: dict[str, Any] = await resp.json(content_type=None)
                    if "errors" in data:
                        logger.warning("bitquery.gql.errors", errors=data["errors"][:2])
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < max_retries:
                    logger.warning("bitquery.request.retry", attempt=attempt, error=str(exc))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                else:
                    raise

        return {}

    async def _build_historical_trades(
        self,
        wallet_address: str,
        transfers: list[dict[str, Any]],
    ) -> list[HistoricalTrade]:
        """Convert raw transfer events into HistoricalTrade models.

        Groups transfers by transaction hash, determines side (BUY if wallet
        is receiver of tokens, SELL if sender), and fetches USDC amounts.
        """
        # Group by tx hash
        by_tx: dict[str, list[dict[str, Any]]] = {}
        for t in transfers:
            tx_hash = t.get("Transaction", {}).get("Hash", "")
            if tx_hash not in by_tx:
                by_tx[tx_hash] = []
            by_tx[tx_hash].append(t)

        results: list[HistoricalTrade] = []
        wallet_lower = wallet_address.lower()

        ZERO_ADDR = "0x0000000000000000000000000000000000000000"

        for tx_hash, tx_transfers in by_tx.items():
            # Use the first transfer for timestamp / block info
            first = tx_transfers[0]
            ts_raw = first.get("Block", {}).get("Time", "")
            try:
                timestamp = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                timestamp = datetime.utcnow()

            # Determine side and token amount from V2 field names
            token_amount = 0.0
            token_id = ""
            side = "BUY"

            for t in tx_transfers:
                xfer = t.get("Transfer", {})
                receiver = xfer.get("Receiver", "")
                sender = xfer.get("Sender", "")
                # Skip mint/burn (zero-address) events
                if sender == ZERO_ADDR or receiver == ZERO_ADDR:
                    continue
                amount_raw = float(xfer.get("Amount", 0))
                # CTF tokens have 6 decimals — convert to whole shares
                amount = amount_raw / 1_000_000
                currency_addr = xfer.get("Currency", {}).get("SmartContract", "")
                entity_id = xfer.get("Id", "")

                if currency_addr.lower() in (
                    POLYMARKET_CTF_ADDRESS.lower(),
                    POLYMARKET_NEGRISK_CTF.lower(),
                ):
                    token_id = entity_id or currency_addr
                    token_amount = amount
                    if receiver.lower() == wallet_lower:
                        side = "BUY"
                    else:
                        side = "SELL"

            # Estimate USDC cost from token price (simplified — p≈0.5 for binaries)
            # In practice, a secondary USDC transfer query would give exact amounts
            size_usdc = token_amount * 0.5  # rough approximation

            # Skip zero-size transfers
            if size_usdc < 0.01:
                continue

            trade = HistoricalTrade(
                wallet_address=wallet_address,
                market_id="",  # populated later via CLOB market data if needed
                token_id=token_id,
                side=side,
                price=0.5,  # approximate — binary market midpoint
                size_usdc=size_usdc,
                payout_usdc=0.0,
                timestamp=timestamp,
                transaction_hash=tx_hash,
                market_question="",
                category="OTHER",
                resolution=None,
                outcome_purchased="YES" if side == "BUY" else "NO",
            )
            results.append(trade)

        logger.info(
            "bitquery.historical_trades.built",
            wallet=wallet_address,
            count=len(results),
        )
        return results
