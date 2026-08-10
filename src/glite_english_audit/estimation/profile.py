"""Calibration profile models and the private cross-run history record.

``TokenUsageProfileEntry`` mirrors the specification 10.1 example field for
field. The committed profile carries only numbers plus public step, runtime,
model, and effort identifiers; no source description or benchmark summary is
ever stored (specification, 3.7). ``CalibrationRecord`` is one completed batch
in the private cross-run history JSONL at
:func:`glite_english_audit.paths.calibration_history_path`; it holds counts
and durations only, never text (specification, 10.3).

Step, runtime, and effort identifiers here are the public kebab-case strings
from the specification 10.1 example (``find-mistakes``, ``claude-code``), not
the :class:`~glite_english_audit.artifacts.enums.AgentRuntime` values, because
the committed profile is a public cross-tool contract keyed by those strings.
"""

import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from glite_english_audit.artifacts.io import read_model
from glite_english_audit.paths import repo_root

PRODUCER_VERSION: str = "0.1.0"

PROFILE_SCHEMA_VERSION: int = 1

# Public identifiers in the committed profile: kebab-case, as in the
# specification 10.1 example. Model IDs additionally allow dots (e.g. a pinned
# provider model string).
_KEBAB_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_MODEL_PATTERN = re.compile(r"^[a-z0-9]+([.-][a-z0-9]+)*$")


def _validate_kebab(value: str) -> str:
    if not _KEBAB_PATTERN.fullmatch(value):
        msg = f"not a kebab-case identifier: {value!r}"
        raise ValueError(msg)
    return value


class TokenUsageProfileEntry(BaseModel):
    """Measured token coefficients for one step/runtime/model/effort cell.

    Field names and meanings follow the specification 10.1 example exactly.
    ``messages_measured == 0`` marks a placeholder cell that was never
    calibrated against real usage; loaders and estimators must treat it as
    low-confidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: str
    runtime: str
    model: str
    effort: str
    messages_measured: int = Field(ge=0)
    average_words_per_message: float = Field(gt=0)
    fixed_input_tokens_per_batch: int = Field(ge=0)
    input_tokens_per_message: float = Field(ge=0)
    input_tokens_per_word: float = Field(ge=0)
    cached_input_tokens_per_message: float = Field(ge=0)
    output_tokens_per_message: float = Field(ge=0)
    retry_rate: float = Field(ge=0, le=1)
    p50_total_tokens_per_message: float = Field(gt=0)
    p90_total_tokens_per_message: float = Field(gt=0)

    @field_validator("step", "runtime", "effort")
    @classmethod
    def _kebab_identifier(cls, value: str) -> str:
        return _validate_kebab(value)

    @field_validator("model")
    @classmethod
    def _model_identifier(cls, value: str) -> str:
        if not _MODEL_PATTERN.fullmatch(value):
            msg = f"not a valid model identifier: {value!r}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _p90_at_least_p50(self) -> "TokenUsageProfileEntry":
        if self.p90_total_tokens_per_message < self.p50_total_tokens_per_message:
            msg = "p90_total_tokens_per_message must be >= p50_total_tokens_per_message"
            raise ValueError(msg)
        return self

    @property
    def is_uncalibrated(self) -> bool:
        """True for placeholder cells never measured against real usage."""
        return self.messages_measured == 0


class TokenUsageProfile(BaseModel):
    """The committed calibration profile: numbers only (specification, 3.7).

    ``entries`` are the cells an estimate may use. ``retired_entries`` are cells
    whose step the pipeline no longer runs: verify-findings was deleted and
    create-safe-records was merged into find-mistakes. Those measurements
    happened and deleting them would destroy a record, but a step that does not
    run must not reach a total the user consents to, so they are kept in a field
    no estimator reads. Nothing else distinguishes the two: a retired cell is
    the same record it always was.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    entries: tuple[TokenUsageProfileEntry, ...]
    retired_entries: tuple[TokenUsageProfileEntry, ...] = ()

    def entry_for(
        self, *, step: str, runtime: str, model: str, effort: str
    ) -> TokenUsageProfileEntry | None:
        """The entry for an exact step/runtime/model/effort cell, if present."""
        for entry in self.entries:
            if (
                entry.step == step
                and entry.runtime == runtime
                and entry.model == model
                and entry.effort == effort
            ):
                return entry
        return None

    def low_confidence_entries(self) -> tuple[TokenUsageProfileEntry, ...]:
        """Entries that were never calibrated (``messages_measured == 0``)."""
        return tuple(entry for entry in self.entries if entry.is_uncalibrated)


def default_profile_path() -> Path:
    """Location of the committed profile inside this repository."""
    return repo_root() / "calibration" / "token-usage-profile.json"


def load_token_usage_profile(path: Path | None = None) -> TokenUsageProfile:
    """Load and validate the committed profile.

    Placeholder entries (``messages_measured == 0``) stay in the profile; they
    are exposed through :meth:`TokenUsageProfile.low_confidence_entries` and
    :attr:`TokenUsageProfileEntry.is_uncalibrated` so estimators label them
    low-confidence instead of dropping them.
    """
    source = path if path is not None else default_profile_path()
    return read_model(source, TokenUsageProfile)


# Partition key per specification 10.3: runtime, pinned model, effort, step,
# skill version, prompt version, schema version, and batching strategy
# (batch size). History from a different partition gets little or no weight.
CalibrationPartitionKey = tuple[str, str, str, str, int, int, int, int]


class CalibrationRecord(BaseModel):
    """One completed batch in the private cross-run calibration history.

    Numbers only: counts, token totals, and duration. No text, path, session
    ID, or source description is ever recorded (specification, 10.3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: str
    model: str
    effort: str
    step: str
    skill_version: int = Field(ge=1)
    prompt_version: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    batch_size: int = Field(ge=1)
    words: int = Field(ge=0)
    utterances: int = Field(ge=0)
    fresh_input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    retries: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    recorded_at: datetime

    @field_validator("step", "runtime", "effort")
    @classmethod
    def _kebab_identifier(cls, value: str) -> str:
        return _validate_kebab(value)

    @field_validator("model")
    @classmethod
    def _model_identifier(cls, value: str) -> str:
        if not _MODEL_PATTERN.fullmatch(value):
            msg = f"not a valid model identifier: {value!r}"
            raise ValueError(msg)
        return value

    @property
    def total_tokens(self) -> int:
        """Fresh input plus cached input plus output tokens for the batch."""
        return self.fresh_input_tokens + self.cached_input_tokens + self.output_tokens

    def partition_key(self) -> CalibrationPartitionKey:
        """The compatibility partition this record belongs to."""
        return (
            self.runtime,
            self.model,
            self.effort,
            self.step,
            self.skill_version,
            self.prompt_version,
            self.schema_version,
            self.batch_size,
        )

    def is_compatible_with(self, other: "CalibrationRecord") -> bool:
        """Whether two records share the same calibration partition."""
        return self.partition_key() == other.partition_key()


def resolve_models(
    profile: TokenUsageProfile, *, runtime: str, processing_profile: str
) -> dict[str, str]:
    """Which measured cell each step is priced against. Not what will run.

    Read that twice, because the function used to be read the other way and
    the manifest, the preflight, and a setup question all repeated it. Steps c,
    d and e run on whatever model the session is running — nothing here selects
    a model, and this repository has no place to select one. What a calibration
    profile can say is which measurements an estimate is computed from, and
    that is all this returns.

    ``recommended`` takes the cheapest measured cell per step; ``maximum-
    assurance`` takes the most expensive. Ordering is by measured p90 cost per
    message, the only cost signal this project has measured; it is a proxy for
    price, not a price.

    What the run actually ran is
    :func:`glite_english_audit.runtime_session.observed_model_ids`, and that is
    what the run manifest freezes. Nothing this function returns may be shown
    to a user as the model that will read their writing.

    Nothing in ``src/`` calls this. The estimate prices against the most
    expensive measured cell, and the run manifest records what the session was
    observed running, so neither needs a per-step map. It is kept because the
    committed profile is keyed per step and a reader comparing the two needs
    something that reads it — but a caller that presents its result to a user as
    what will run would reintroduce the defect this module's history is about,
    and there is no such caller by design.
    """
    if processing_profile not in ("recommended", "maximum-assurance"):
        msg = f"unknown processing profile: {processing_profile!r}"
        raise ValueError(msg)
    cheapest = processing_profile == "recommended"
    chosen: dict[str, str] = {}
    for entry in profile.entries:
        if entry.runtime != runtime:
            continue
        current = chosen.get(entry.step)
        if current is None:
            chosen[entry.step] = entry.model
            continue
        rival = next(
            e
            for e in profile.entries
            if e.runtime == runtime and e.step == entry.step and e.model == current
        )
        better = (
            entry.p90_total_tokens_per_message < rival.p90_total_tokens_per_message
            if cheapest
            else entry.p90_total_tokens_per_message > rival.p90_total_tokens_per_message
        )
        if better:
            chosen[entry.step] = entry.model
    return chosen
