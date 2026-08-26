from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from app.services.questionnaire_taxonomy import (
    QUESTIONNAIRE_VERSION,
    VALID_ASSET_SCALES,
    VALID_HORIZONS,
    VALID_INTEL_FOCUSES,
    VALID_MARKETS,
    VALID_OBJECTIVES,
    VALID_RISK_APPETITES,
    VALID_SECTORS,
    VALID_STYLES,
)

# (field name, allowed values) for the six single-select dimensions.
_SINGLE_SELECT_FIELDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("asset_scale", VALID_ASSET_SCALES),
    ("style", VALID_STYLES),
    ("horizon", VALID_HORIZONS),
    ("risk_appetite", VALID_RISK_APPETITES),
    ("objective", VALID_OBJECTIVES),
    ("intel_focus", VALID_INTEL_FOCUSES),
)
# (field name, allowed values) for the two multi-select dimensions.
_MULTI_SELECT_FIELDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("markets", VALID_MARKETS),
    ("sectors_of_interest", VALID_SECTORS),
)


class QuestionnaireIn(BaseModel):
    """The 8 closed-enum questionnaire dimensions (Ring 1-B design.md §8.3).

    This is the only user-writable entry point for these fields (PUT
    /investment-context takes this straight from the client) — validated
    here, not just DB-CHECKed, so an out-of-taxonomy value 422s instead of
    surfacing as a raw IntegrityError/500 (same boundary argument as
    ParsedRow's currency/asset_class validators in app/schemas/holdings.py).
    """

    asset_scale: str
    markets: list[str]
    style: str
    horizon: str
    risk_appetite: str
    sectors_of_interest: list[str]
    objective: str
    intel_focus: str

    @field_validator("asset_scale", "style", "horizon", "risk_appetite", "objective", "intel_focus")
    @classmethod
    def _single_select_must_be_known(cls, v: str, info: ValidationInfo) -> str:
        field_name = info.field_name or ""
        allowed = dict(_SINGLE_SELECT_FIELDS)[field_name]
        if v not in allowed:
            raise ValueError(f"unrecognized {field_name} {v!r} — not in the closed enum")
        return v

    @field_validator("markets", "sectors_of_interest")
    @classmethod
    def _multi_select_must_be_known(cls, v: list[str], info: ValidationInfo) -> list[str]:
        field_name = info.field_name or ""
        allowed = dict(_MULTI_SELECT_FIELDS)[field_name]
        unknown = [item for item in v if item not in allowed]
        if unknown:
            raise ValueError(
                f"unrecognized {field_name} value(s) {unknown!r} — not in the closed enum"
            )
        return v


class QuestionnaireOut(QuestionnaireIn):
    """Same shape as the input — the user reads back exactly what they
    submitted (§8.4: no system-inference readback endpoint exists at all)."""


class InvestmentContextIn(BaseModel):
    """PUT /investment-context body: questionnaire + free text, submitted
    together as one overwrite (Concept §4.2 — reanswering replaces the row)."""

    questionnaire: QuestionnaireIn
    # Concept §4.2 — "系统给予最高尊重": no format requirement, no length cap
    # beyond what the DB/encryption layer already tolerates, no filtering.
    free_text: str | None = None


class InvestmentContextOut(BaseModel):
    """GET /investment-context response — the user's own answers only, never
    any system-inferred conclusion (§8.4/§1.4: stricter than even the
    display-preference hiding in Concept §4.2, since this hides the product's
    own analysis stance, not just a profile guess about the user)."""

    model_config = ConfigDict(from_attributes=True)

    questionnaire: QuestionnaireOut
    questionnaire_version: str
    free_text: str | None
    updated_at: datetime


DEFAULT_QUESTIONNAIRE_VERSION = QUESTIONNAIRE_VERSION
