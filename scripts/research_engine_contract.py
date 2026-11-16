"""Immutable shared contracts for the Stage 1 research engines.

These types describe evidence and model outputs; they do not represent trades,
executable quotes, or authenticated safety conclusions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
import re
from typing import TypeAlias

from scripts.address_identity import address_key, normalize_chain


class EvidenceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    STALE = "STALE"
    CONFLICTING = "CONFLICTING"
    UNVERIFIED = "UNVERIFIED"


class MeasurementScope(str, Enum):
    TOKEN = "TOKEN"
    POOL = "POOL"
    CHAIN = "CHAIN"
    SECTOR = "SECTOR"


class DerivationKind(str, Enum):
    RAW = "RAW"
    AGGREGATED = "AGGREGATED"
    DERIVED = "DERIVED"
    MIRRORED = "MIRRORED"


class CandleWindowKind(str, Enum):
    OUTCOME_24H = "OUTCOME_24H"
    TECHNICAL_LOOKBACK = "TECHNICAL_LOOKBACK"


class DepthModelStatus(str, Enum):
    NOT_MODELED = "NOT_MODELED"
    MODELED = "MODELED"
    PROVIDER_QUOTED = "PROVIDER_QUOTED"


class SwapSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    UNKNOWN = "UNKNOWN"


class SwapSideBasis(str, Enum):
    TOKEN_BALANCE_DELTA = "TOKEN_BALANCE_DELTA"
    QUOTE_FLOW_LABEL = "QUOTE_FLOW_LABEL"
    PROVIDER_LABEL = "PROVIDER_LABEL"
    UNKNOWN = "UNKNOWN"


class LiquidityKind(str, Enum):
    REAL = "REAL"
    VIRTUAL = "VIRTUAL"
    UNKNOWN = "UNKNOWN"


class HolderAttribution(str, Enum):
    ADDRESS_LEVEL_ONLY = "ADDRESS_LEVEL_ONLY"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


class EvidenceAnomaly(str, Enum):
    UNKNOWN_SIDE = "UNKNOWN_SIDE"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    TRUNCATED_SAMPLE = "TRUNCATED_SAMPLE"
    PROVIDER_LABEL_ONLY = "PROVIDER_LABEL_ONLY"
    UNDOCUMENTED_AGGRESSION = "UNDOCUMENTED_AGGRESSION"
    OVERLAPPING_FEEDS = "OVERLAPPING_FEEDS"


@dataclass(frozen=True)
class MarketIdentity:
    chain: str
    token_mint: str
    pool_address: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (
            self.chain, self.token_mint, self.pool_address
        )):
            raise ValueError("chain, token_mint, and pool_address are required")

    @property
    def join_key(self) -> tuple[str, str, str]:
        chain = normalize_chain(self.chain)
        return (
            chain,
            address_key(chain, self.token_mint),
            address_key(chain, self.pool_address),
        )

    def matches(self, other: MarketIdentity) -> bool:
        return isinstance(other, MarketIdentity) and self.join_key == other.join_key


@dataclass(frozen=True)
class AggregationWindow:
    start_utc: str
    end_utc: str
    interval_seconds: int

    def __post_init__(self) -> None:
        start = _parse_utc(self.start_utc)
        end = _parse_utc(self.end_utc)
        if start is None or end is None or end <= start:
            raise ValueError("aggregation window must have ordered UTC timestamps")
        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, int)
            or self.interval_seconds <= 0
        ):
            raise ValueError("aggregation interval must be a positive number of seconds")
        if (end - start).total_seconds() % self.interval_seconds:
            raise ValueError("aggregation duration must be an exact multiple of its interval")


@dataclass(frozen=True)
class AdapterVerification:
    """Adapter-issued checks; descriptive source metadata alone is not verification."""

    adapter_id: str
    adapter_version: str
    source_record_id: str
    verified_identity: MarketIdentity | None
    verified_scope: MeasurementScope
    verified_scope_key: str
    verified_field_path: str
    source_as_of_utc: str | None
    transport_status: EvidenceStatus
    schema_status: EvidenceStatus
    field_status: EvidenceStatus
    identity_status: EvidenceStatus
    freshness_limit_seconds: int
    independent_source_id: str | None = None

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (
            self.adapter_id, self.adapter_version, self.source_record_id,
            self.verified_scope_key, self.verified_field_path,
        )):
            raise ValueError("adapter verification requires adapter, record, scope, and field identifiers")
        if isinstance(self.freshness_limit_seconds, bool) or self.freshness_limit_seconds <= 0:
            raise ValueError("freshness limit must be positive")
        if self.source_as_of_utc is not None and _parse_utc(self.source_as_of_utc) is None:
            raise ValueError("adapter source-as-of timestamp must include a UTC offset")

    def status(
        self,
        *,
        scope: MeasurementScope,
        scope_key: str,
        field_path: str,
        identity: MarketIdentity | None,
        as_of_utc: str,
    ) -> EvidenceStatus:
        if self.verified_scope is not scope or not _same_scope_key(scope, self.verified_scope_key, scope_key):
            return EvidenceStatus.CONFLICTING
        if self.verified_field_path != field_path:
            return EvidenceStatus.CONFLICTING
        if (identity is None) != (self.verified_identity is None):
            return EvidenceStatus.CONFLICTING
        if identity is not None and not identity.matches(self.verified_identity):
            return EvidenceStatus.CONFLICTING
        if any(status is EvidenceStatus.CONFLICTING for status in (
            self.transport_status, self.schema_status, self.field_status, self.identity_status
        )):
            return EvidenceStatus.CONFLICTING
        if any(status is not EvidenceStatus.VERIFIED for status in (
            self.transport_status, self.schema_status, self.field_status, self.identity_status
        )):
            return EvidenceStatus.UNVERIFIED
        source_as_of = _parse_utc(self.source_as_of_utc)
        evaluated_at = _parse_utc(as_of_utc)
        if source_as_of is None or evaluated_at is None:
            return EvidenceStatus.UNVERIFIED
        age_seconds = (evaluated_at - source_as_of).total_seconds()
        if age_seconds < 0:
            return EvidenceStatus.CONFLICTING
        if age_seconds > self.freshness_limit_seconds:
            return EvidenceStatus.STALE
        return EvidenceStatus.VERIFIED


@dataclass(frozen=True)
class EvidenceProvenance:
    provider: str
    endpoint: str
    scope: MeasurementScope
    scope_key: str
    identity: MarketIdentity | None
    source_event_time_utc: str | None
    source_as_of_utc: str | None
    retrieved_at_utc: str | None
    ingested_at_utc: str | None
    field_path: str
    payload_sha256: str | None = None
    adapter_verification: AdapterVerification | None = None

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (
            self.provider, self.endpoint, self.scope_key, self.field_path
        )):
            raise ValueError("provider, endpoint, scope_key, and field_path are required")
        for value in (
            self.source_event_time_utc, self.source_as_of_utc,
            self.retrieved_at_utc, self.ingested_at_utc,
        ):
            if value is not None and _parse_utc(value) is None:
                raise ValueError("provenance timestamps must include a UTC offset")
        if self.scope in {MeasurementScope.TOKEN, MeasurementScope.POOL} and self.identity is None:
            raise ValueError("token- and pool-scoped provenance requires exact market identity")
        if self.scope in {MeasurementScope.CHAIN, MeasurementScope.SECTOR} and self.identity is not None:
            raise ValueError("chain- and sector-scoped provenance must not claim pool identity")
        if self.payload_sha256 is not None and not re.fullmatch(
            r"[0-9a-fA-F]{64}", self.payload_sha256
        ):
            raise ValueError("payload_sha256 must be a 64-character SHA-256 digest when captured")

    def verification_status(
        self,
        *,
        scope: MeasurementScope,
        scope_key: str,
        field_path: str,
        identity: MarketIdentity | None,
        as_of_utc: str,
    ) -> EvidenceStatus:
        if self.adapter_verification is None:
            return EvidenceStatus.UNVERIFIED
        if not self.source_as_of_utc or not self.retrieved_at_utc or not self.ingested_at_utc:
            return EvidenceStatus.UNVERIFIED
        if _parse_utc(self.source_as_of_utc) != _parse_utc(
            self.adapter_verification.source_as_of_utc
        ):
            return EvidenceStatus.CONFLICTING
        if (
            self.scope is not scope
            or not _same_scope_key(scope, self.scope_key, scope_key)
            or self.field_path != field_path
        ):
            return EvidenceStatus.CONFLICTING
        if (identity is None) != (self.identity is None):
            return EvidenceStatus.CONFLICTING
        if identity is not None and not identity.matches(self.identity):
            return EvidenceStatus.CONFLICTING
        retrieved = _parse_utc(self.retrieved_at_utc)
        ingested = _parse_utc(self.ingested_at_utc)
        source_as_of = _parse_utc(self.source_as_of_utc)
        if retrieved is None or ingested is None or source_as_of is None:
            return EvidenceStatus.UNVERIFIED
        source_event = _parse_utc(self.source_event_time_utc)
        if source_event is not None and source_event > source_as_of:
            return EvidenceStatus.CONFLICTING
        if retrieved < source_as_of or ingested < retrieved:
            return EvidenceStatus.CONFLICTING
        return self.adapter_verification.status(
            scope=scope,
            scope_key=scope_key,
            field_path=field_path,
            identity=identity,
            as_of_utc=as_of_utc,
        )


EvidenceValue: TypeAlias = float | int | str | None


@dataclass(frozen=True)
class Measurement:
    name: str
    value: EvidenceValue
    unit: str
    aggregation_window: AggregationWindow | None
    scope: MeasurementScope
    derivation: DerivationKind
    scope_key: str
    identity: MarketIdentity | None
    provenance: EvidenceProvenance | None
    parent_measurement_ids: tuple[str, ...] = ()
    aggregation_policy: str | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (
            self.name, self.unit, self.scope_key
        )):
            raise ValueError("measurement name, unit, and scope_key are required")
        if isinstance(self.value, bool):
            raise ValueError("boolean values are not numeric measurements")
        if isinstance(self.value, (int, float)) and not math.isfinite(float(self.value)):
            raise ValueError("numeric measurement values must be finite")
        if self.value is not None and not isinstance(self.value, (int, float, str)):
            raise ValueError("measurement value must be numeric, categorical, or unavailable")
        object.__setattr__(self, "parent_measurement_ids", tuple(self.parent_measurement_ids))
        if self.derivation in {DerivationKind.DERIVED, DerivationKind.AGGREGATED, DerivationKind.MIRRORED}:
            if not self.parent_measurement_ids:
                raise ValueError("derived, aggregated, and mirrored values require lineage")
            if not self.aggregation_policy:
                raise ValueError("derived, aggregated, and mirrored values require a lineage policy")
        elif self.parent_measurement_ids:
            raise ValueError("raw measurements cannot declare parent measurements")
        if self.scope in {MeasurementScope.TOKEN, MeasurementScope.POOL} and self.identity is None:
            raise ValueError("token- and pool-scoped measurements require exact market identity")
        if self.scope is MeasurementScope.POOL and address_key(
            self.identity.chain, self.scope_key
        ) != self.identity.join_key[2]:
            raise ValueError("pool scope_key must match the exact pool address")
        if self.scope is MeasurementScope.TOKEN and address_key(
            self.identity.chain, self.scope_key
        ) != self.identity.join_key[1]:
            raise ValueError("token scope_key must match the exact token address")
        if self.value is None and not self.unavailable_reason:
            raise ValueError("unavailable measurements require an explicit reason")
        if self.value is None and self.provenance is not None:
            raise ValueError("unavailable measurements must not carry fabricated source evidence")
        if self.provenance is not None and (
            self.provenance.scope is not self.scope
            or not _same_scope_key(self.scope, self.provenance.scope_key, self.scope_key)
        ):
            raise ValueError("measurement and provenance scopes must match")
        if self.provenance is not None and (
            (self.identity is None) != (self.provenance.identity is None)
            or (self.identity is not None and not self.identity.matches(self.provenance.identity))
        ):
            raise ValueError("measurement and provenance identities must match")

    def evidence_status(self, as_of_utc: str) -> EvidenceStatus:
        if self.value is None:
            return EvidenceStatus.MISSING
        if self.provenance is None:
            return EvidenceStatus.UNVERIFIED
        return self.provenance.verification_status(
            scope=self.scope,
            scope_key=self.scope_key,
            field_path=self.name,
            identity=self.identity,
            as_of_utc=as_of_utc,
        )


@dataclass(frozen=True)
class SwapSideEvidence:
    side: SwapSide
    basis: SwapSideBasis
    status: EvidenceStatus
    source_field_path: str | None = None

    def __post_init__(self) -> None:
        if self.side is SwapSide.UNKNOWN and self.status is EvidenceStatus.VERIFIED:
            raise ValueError("unknown side cannot be treated as verified buy/sell evidence")
        if self.basis is SwapSideBasis.PROVIDER_LABEL and self.status is EvidenceStatus.VERIFIED:
            raise ValueError("provider buy/sell labels do not verify taker aggression")
        if self.side is not SwapSide.UNKNOWN and self.status is EvidenceStatus.VERIFIED and (
            self.basis is not SwapSideBasis.TOKEN_BALANCE_DELTA
        ):
            raise ValueError("verified side classification requires token balance-delta evidence")
        if self.basis is SwapSideBasis.UNKNOWN and self.side is not SwapSide.UNKNOWN:
            raise ValueError("an asserted buy/sell side requires an explicit evidence basis")
        if self.side is not SwapSide.UNKNOWN and not self.source_field_path:
            raise ValueError("buy/sell side evidence requires its source field path")

    @property
    def taker_aggression_verified(self) -> bool:
        return False


@dataclass(frozen=True)
class SwapEvent:
    event_id: str
    identity: MarketIdentity
    occurred_at_utc: str
    transaction_id: str
    block_or_slot: int
    transaction_index: int
    event_index: int
    base_token_mint: str
    quote_token_mint: str
    base_amount: float
    quote_amount: float
    base_unit: str
    quote_unit: str
    side_evidence: SwapSideEvidence
    provenance: EvidenceProvenance | None

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.transaction_id.strip():
            raise ValueError("swap event and transaction identities are required")
        if _parse_utc(self.occurred_at_utc) is None:
            raise ValueError("swap event timestamp must include a UTC offset")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.block_or_slot, self.transaction_index, self.event_index)
        ):
            raise ValueError("swap event order fields must be non-negative integers")
        if any(isinstance(value, bool) or value < 0 for value in (
            self.base_amount, self.quote_amount,
        )):
            raise ValueError("swap order and amount values must be non-negative numbers")
        if any(not math.isfinite(float(value)) for value in (self.base_amount, self.quote_amount)):
            raise ValueError("swap amounts must be finite")
        if not self.base_token_mint or not self.quote_token_mint or not self.base_unit or not self.quote_unit:
            raise ValueError("swap token orientation and amount units are required")
        if address_key(self.identity.chain, self.base_token_mint) == address_key(
            self.identity.chain, self.quote_token_mint
        ):
            raise ValueError("base and quote token identities must differ")
        if not address_key(self.identity.chain, self.base_token_mint) == address_key(
            self.identity.chain, self.identity.token_mint
        ):
            raise ValueError("swap base token must match the tracked token identity")
        if self.provenance is not None and not self.identity.matches(self.provenance.identity):
            raise ValueError("swap provenance must match the exact chain, token, and pool identity")

    @property
    def order_key(self) -> tuple[int, int, int, str]:
        return self.block_or_slot, self.transaction_index, self.event_index, self.transaction_id


@dataclass(frozen=True)
class EventCoverage:
    window: AggregationWindow
    observed_event_count: int
    expected_event_count: int | None
    complete: bool
    truncated: bool
    continuation_cursor: str | None = None
    duplicate_event_ids: tuple[str, ...] = ()
    anomalies: tuple[EvidenceAnomaly, ...] = ()

    def __post_init__(self) -> None:
        if self.observed_event_count < 0:
            raise ValueError("observed event count cannot be negative")
        if self.expected_event_count is not None and self.expected_event_count < 0:
            raise ValueError("expected event count cannot be negative")
        object.__setattr__(self, "duplicate_event_ids", tuple(self.duplicate_event_ids))
        object.__setattr__(self, "anomalies", tuple(self.anomalies))
        if self.truncated and EvidenceAnomaly.TRUNCATED_SAMPLE not in self.anomalies:
            object.__setattr__(
                self, "anomalies", (*self.anomalies, EvidenceAnomaly.TRUNCATED_SAMPLE)
            )
        if self.duplicate_event_ids and EvidenceAnomaly.DUPLICATE_EVENT not in self.anomalies:
            object.__setattr__(
                self, "anomalies", (*self.anomalies, EvidenceAnomaly.DUPLICATE_EVENT)
            )
        if self.complete and (self.truncated or (
            self.expected_event_count is not None
            and self.observed_event_count != self.expected_event_count
        ) or self.duplicate_event_ids):
            raise ValueError("complete event coverage cannot be truncated or count-mismatched")

    @property
    def coverage_pct(self) -> float | None:
        if self.expected_event_count is None:
            return None
        if self.expected_event_count == 0:
            return 100.0 if self.complete else 0.0
        return min(100.0, self.observed_event_count / self.expected_event_count * 100.0)


@dataclass(frozen=True)
class HolderEvidence:
    holder_address: str
    balance: float
    unit: str
    attribution: HolderAttribution = HolderAttribution.ADDRESS_LEVEL_ONLY
    beneficial_owner: None = None

    def __post_init__(self) -> None:
        if not self.holder_address.strip() or not self.unit.strip():
            raise ValueError("holder address and balance unit are required")
        if isinstance(self.balance, bool) or not math.isfinite(float(self.balance)) or self.balance < 0:
            raise ValueError("holder balance must be finite and non-negative")
        if self.beneficial_owner is not None:
            raise ValueError("address-level holder evidence cannot infer a beneficial owner")


@dataclass(frozen=True)
class AssetAges:
    token_age_hours: float | None
    pool_age_hours: float | None
    as_of_utc: str

    def __post_init__(self) -> None:
        if _parse_utc(self.as_of_utc) is None:
            raise ValueError("age reference timestamp must include a UTC offset")
        for value in (self.token_age_hours, self.pool_age_hours):
            if value is not None and (
                isinstance(value, bool) or not math.isfinite(float(value)) or value < 0
            ):
                raise ValueError("token and pool age values must be finite and non-negative")


@dataclass(frozen=True)
class TradeFlowSummary:
    trade_count: Measurement | None
    buy_count: Measurement | None
    sell_count: Measurement | None
    buy_notional: Measurement | None
    sell_notional: Measurement | None
    net_quote_delta: Measurement | None
    anomalies: tuple[EvidenceAnomaly, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "anomalies", tuple(self.anomalies))
        for measurement, expected_unit in (
            (self.trade_count, "trade"),
            (self.buy_count, "trade"),
            (self.sell_count, "trade"),
        ):
            if measurement is not None and measurement.unit != expected_unit:
                raise ValueError("trade counts must use the 'trade' unit")
        for measurement in (self.buy_notional, self.sell_notional, self.net_quote_delta):
            if measurement is not None and not measurement.unit.endswith("_notional"):
                raise ValueError("buy/sell notional and net delta require explicit quote notional units")
        if self.buy_count is not None and self.sell_count is not None and self.trade_count is not None:
            counts = (self.buy_count.value, self.sell_count.value, self.trade_count.value)
            if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in counts):
                if self.buy_count.value + self.sell_count.value > self.trade_count.value:
                    raise ValueError("buy and sell counts cannot exceed total trade count")
        values = (self.buy_count, self.sell_count, self.buy_notional, self.sell_notional)
        if any(item is not None and item.value is None for item in values):
            object.__setattr__(
                self, "anomalies",
                tuple(dict.fromkeys((*self.anomalies, EvidenceAnomaly.UNKNOWN_SIDE))),
            )

    @property
    def buy_sell_dominance_available(self) -> bool:
        return bool(
            self.buy_count and self.sell_count
            and self.buy_count.value is not None and self.sell_count.value is not None
            and EvidenceAnomaly.UNKNOWN_SIDE not in self.anomalies
        )


@dataclass(frozen=True)
class Candle:
    time_open_utc: str
    time_close_utc: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    provenance: EvidenceProvenance | None

    def __post_init__(self) -> None:
        opened = _parse_utc(self.time_open_utc)
        closed = _parse_utc(self.time_close_utc)
        if opened is None or closed is None or closed <= opened:
            raise ValueError("candle timestamps must be ordered UTC timestamps")
        values = (self.open, self.high, self.low, self.close)
        if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in values):
            raise ValueError("candle prices must be finite numeric values")
        if self.volume is not None and (
            isinstance(self.volume, bool) or not math.isfinite(float(self.volume))
        ):
            raise ValueError("candle volume must be finite when supplied")
        object.__setattr__(self, "time_open_utc", _format_utc(opened))
        object.__setattr__(self, "time_close_utc", _format_utc(closed))
        if self.low <= 0 or self.high < self.low or not self.low <= self.open <= self.high:
            raise ValueError("candle OHLC values are inconsistent")
        if not self.low <= self.close <= self.high:
            raise ValueError("candle close must be within its low/high range")
        if self.volume is not None and self.volume < 0:
            raise ValueError("candle volume cannot be negative")


@dataclass(frozen=True)
class CandleWindow:
    kind: CandleWindowKind
    start_utc: str
    end_utc: str
    interval_seconds: int
    expected_slots: int
    candles: tuple[Candle, ...]
    cutoff_utc: str
    expected_identity: MarketIdentity
    missing_slots_utc: tuple[str, ...] = ()
    duplicate_slots_utc: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        start = _parse_utc(self.start_utc)
        end = _parse_utc(self.end_utc)
        cutoff = _parse_utc(self.cutoff_utc)
        if start is None or end is None or end <= start:
            raise ValueError("candle window must have ordered UTC timestamps")
        if cutoff is None:
            raise ValueError("candle cutoff must include a UTC offset")
        if isinstance(self.interval_seconds, bool) or isinstance(self.expected_slots, bool):
            raise ValueError("candle interval and slot count must be integers")
        if self.interval_seconds <= 0 or self.expected_slots <= 0:
            raise ValueError("candle window interval and expected slot count must be positive")
        if self.kind is CandleWindowKind.OUTCOME_24H and (
            self.interval_seconds != 3600
            or self.expected_slots != 24
            or end - start != timedelta(hours=24)
        ):
            raise ValueError("24-hour outcomes require exactly 24 one-hour slots over 24 hours")
        if end - start != timedelta(seconds=self.interval_seconds * self.expected_slots):
            raise ValueError("candle window duration must equal interval times expected slots")
        object.__setattr__(self, "start_utc", _format_utc(start))
        object.__setattr__(self, "end_utc", _format_utc(end))
        object.__setattr__(self, "cutoff_utc", _format_utc(cutoff))
        object.__setattr__(self, "candles", tuple(self.candles))
        opens = [candle.time_open_utc for candle in self.candles]
        duplicates = tuple(sorted({stamp for stamp in opens if opens.count(stamp) > 1}))
        object.__setattr__(self, "duplicate_slots_utc", duplicates)
        expected = set(self.expected_slot_opens_utc)
        present = set(opens)
        missing = tuple(stamp for stamp in self.expected_slot_opens_utc if stamp not in present)
        object.__setattr__(self, "missing_slots_utc", missing)
        if any(candle.time_close_utc != _format_utc(
            _parse_utc(candle.time_open_utc) + timedelta(seconds=self.interval_seconds)
        ) for candle in self.candles):
            raise ValueError("candle duration must match the declared sampling interval")
        if any(opened not in expected for opened in opens):
            raise ValueError("candle open timestamp falls outside the declared window slots")
        if any(candle.time_close_utc > self.cutoff_utc for candle in self.candles):
            raise ValueError("candle data must be complete by the declared point-in-time cutoff")
        if any(candle.provenance is not None and (
            candle.provenance.identity is None
            or not self.expected_identity.matches(candle.provenance.identity)
        ) for candle in self.candles):
            raise ValueError("candle provenance must match the expected exact market identity")

    @property
    def expected_slot_opens_utc(self) -> tuple[str, ...]:
        start = _parse_utc(self.start_utc)
        return tuple(_format_utc(
            start + timedelta(seconds=index * self.interval_seconds)
        ) for index in range(self.expected_slots))

    @property
    def coverage_pct(self) -> float:
        observed = {candle.time_open_utc for candle in self.candles}
        return len(observed.intersection(self.expected_slot_opens_utc)) / self.expected_slots * 100.0

    def elapsed_slot_opens_utc(self, as_of_utc: str) -> tuple[str, ...]:
        as_of = _parse_utc(as_of_utc)
        cutoff = _parse_utc(self.cutoff_utc)
        if as_of is None or cutoff is None:
            return ()
        elapsed_cutoff = min(as_of, cutoff)
        return tuple(
            opened for opened in self.expected_slot_opens_utc
            if _parse_utc(opened) + timedelta(seconds=self.interval_seconds) <= elapsed_cutoff
        )

    def missing_elapsed_slots_utc(self, as_of_utc: str) -> tuple[str, ...]:
        present = {candle.time_open_utc for candle in self.candles}
        return tuple(opened for opened in self.elapsed_slot_opens_utc(as_of_utc) if opened not in present)

    @property
    def future_censored_slots(self) -> int:
        return self.expected_slots - len(self.elapsed_slot_opens_utc(self.cutoff_utc))

    @property
    def gap_runs(self) -> tuple[tuple[str, int], ...]:
        missing = set(self.missing_slots_utc)
        runs: list[tuple[str, int]] = []
        run_start = None
        run_count = 0
        previous = None
        for opened in self.expected_slot_opens_utc:
            if opened in missing:
                stamp = _parse_utc(opened)
                if previous is None or stamp - previous != timedelta(seconds=self.interval_seconds):
                    if run_start is not None:
                        runs.append((run_start, run_count))
                    run_start, run_count = opened, 1
                else:
                    run_count += 1
                previous = stamp
            else:
                if run_start is not None:
                    runs.append((run_start, run_count))
                    run_start, run_count = None, 0
                previous = None
        if run_start is not None:
            runs.append((run_start, run_count))
        return tuple(runs)

    def freshness_status(self, as_of_utc: str) -> EvidenceStatus:
        if _parse_utc(as_of_utc) is None:
            return EvidenceStatus.UNVERIFIED
        for candle in self.candles:
            if candle.provenance is None:
                return EvidenceStatus.UNVERIFIED
            status = candle.provenance.verification_status(
                scope=MeasurementScope.POOL,
                scope_key=self.expected_identity.join_key[2],
                field_path="candle",
                identity=self.expected_identity,
                as_of_utc=as_of_utc,
            )
            if status in {EvidenceStatus.CONFLICTING, EvidenceStatus.STALE, EvidenceStatus.UNVERIFIED}:
                return status
        return EvidenceStatus.VERIFIED

    def coverage_status(self, as_of_utc: str) -> EvidenceStatus:
        as_of = _parse_utc(as_of_utc)
        if as_of is None:
            return EvidenceStatus.UNVERIFIED
        cutoff = _parse_utc(self.cutoff_utc)
        if cutoff > as_of:
            return EvidenceStatus.CONFLICTING
        if self.duplicate_slots_utc:
            return EvidenceStatus.CONFLICTING
        if any(_parse_utc(candle.time_close_utc) > as_of for candle in self.candles):
            return EvidenceStatus.CONFLICTING
        if any(_parse_utc(candle.time_close_utc) > cutoff for candle in self.candles):
            return EvidenceStatus.CONFLICTING
        freshness = self.freshness_status(as_of_utc)
        if freshness is not EvidenceStatus.VERIFIED:
            return freshness
        if as_of < _parse_utc(self.end_utc) or self.missing_elapsed_slots_utc(as_of_utc):
            return EvidenceStatus.PARTIAL
        if len(self.candles) != self.expected_slots:
            return EvidenceStatus.CONFLICTING
        return EvidenceStatus.VERIFIED


@dataclass(frozen=True)
class TradePlan:
    maximum_holding_hours: int
    single_take_profit_pct: float
    stop_loss_pct: None = None
    trailing_stop_enabled: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.maximum_holding_hours, bool) or self.maximum_holding_hours != 24:
            raise ValueError("the research trade plan is capped at 24 hours")
        if isinstance(self.single_take_profit_pct, bool) or not math.isfinite(
            float(self.single_take_profit_pct)
        ) or self.single_take_profit_pct < 40.0:
            raise ValueError("the single technical take-profit must be at least +40.00%")
        if self.stop_loss_pct is not None or self.trailing_stop_enabled:
            raise ValueError("stop-loss and trailing-stop rules are not part of this trade plan")


@dataclass(frozen=True)
class CalibratedProbability:
    value_pct: float
    calibration_id: str
    holdout_dataset_id: str
    evaluated_at_utc: str

    def __post_init__(self) -> None:
        if isinstance(self.value_pct, bool) or not math.isfinite(float(self.value_pct)):
            raise ValueError("calibrated probability must be finite")
        if not 0.0 <= self.value_pct <= 100.0:
            raise ValueError("calibrated probability must be between 0 and 100")
        if not self.calibration_id.strip() or not self.holdout_dataset_id.strip():
            raise ValueError("calibrated probability requires calibration and holdout identifiers")
        if _parse_utc(self.evaluated_at_utc) is None:
            raise ValueError("calibrated probability timestamp must include a UTC offset")


class RiskEvidenceStatus(str, Enum):
    CONFIRMED_ADVERSE = "CONFIRMED_ADVERSE"
    NO_ADVERSE_OBSERVED = "NO_ADVERSE_OBSERVED"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICTING = "CONFLICTING"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class EngineInput:
    identity: MarketIdentity
    as_of_utc: str
    measurements: tuple[Measurement, ...]
    candle_windows: tuple[CandleWindow, ...]
    swap_events: tuple[SwapEvent, ...]
    swap_coverage: EventCoverage | None
    trade_plan: TradePlan

    def __post_init__(self) -> None:
        if _parse_utc(self.as_of_utc) is None:
            raise ValueError("engine as_of timestamp must include a UTC offset")
        object.__setattr__(self, "measurements", tuple(self.measurements))
        object.__setattr__(self, "candle_windows", tuple(self.candle_windows))
        object.__setattr__(self, "swap_events", tuple(self.swap_events))
        if any(not isinstance(item, Measurement) for item in self.measurements):
            raise ValueError("engine measurements must use the shared Measurement type")
        if any(not isinstance(item, CandleWindow) for item in self.candle_windows):
            raise ValueError("engine candle windows must use the shared CandleWindow type")
        if any(
            measurement.scope in {MeasurementScope.TOKEN, MeasurementScope.POOL}
            and not measurement.identity.matches(self.identity)
            for measurement in self.measurements
        ):
            raise ValueError("token/pool measurements must match exact engine identity")
        if any(not window.expected_identity.matches(self.identity) for window in self.candle_windows):
            raise ValueError("candle windows must match exact engine identity")
        if any(not event.identity.matches(self.identity) for event in self.swap_events):
            raise ValueError("swap event identity must match the engine market identity")
        if tuple(sorted(self.swap_events, key=lambda event: event.order_key)) != self.swap_events:
            raise ValueError("swap events must be ordered by stable block/transaction/event position")
        duplicate_ids = tuple(sorted({
            event.event_id for event in self.swap_events
            if sum(item.event_id == event.event_id for item in self.swap_events) > 1
        }))
        if duplicate_ids and (
            self.swap_coverage is None
            or not set(duplicate_ids).issubset(self.swap_coverage.duplicate_event_ids)
        ):
            raise ValueError("duplicate swap event identities must remain explicit in coverage")


@dataclass(frozen=True)
class DepthAssessment:
    model_status: DepthModelStatus
    liquidity_kind: LiquidityKind
    estimated_depth_usd: float | None
    notional_usd: float | None
    source_id: str | None = None
    execution_status: str = "EXECUTION_UNVERIFIED"

    def __post_init__(self) -> None:
        for value in (self.estimated_depth_usd, self.notional_usd):
            if value is not None and (
                isinstance(value, bool) or not math.isfinite(float(value)) or value < 0
            ):
                raise ValueError("depth and notional values must be finite and non-negative")
        if self.execution_status != "EXECUTION_UNVERIFIED":
            raise ValueError("modeled or quoted depth cannot assert execution verification")


@dataclass(frozen=True)
class EngineResult:
    engine_id: str
    score: float | None
    evidence_status: EvidenceStatus
    data_coverage_pct: float
    data_quality_confidence_pct: float | None = None
    evidence: tuple[EvidenceProvenance, ...] = ()
    diagnostics: tuple[tuple[str, str | float | int | bool | None], ...] = ()
    adverse_status: RiskEvidenceStatus = RiskEvidenceStatus.UNAVAILABLE
    adverse_coverage_complete: bool = False
    technical_target_upside_pct: float | None = None
    projected_upside_pct: float | None = None
    projected_probability_pct: float | None = None
    calibrated_probability: CalibratedProbability | None = None
    executable_net_return_pct: float | None = None

    def __post_init__(self) -> None:
        if not self.engine_id.strip():
            raise ValueError("engine_id is required")
        for name, value in (
            ("score", self.score),
            ("data_coverage_pct", self.data_coverage_pct),
            ("data_quality_confidence_pct", self.data_quality_confidence_pct),
            ("technical_target_upside_pct", self.technical_target_upside_pct),
            ("projected_upside_pct", self.projected_upside_pct),
            ("projected_probability_pct", self.projected_probability_pct),
            ("executable_net_return_pct", self.executable_net_return_pct),
        ):
            if value is not None and (
                isinstance(value, bool) or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be a finite numeric value")
        if self.score is not None and not 0.0 <= self.score <= 100.0:
            raise ValueError("engine score must be between 0 and 100")
        if not 0.0 <= self.data_coverage_pct <= 100.0:
            raise ValueError("data coverage must be between 0 and 100")
        if self.data_quality_confidence_pct is not None and not (
            0.0 <= self.data_quality_confidence_pct <= 100.0
        ):
            raise ValueError("data-quality confidence must be between 0 and 100")
        if self.projected_probability_pct is not None and not (
            0.0 <= self.projected_probability_pct <= 100.0
        ):
            raise ValueError("projected probability must be between 0 and 100")
        if self.adverse_status is RiskEvidenceStatus.NO_ADVERSE_OBSERVED and (
            not self.adverse_coverage_complete or self.evidence_status is not EvidenceStatus.VERIFIED
        ):
            raise ValueError("no-adverse-observed requires complete, verified risk evidence")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


def independent_provider_count(
    measurements: tuple[Measurement, ...],
    *,
    identity: MarketIdentity,
    as_of_utc: str,
) -> int:
    """Count raw corroboration without pooling distinct windows, scopes, or lineages."""
    groups: dict[tuple[object, ...], set[str]] = {}
    for measurement in measurements:
        provenance = measurement.provenance
        adapter = provenance.adapter_verification if provenance is not None else None
        if (
            measurement.derivation is not DerivationKind.RAW
            or provenance is None
            or adapter is None
            or not adapter.independent_source_id
        ):
            continue
        expected_identity = identity if measurement.scope in {
            MeasurementScope.TOKEN, MeasurementScope.POOL
        } else None
        if expected_identity is not None and (
            measurement.identity is None or not measurement.identity.matches(expected_identity)
        ):
            continue
        if measurement.evidence_status(as_of_utc) is not EvidenceStatus.VERIFIED:
            continue
        group = (
            measurement.name,
            measurement.unit,
            measurement.scope,
            measurement.scope_key,
            measurement.aggregation_window,
        )
        groups.setdefault(group, set()).add(adapter.independent_source_id)
    return max((len(sources) for sources in groups.values()), default=0)


def _parse_utc(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.isoformat(timespec="microseconds" if value.microsecond else "seconds").replace(
        "+00:00", "Z"
    )
