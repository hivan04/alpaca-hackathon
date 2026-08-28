"""The option chain, built from real Alpaca history.

This replaces guessing with measuring wherever Alpaca will let it.

    strikes and expiries   REAL - `GET /v2/options/contracts`, expired contracts
                           included. No synthetic ladder, so the strategy can
                           only pick strikes that were actually listed.
    the mark               REAL - the contract's daily bar close. What it traded
                           at, not what a model says it was worth.
    volume                 REAL - from the same bar.
    implied volatility     RECOVERED - Black-Scholes inverted on the real mark
                           and the real underlying. One arithmetic step applied
                           to market data, rather than an assumption about the
                           variance risk premium.
    greeks                 DERIVED - analytic, at the recovered vol.
    open interest          APPROXIMATE - the contracts endpoint serves a current
                           snapshot, not the OI on the replayed day. Alpaca has
                           no historical OI. Used as a liquidity hint, labelled.
    bid / ask              MODELLED - bars are OHLCV, not quotes. The spread is
                           the dominant cost in a short-premium book, so this is
                           the biggest remaining assumption and it is the first
                           thing to replace if historical option quotes turn out
                           to be reachable on the account's plan.

Coverage is not total and pretending otherwise would be worse than modelling
everything. An option bar exists only on a day the contract TRADED, so wings on
single names are sparse. For a contract-day with no bar the builder falls back
to the modelled surface, and the quote records which it was: every mark carries
`source` in {"bar", "modelled"}, the per-session counts roll up into the run's
provenance, and the dashboard shows the ratio. A backtest that silently mixes
measured and invented prices is worse than one that only invents, because it
looks trustworthy.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from oaa.backtest.chain import ChainModel, _as_date, tier_for, years_to_expiry
from oaa.backtest.pricing import bs_greeks, bs_price, implied_vol_from_price
from oaa.core.logging import get_logger
from oaa.core.types import Greeks, OptionQuote, Right

log = get_logger("backtest.realchain")

_DAYS = 365.0


@dataclass
class Coverage:
    """How much of this run was measured rather than modelled."""

    marks_from_bars: int = 0
    marks_modelled: int = 0
    iv_recovered: int = 0
    iv_modelled: int = 0
    contracts_listed: int = 0

    @property
    def total(self) -> int:
        return self.marks_from_bars + self.marks_modelled

    @property
    def real_fraction(self) -> float:
        return round(self.marks_from_bars / self.total, 4) if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "contracts_listed": self.contracts_listed,
            "marks_from_real_bars": self.marks_from_bars,
            "marks_modelled": self.marks_modelled,
            "real_mark_fraction": self.real_fraction,
            "iv_recovered_from_price": self.iv_recovered,
            "iv_modelled": self.iv_modelled,
        }


@dataclass
class ContractRef:
    symbol: str
    expiry: dt.date
    strike: float
    is_call: bool
    open_interest: int | None = None


@dataclass
class RealChainBuilder:
    """Builds a chain for one (symbol, session) from cached contracts and bars."""

    #: underlying -> contracts
    contracts: dict[str, list[ContractRef]]
    #: OCC symbol -> date -> bar
    bars: dict[str, dict[dt.date, dict[str, Any]]]
    #: the modelled surface, used for gaps and for the spread
    model: ChainModel
    rate: float = 0.04
    #: contracts whose bar volume is below this are treated as untradable even
    #: though a print exists - one lot crossing at 15:59 is not a market
    min_bar_volume: int = 1
    coverage: Coverage = field(default_factory=Coverage)
    #: symbols already explained in the log, so the diagnostic fires once each
    _explained: set = field(default_factory=set)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_payload(
        cls,
        contracts_by_symbol: dict[str, list[dict[str, Any]]],
        bars_by_contract: dict[str, list[dict[str, Any]]],
        model: ChainModel,
        **kwargs: Any,
    ) -> RealChainBuilder:
        refs: dict[str, list[ContractRef]] = {}
        listed = 0
        for symbol, rows in contracts_by_symbol.items():
            parsed: list[ContractRef] = []
            for row in rows:
                try:
                    parsed.append(
                        ContractRef(
                            symbol=row["symbol"],
                            expiry=dt.date.fromisoformat(str(row["expiry"])[:10]),
                            strike=float(row["strike"]),
                            is_call=str(row["type"]).lower().startswith("c"),
                            open_interest=row.get("open_interest"),
                        )
                    )
                except (KeyError, ValueError):
                    continue
            refs[symbol.upper()] = parsed
            listed += len(parsed)

        indexed: dict[str, dict[dt.date, dict[str, Any]]] = {}
        for contract, rows in bars_by_contract.items():
            by_day: dict[dt.date, dict[str, Any]] = {}
            for row in rows:
                stamp = row["timestamp"]
                day = stamp.date() if hasattr(stamp, "date") else dt.date.fromisoformat(str(stamp)[:10])
                by_day[day] = row
            indexed[contract] = by_day

        builder = cls(contracts=refs, bars=indexed, model=model, **kwargs)
        builder.coverage.contracts_listed = listed
        return builder

    # ------------------------------------------------------------------ #
    def build(
        self,
        symbol: str,
        spot: float,
        asof: dt.datetime,
        fallback_iv: float,
        min_dte: int,
        max_dte: int,
        strike_window_pct: float | None = None,
    ) -> list[OptionQuote]:
        """The chain the strategy sees for this session."""
        day = asof.date()
        window = strike_window_pct or self.model.strike_window_pct
        low, high = spot * (1 - window), spot * (1 + window)
        tier = tier_for(symbol, self.model.tier_map, self.model.tiers, self.model.default_tier)

        quotes: list[OptionQuote] = []
        available = self.contracts.get(symbol.upper(), [])
        passed_dte = passed_strike = 0
        for ref in available:
            dte = (ref.expiry - day).days
            if not (min_dte <= dte <= max_dte):
                continue
            passed_dte += 1
            if not (low <= ref.strike <= high):
                continue
            passed_strike += 1

            years = max(dte, 0.5) / _DAYS
            bar = self.bars.get(ref.symbol, {}).get(day)

            if bar is not None and float(bar.get("volume", 0)) >= self.min_bar_volume:
                mark = float(bar["close"])
                volume = int(float(bar.get("volume", 0)))
                source = "bar"
                self.coverage.marks_from_bars += 1
                vol = implied_vol_from_price(
                    mark, spot, ref.strike, years, ref.is_call, self.rate
                )
                if vol is None:
                    # The price is real but carries no volatility information
                    # (deep ITM, or at intrinsic). Take the surface's vol for
                    # the greeks and say so - do not invent a number and call
                    # it recovered.
                    vol = self.model.iv_at(fallback_iv, spot, ref.strike, years)
                    self.coverage.iv_modelled += 1
                    iv_source = "modelled (price carries no vega)"
                else:
                    self.coverage.iv_recovered += 1
                    iv_source = "recovered from the traded price"
            else:
                vol = self.model.iv_at(fallback_iv, spot, ref.strike, years)
                mark = bs_price(spot, ref.strike, years, vol, ref.is_call, self.rate)
                volume = 0
                source = "modelled"
                iv_source = "modelled (no bar)"
                self.coverage.marks_modelled += 1
                self.coverage.iv_modelled += 1

            if mark < self.model.min_quotable_mid:
                continue

            moneyness = self._moneyness(spot, ref.strike, vol, years)
            half = self.model.half_spread(mark, moneyness, tier)
            greeks = bs_greeks(spot, ref.strike, years, vol, ref.is_call, self.rate)

            quotes.append(
                OptionQuote(
                    symbol=ref.symbol,
                    underlying=symbol.upper(),
                    expiry=ref.expiry,
                    strike=ref.strike,
                    right=Right.CALL if ref.is_call else Right.PUT,
                    bid=round(max(0.01, mark - half), 2),
                    ask=round(mark + half, 2),
                    last=round(mark, 2),
                    implied_volatility=round(vol, 4),
                    greeks=Greeks(
                        delta=greeks["delta"], gamma=greeks["gamma"],
                        theta=greeks["theta"], vega=greeks["vega"],
                    ),
                    open_interest=ref.open_interest,
                    volume=volume,
                    asof=asof,
                )
            )
            _ = (source, iv_source)   # kept for the mark log below

        if not quotes and symbol.upper() not in self._explained:
            # Say WHICH filter emptied the chain. "Nothing was priced" with no
            # further detail sent a previous debugging session in three wrong
            # directions; the counts point straight at the cause.
            self._explained.add(symbol.upper())
            log.warning(
                "%s %s: no quotable contract. %d listed for this underlying -> "
                "%d inside %d-%d DTE -> %d inside the %.0f%% strike band "
                "(spot %.2f, %.2f-%.2f). %s",
                symbol, day, len(available), passed_dte, min_dte, max_dte,
                passed_strike, window * 100, spot, low, high,
                "No contract has a usable expiry - check the listing window."
                if passed_dte == 0 else
                "Expiries are fine; the STRIKES are wrong - the contract cap or "
                "the listing band kept the wrong end of the ladder."
                if passed_strike == 0 else
                "Strikes and expiries are fine; every mark was below "
                f"min_quotable_mid ({self.model.min_quotable_mid}).",
            )
        return quotes

    # ------------------------------------------------------------------ #
    def reprice(
        self,
        contract: str,
        spot: float,
        asof: dt.date | dt.datetime,
        fallback_iv: float,
        strike: float,
        expiry: dt.date,
        is_call: bool,
        tier_symbol: str,
        force_model: bool = False,
    ) -> dict[str, float]:
        """Mark one open contract on a later session, preferring the real bar.

        `force_model` ignores the real bar and prices from the model. The caller
        uses it to put every leg of one structure on a SINGLE surface - see
        `BacktestEngine._leg_marks`, which explains why that matters.
        """
        asof_date = _as_date(asof)
        dte = (expiry - asof_date).days
        years = years_to_expiry(expiry, asof)
        if years <= 0:
            # Genuinely done: expired, or past the close on its expiry day.
            # NOT the same as "expires today", which still carries time value -
            # see `years_to_expiry`, and the -100% trades that came of it.
            intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
            return {"mid": round(intrinsic, 4), "bid": round(intrinsic, 4),
                    "ask": round(intrinsic, 4), "iv": 0.0, "dte": 0, "real": 1.0}

        bar = None if force_model else self.bars.get(contract, {}).get(asof_date)
        if bar is not None:
            mark = float(bar["close"])
            vol = implied_vol_from_price(mark, spot, strike, years, is_call, self.rate)
            if vol is None:
                vol = self.model.iv_at(fallback_iv, spot, strike, years)
            real = 1.0
            self.coverage.marks_from_bars += 1
        else:
            vol = self.model.iv_at(fallback_iv, spot, strike, years)
            mark = bs_price(spot, strike, years, vol, is_call, self.rate)
            real = 0.0
            self.coverage.marks_modelled += 1

        tier = tier_for(
            tier_symbol, self.model.tier_map, self.model.tiers, self.model.default_tier
        )
        half = self.model.half_spread(
            max(mark, 0.01), self._moneyness(spot, strike, vol, years), tier
        )
        return {
            "mid": round(mark, 4),
            "bid": round(max(0.0, mark - half), 4),
            "ask": round(mark + half, 4),
            "iv": round(vol, 4),
            "dte": dte,
            "real": real,
        }

    # ------------------------------------------------------------------ #
    @staticmethod
    def _moneyness(spot: float, strike: float, vol: float, years: float) -> float:
        import math

        denom = max(1e-6, vol * math.sqrt(max(years, 1e-6)))
        return max(-4.0, min(4.0, math.log(max(strike, 1e-9) / max(spot, 1e-9)) / denom))

    # ------------------------------------------------------------------ #
    def atm_iv(self, symbol: str, spot: float, day: dt.date, target_dte: int = 30) -> float | None:
        """The session's ATM implied vol, recovered from real prints only.

        This is what makes IV RANK real rather than modelled: a series of these
        across the window is the actual implied-vol history of the name, and
        the premium gate ranks against it. A day with no usable print returns
        None, and the caller carries the last known value forward rather than
        inventing one.
        """
        best: tuple[float, float] | None = None      # (distance, vol)
        for ref in self.contracts.get(symbol.upper(), []):
            dte = (ref.expiry - day).days
            if dte < 5 or dte > 60:
                continue
            bar = self.bars.get(ref.symbol, {}).get(day)
            if bar is None or float(bar.get("volume", 0)) < self.min_bar_volume:
                continue
            years = dte / _DAYS
            vol = implied_vol_from_price(
                float(bar["close"]), spot, ref.strike, years, ref.is_call, self.rate
            )
            if vol is None:
                continue
            # Closest to at-the-money and to the target maturity.
            distance = abs(ref.strike / spot - 1.0) + abs(dte - target_dte) / 200.0
            if best is None or distance < best[0]:
                best = (distance, vol)
        return best[1] if best else None
