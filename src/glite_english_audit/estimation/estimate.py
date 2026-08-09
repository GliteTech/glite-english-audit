"""CLI: estimate every period preset before the user is asked to choose one.

Run: ``uv run python -m glite_english_audit.estimation.estimate``

Specification 2.4 requires an estimate for every preset *before* the period
question: candidate words, a processing-time range, a token range, and either
a price and quota figure or a clear statement that neither is available.
Without this command an agent can only say "many hours", which is the answer
the requirement exists to prevent.

This module reads the same pending inventory
:mod:`glite_english_audit.pipeline.start_run` later adopts and applies that
module's selection rules, so the estimate describes the run the user is about
to start rather than a different set of sources.

Interpolation rule
------------------
Discovery reports one candidate count and one date range per source instance,
never a per-day histogram. A preset window therefore takes each instance's
counts in proportion to how much of that instance's own earliest-to-latest
span the window covers, assuming candidates are spread evenly across the span.
Real writing is bursty and usually heavier lately, so a short recent window is
more often understated than overstated. An instance whose span is a single
moment counts in full when that moment falls inside the window. An instance
with no date range cannot be placed in time at all: it counts in full for
every window, which overstates short ones, and such instances are counted in
the output so the caller can say so. Only ``everything`` is exact; every other
preset word count is an estimate, not a measurement.

Output is aggregate numbers only: no label, path, or text (specification 2.3).
"""

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from glite_english_audit.artifacts.enums import AgentRuntime
from glite_english_audit.artifacts.envelope import as_utc, utc_now
from glite_english_audit.artifacts.io import read_model
from glite_english_audit.artifacts.models import SourceInstanceRecord
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.estimation.estimator import (
    HIGH_CONFIDENCE_MIN_RECORDS,
    SAFE_RECORD_UNITS_PER_MESSAGE,
    STEP_CREATE_SAFE_RECORDS,
    STEP_FIND_MISTAKES,
    STEP_VERIFY_FINDINGS,
    VERIFY_UNITS_PER_MESSAGE,
    EstimateConfidence,
    PresetEstimate,
    TimeRange,
    TokenEstimate,
    apply_confidence,
    apply_time_confidence,
    confidence_for,
    estimate_run,
    estimate_stage,
    estimate_unit_time,
    format_token_range,
    profile_batches,
    render_estimate_report,
)
from glite_english_audit.estimation.profile import (
    TokenUsageProfile,
    TokenUsageProfileEntry,
    load_token_usage_profile,
)
from glite_english_audit.paths import pending_inventory_dir
from glite_english_audit.pipeline.start_run import (
    INVENTORY_NAME,
    PERIOD_PRESETS,
    resolve_period,
    resolve_selection,
)

PRODUCER_VERSION: str = "0.1.0"

# What the user reads in the first column. Keys are the preset identifiers
# start_run accepts, so the agent can pass the answer straight to --period.
PRESET_LABELS: dict[str, str] = {
    "last-7-days": "Last 7 days",
    "last-30-days": "Last 30 days",
    "last-3-months": "Last 3 months",
    "last-year": "Last year",
    "everything": "Everything",
}

# Specification 2.4 lists custom dates as a sixth option whose numbers cannot
# exist yet. It is a table row only: it is not a preset, so it carries no
# estimate in the machine-readable output.
CUSTOM_ROW_LABEL: str = "Custom dates"
CUSTOM_ROW_TEXT: str = "Calculated after dates are entered"

QUOTA_UNAVAILABLE_NOTE: str = (
    "Quota and price are unavailable: this client does not read subscription limits or "
    "provider prices, so no percentage or price range is shown."
)


class WindowCounts(BaseModel):
    """Candidate volume attributed to one period window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    words: int = Field(ge=0)
    utterances: int = Field(ge=0)
    undated_instances: int = Field(ge=0)


class StepUnits(BaseModel):
    """Units each semantic step processes for one window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    find_mistakes: int = Field(ge=0)
    verify_findings: int = Field(ge=0)
    create_safe_records: int = Field(ge=0)

    @property
    def total(self) -> int:
        """Units across all three steps."""
        return self.find_mistakes + self.verify_findings + self.create_safe_records


class RuntimeSteps(BaseModel):
    """The three calibrated semantic cells used for one runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    find_mistakes: TokenUsageProfileEntry
    verify_findings: TokenUsageProfileEntry
    create_safe_records: TokenUsageProfileEntry

    def entries(self) -> tuple[TokenUsageProfileEntry, ...]:
        """The three entries in pipeline order."""
        return (self.find_mistakes, self.verify_findings, self.create_safe_records)


class PresetRow(BaseModel):
    """One preset's estimate, as the agent receives it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preset: str
    label: str
    words: int = Field(ge=0)
    utterances: int = Field(ge=0)
    tokens: TokenEstimate
    minutes: TimeRange
    confidence: EstimateConfidence


class EstimateReport(BaseModel):
    """The whole preset comparison: numbers, caveats, and a table to show."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: str
    producer_version: str
    profile_schema_version: int
    computed_at: datetime
    selected_instances: int
    undated_instances: int
    concurrent_batches: int
    presets: tuple[PresetRow, ...]
    notes: tuple[str, ...]
    table: str


def profile_runtime_id(runtime: AgentRuntime) -> str:
    """The kebab-case runtime identifier the committed profile is keyed by."""
    return runtime.value.replace("_", "-")


def window_fraction(
    record: SourceInstanceRecord, *, start: datetime, end: datetime
) -> float | None:
    """Share of one instance's candidates that falls inside a window.

    ``None`` when the instance carries no date range and cannot be placed in
    time. See the module docstring for the assumption this makes.
    """
    if record.earliest_timestamp is None or record.latest_timestamp is None:
        return None
    first = as_utc(record.earliest_timestamp)
    last = as_utc(record.latest_timestamp)
    if last < first:
        first, last = last, first
    span_seconds = (last - first).total_seconds()
    if span_seconds <= 0:
        return 1.0 if start <= first <= end else 0.0
    overlap = (min(last, end) - max(first, start)).total_seconds()
    return max(0.0, min(1.0, overlap / span_seconds))


def window_counts(
    records: list[SourceInstanceRecord], *, start: datetime, end: datetime
) -> WindowCounts:
    """Interpolate candidate words and messages for one period window."""
    words = 0
    utterances = 0
    undated = 0
    for record in records:
        fraction = window_fraction(record, start=start, end=end)
        if fraction is None:
            undated += 1
            fraction = 1.0
        words += round(record.candidate_words * fraction)
        utterances += round(record.candidate_messages * fraction)
    return WindowCounts(words=words, utterances=utterances, undated_instances=undated)


def step_units(utterances: int) -> StepUnits:
    """Units each step processes for a window of ``utterances`` messages.

    Only the first step sees every message; verification sees the findings
    produced and safe-record creation sees the findings retained, both scaled
    by the measured ratios in :mod:`estimator`.
    """
    return StepUnits(
        find_mistakes=utterances,
        verify_findings=math.ceil(utterances * VERIFY_UNITS_PER_MESSAGE),
        create_safe_records=math.ceil(utterances * SAFE_RECORD_UNITS_PER_MESSAGE),
    )


def _most_expensive_entry(
    profile: TokenUsageProfile, *, step: str, runtime: str
) -> TokenUsageProfileEntry:
    """The conservative cell for a step when several models or efforts exist."""
    candidates = [
        entry for entry in profile.entries if entry.step == step and entry.runtime == runtime
    ]
    if not candidates:
        msg = f"the calibration profile has no {step!r} entry for runtime {runtime!r}"
        raise ValueError(msg)
    return max(candidates, key=lambda entry: entry.p90_total_tokens_per_message)


def select_runtime_steps(profile: TokenUsageProfile, *, runtime: str) -> RuntimeSteps:
    """Pick the profile cells this runtime will use.

    A runtime with several model or effort variants for one step gets the most
    expensive one, so the range never understates the run that actually
    happens.
    """
    return RuntimeSteps(
        find_mistakes=_most_expensive_entry(profile, step=STEP_FIND_MISTAKES, runtime=runtime),
        verify_findings=_most_expensive_entry(profile, step=STEP_VERIFY_FINDINGS, runtime=runtime),
        create_safe_records=_most_expensive_entry(
            profile, step=STEP_CREATE_SAFE_RECORDS, runtime=runtime
        ),
    )


def entry_confidence(entry: TokenUsageProfileEntry) -> EstimateConfidence:
    """Confidence of one committed cell, before any local run history.

    Local calibration history (specification, 10.3) is not folded in yet: no
    run writes it, so the only evidence is the committed sample size.
    """
    return confidence_for(entry, compatible_history_records=profile_batches(entry))


def steps_confidence(steps: RuntimeSteps) -> EstimateConfidence:
    """A run is only as calibrated as its least calibrated step."""
    if all(entry_confidence(entry) is EstimateConfidence.HIGH for entry in steps.entries()):
        return EstimateConfidence.HIGH
    return EstimateConfidence.LOW


def estimate_tokens(counts: WindowCounts, steps: RuntimeSteps) -> TokenEstimate:
    """Total tokens across the three semantic steps for one window.

    Only the first step is fed the real word count; the later steps read
    findings rather than source text, so they are estimated at their own
    calibrated average length instead of being scaled by source words.
    """
    units = step_units(counts.utterances)
    downstream = (
        (units.verify_findings, steps.verify_findings),
        (units.create_safe_records, steps.create_safe_records),
    )
    stages = [estimate_stage(counts.words, units.find_mistakes, steps.find_mistakes)]
    stages.extend(
        estimate_stage(round(count * entry.average_words_per_message), count, entry)
        for count, entry in downstream
    )
    return estimate_run(stages)


def estimate_preset(
    *,
    preset: str,
    counts: WindowCounts,
    steps: RuntimeSteps,
    concurrent_batches: int,
) -> PresetRow:
    """One table row: words, utterances, tokens, minutes, and confidence."""
    confidence = steps_confidence(steps)
    tokens = apply_confidence(estimate_tokens(counts, steps), confidence)
    minutes = apply_time_confidence(
        estimate_unit_time(
            step_units(counts.utterances).total, concurrent_batches=concurrent_batches
        ),
        confidence,
    )
    return PresetRow(
        preset=preset,
        label=PRESET_LABELS.get(preset, preset),
        words=counts.words,
        utterances=counts.utterances,
        tokens=tokens,
        minutes=minutes,
        confidence=confidence,
    )


def build_notes(
    *, steps: RuntimeSteps, runtime: str, undated_instances: int, concurrent_batches: int
) -> tuple[str, ...]:
    """The caveats that must reach the user with the numbers."""
    notes = [
        "Words and messages are candidates, counted before stage 3 drops text you did not "
        "write, and interpolated from each source's date range. Only Everything is exact.",
    ]
    if undated_instances:
        notes.append(
            f"{undated_instances} source instances report no date range, so they count in "
            "full in every period and overstate the short ones."
        )
    notes.append(
        "Most estimated tokens are cached input re-read on each turn, not fresh input, "
        "so the totals are large and are not billed at the fresh-input rate."
    )
    notes.append(
        "Covers the three measured model steps. Stage 3, which decides which text you "
        "wrote, has no calibrated cell yet, so its cost is missing from these totals."
    )
    uncalibrated = [entry.step for entry in steps.entries() if entry.is_uncalibrated]
    low = [
        entry.step
        for entry in steps.entries()
        if entry_confidence(entry) is EstimateConfidence.LOW and not entry.is_uncalibrated
    ]
    if uncalibrated:
        notes.append(
            f"Never measured for {runtime}: {', '.join(uncalibrated)}. Those numbers are "
            "extrapolated, not measured, so the range is widened and marked low confidence."
        )
    if low:
        notes.append(
            f"Fewer than {HIGH_CONFIDENCE_MIN_RECORDS} measured batches for {', '.join(low)}, "
            "so the total stays low confidence and its upper bound is widened."
        )
    notes.append(QUOTA_UNAVAILABLE_NOTE)
    if concurrent_batches == 1:
        notes.append(
            "Times assume one batch at a time; running N batches in parallel divides them "
            "by roughly N."
        )
    else:
        notes.append(f"Times assume {concurrent_batches} batches running in parallel.")
    return tuple(notes)


def _expected_use(row: PresetRow) -> str:
    """The last table column: tokens, plus the confidence word when earned."""
    rendered = f"{format_token_range(row.tokens)} tokens"
    if row.confidence is EstimateConfidence.LOW:
        return f"{rendered}, low confidence"
    return rendered


def render_table(rows: list[PresetRow], *, notes: tuple[str, ...]) -> str:
    """The specification 2.4 table for these rows, with its caveats attached."""
    presets = [
        PresetEstimate(
            period=row.label,
            words=row.words,
            time=row.minutes,
            expected_use=_expected_use(row),
        )
        for row in rows
    ]
    presets.append(PresetEstimate(period=CUSTOM_ROW_LABEL, expected_use=CUSTOM_ROW_TEXT))
    return render_estimate_report(presets, notes=notes)


def build_report(
    *,
    inventory_dir: Path,
    runtime: AgentRuntime,
    include_sources: list[str] | None = None,
    exclude_sources: list[str] | None = None,
    exclude_labels: list[str] | None = None,
    profile_path: Path | None = None,
    concurrent_batches: int = 1,
    now: datetime | None = None,
) -> EstimateReport:
    """Estimate every preset for the selection the user is about to make."""
    moment = now if now is not None else utc_now()
    inventory = read_model(inventory_dir / INVENTORY_NAME, PrivateInventory)
    selected = set(
        resolve_selection(
            inventory,
            include_sources=include_sources,
            exclude_sources=exclude_sources,
            exclude_labels=exclude_labels,
        )
    )
    records = [record for record in inventory.records if record.instance_key in selected]
    if not records:
        msg = "no eligible source instance was selected; nothing to estimate"
        raise ValueError(msg)
    profile = load_token_usage_profile(profile_path)
    runtime_id = profile_runtime_id(runtime)
    steps = select_runtime_steps(profile, runtime=runtime_id)

    rows: list[PresetRow] = []
    undated = 0
    for preset in PERIOD_PRESETS:
        period = resolve_period(preset, moment)
        counts = window_counts(records, start=period.start, end=period.end)
        undated = max(undated, counts.undated_instances)
        rows.append(
            estimate_preset(
                preset=preset,
                counts=counts,
                steps=steps,
                concurrent_batches=concurrent_batches,
            )
        )
    notes = build_notes(
        steps=steps,
        runtime=runtime_id,
        undated_instances=undated,
        concurrent_batches=concurrent_batches,
    )
    return EstimateReport(
        runtime=runtime_id,
        producer_version=PRODUCER_VERSION,
        profile_schema_version=profile.schema_version,
        computed_at=moment,
        selected_instances=len(records),
        undated_instances=undated,
        concurrent_batches=concurrent_batches,
        presets=tuple(rows),
        notes=notes,
        table=render_table(rows, notes=notes),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints aggregate numbers and a table, never text."""
    parser = argparse.ArgumentParser(
        description="Estimate words, time, and tokens for every period preset"
    )
    parser.add_argument(
        "--inventory-dir",
        type=Path,
        default=None,
        help="defaults to the inventory discovery left pending",
    )
    parser.add_argument(
        "--include-source",
        action="append",
        default=None,
        metavar="APP",
        help="add every found instance of this app, by public ID or the name shown "
        "to the user (repeatable)",
    )
    parser.add_argument(
        "--exclude-source",
        action="append",
        default=None,
        metavar="APP",
        help="drop every instance of this app (repeatable)",
    )
    parser.add_argument(
        "--exclude-label",
        action="append",
        default=None,
        metavar="LABEL",
        help='drop one instance by the label the user saw, such as "Claude Code 4" (repeatable)',
    )
    parser.add_argument("--runtime", default="claude_code", choices=[r.value for r in AgentRuntime])
    parser.add_argument(
        "--concurrent-batches",
        type=int,
        default=1,
        help="batches the orchestration will run at once; 1 is the conservative default",
    )
    arguments = parser.parse_args(argv)

    try:
        report = build_report(
            inventory_dir=(
                arguments.inventory_dir
                if arguments.inventory_dir is not None
                else pending_inventory_dir()
            ),
            runtime=AgentRuntime(arguments.runtime),
            include_sources=arguments.include_source,
            exclude_sources=arguments.exclude_source,
            exclude_labels=arguments.exclude_label,
            concurrent_batches=arguments.concurrent_batches,
        )
    except FileNotFoundError:
        sys.stderr.write(
            "no source inventory found; run discovery first: "
            "uv run python -m glite_english_audit.discovery.inventory\n"
        )
        return 2
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    sys.stdout.write(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
