"""Instrument models.

An instrument is one tradeable market on one venue. Atlas normalises the concepts
that differ between venues (contract denomination, multipliers, funding intervals)
and refuses to invent the ones a venue does not publish.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from vizanix_atlas.models.base import AtlasModel
from vizanix_atlas.models.enums import (
    ContractType,
    FundingSemantics,
    InstrumentType,
    OpenInterestUnit,
    OptionType,
    SettlementPeriod,
)


class Instrument(AtlasModel):
    """A normalised instrument.

    One model covers all four instrument types rather than a class hierarchy,
    because the published Parquet table is a single table and a hierarchy would
    only be flattened again on write. Fields that do not apply to a type are null,
    and :meth:`_check_type_consistency` enforces which combinations are coherent.
    """

    instrument_id: str
    venue_slug: str
    instrument_type: InstrumentType
    instrument_class: str = Field(
        description=(
            "The adapter's sub-namespace, for example 'spot' or 'linear-perp'. Part of "
            "instrument_id, so that two venue products under one symbol never collide."
        )
    )

    symbol_native: str = Field(description="The venue's symbol, preserved verbatim.")
    symbol_normalized: str = Field(
        description="Atlas's readable form, for example 'BTC/USDT'. For display, not for joining."
    )

    base_asset_id: str
    quote_asset_id: str
    settlement_asset_id: str | None = Field(
        default=None, description="What the contract settles in. Null for spot."
    )
    underlying_asset_id: str | None = Field(
        default=None, description="For options and dated futures, the asset the contract is on."
    )

    contract_type: ContractType | None = None
    contract_multiplier: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Units of contract_value_asset per contract. Never inferred: a null here "
            "excludes the instrument from open-interest normalisation."
        ),
    )
    contract_value_asset_id: str | None = Field(
        default=None, description="Which asset contract_multiplier is denominated in."
    )
    settlement_period: SettlementPeriod | None = None

    tick_size: float | None = Field(default=None, gt=0)
    quantity_step: float | None = Field(default=None, gt=0)
    minimum_quantity: float | None = Field(default=None, ge=0)
    minimum_notional: float | None = Field(default=None, ge=0)

    expiry: int | None = Field(default=None, description="Epoch milliseconds; null if perpetual.")
    strike: float | None = Field(default=None, gt=0)
    option_type: OptionType | None = None

    funding_interval_hours: float | None = Field(
        default=None,
        gt=0,
        description=(
            "The venue's native funding interval. Observed values across supported "
            "venues range from 1 to 8 hours."
        ),
    )
    funding_semantics: FundingSemantics | None = Field(
        default=None, description="How the venue's funding number must be interpreted."
    )
    open_interest_unit: OpenInterestUnit | None = Field(
        default=None, description="The unit the venue reports open interest in."
    )

    active: bool = True
    listing_time: int | None = None
    delisting_time: int | None = None
    first_seen_at: int | None = None
    last_seen_at: int | None = None

    @property
    def is_derivative(self) -> bool:
        """Whether this instrument is not spot."""
        return self.instrument_type is not InstrumentType.SPOT

    @property
    def is_inverse(self) -> bool:
        """Whether the contract settles in the base asset rather than the quote."""
        return self.contract_type is ContractType.INVERSE

    @property
    def can_normalise_open_interest(self) -> bool:
        """Whether open interest can be converted without guessing.

        Requires both a known unit and, when the unit is contracts, a published
        multiplier. False means the instrument's open interest is still recorded
        raw but excluded from aggregates.
        """
        if self.open_interest_unit is None or self.open_interest_unit is OpenInterestUnit.UNKNOWN:
            return False
        if self.open_interest_unit is OpenInterestUnit.CONTRACTS:
            return self.contract_multiplier is not None
        return True

    @model_validator(mode="after")
    def _check_type_consistency(self) -> Instrument:
        """Reject field combinations that cannot describe a real instrument.

        Catches adapter bugs at the boundary instead of letting an option with no
        strike, or a spot market with a contract multiplier, reach analytics.
        """
        if self.instrument_type is InstrumentType.OPTION:
            if self.strike is None or self.option_type is None:
                raise ValueError("an option requires both strike and option_type")
            if self.expiry is None:
                raise ValueError("an option requires an expiry")
        if self.instrument_type is InstrumentType.FUTURE and self.expiry is None:
            raise ValueError("a dated future requires an expiry")
        if self.instrument_type is InstrumentType.PERPETUAL and self.expiry is not None:
            raise ValueError("a perpetual must not have an expiry")
        if self.instrument_type is InstrumentType.SPOT:
            if self.contract_multiplier is not None or self.contract_type is not None:
                raise ValueError("a spot instrument has no contract type or multiplier")
            if self.funding_interval_hours is not None:
                raise ValueError("a spot instrument has no funding interval")
        if self.expiry is not None and self.listing_time is not None and self.expiry <= self.listing_time:
            raise ValueError("expiry must be after listing_time")
        return self
