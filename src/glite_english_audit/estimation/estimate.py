"""CLI: estimate every period preset before the user is asked to choose one.

Run: ``uv run python -m glite_english_audit.estimation.estimate``

Specification 2.4 requires an estimate for every preset *before* the period
question: candidate words, a processing-time range, a token range, and either
a price and quota figure or a clear statement that neither is available.
Without this command an agent can only say "many hours", which is the answer
the requirement exists to prevent.

All of it is in the JSON. The rendered table is narrower: the two numbers a
person weighs when picking a period, words and time, plus the caveats. Tokens
stay in the JSON for the preflight, where the run skill quotes them beside the
subscription figure that gives them a denominator; the statement that no price
is available is a note rather than a column repeated on every row.

The JSON also carries ``session``: which model and effort this session is
running, and which the numbers were measured on. The preflight states the
first, because the user is about to let a model read everything they wrote.
It is an observation — the per-file agents inherit the session's model, and
nothing in this product selects one — so it is ``null`` when it cannot be read
rather than filled in from the calibration profile.

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
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from glite_english_audit.artifacts.enums import AgentRuntime
from glite_english_audit.artifacts.envelope import as_utc, utc_now
from glite_english_audit.artifacts.io import read_model
from glite_english_audit.artifacts.models import SourceInstanceRecord
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.estimation.estimator import (
    ASSUMED_MESSAGES_PER_SESSION,
    AUTHORED_UTTERANCE_RETENTION,
    AUTHORED_WORD_RETENTION,
    CONFIDENTIALITY_UNITS_PER_CALL,
    STEP_CONFIRM_CONFIDENTIALITY,
    STEP_FIND_MISTAKES,
    STEP_JUDGE_AUTHORSHIP,
    EstimateConfidence,
    PresetEstimate,
    TimeRange,
    TokenEstimate,
    apply_confidence,
    apply_time_confidence,
    confidence_for,
    estimate_run,
    estimate_step,
    estimate_unit_time,
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
from glite_english_audit.runtime_session import detect_effort, detect_model

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

# Two tests decide whether a caveat belongs here. Would a different value change
# what the user does next? Does it answer "could this hurt me?" A note that fails
# both is the maintainer's audit trail printed at a person deciding whether to
# spend an afternoon, and it costs the attention of the notes that pass.
#
# Nine notes failed that pass and left three. What went, and why: the model steps
# the estimate covers and the share of words each reads (the user picks a period,
# not a step); that most of the tokens are cached input (an apology for a number
# no longer shown); how many samples back each calibration cell and which model
# and effort measured them (step, model, and effort names are this repository's
# vocabulary, and their whole user-facing meaning is "rough", which one note now
# says); the assumed messages per session (the user cannot change their session
# lengths, and its only consequence — the run may cost more — is that same note);
# parallelism (the user does not choose it, and the run skill already forbids
# repeating it); and the note explaining why two rows were identical, which went
# with the duplicate row it apologized for.

# Interpolation, and what the word count is a count of. Both change how a reader
# reads the Words column, which is the column a period is chosen on.
ESTIMATE_NOTE: str = (
    "Estimates, not measurements: the shorter periods are worked out from each app's date "
    "range, and only Everything is exact. The word counts include text you pasted, which the "
    "audit reads but does not correct."
)

# The direction the numbers are wrong in, which is the direction that costs the
# user something. Calibration is thin, sessions are assumed to be average length
# when short ones cost more, and a recent window interpolated from a long span is
# more often understated than overstated. One sentence covers all three, because
# what the reader does about them is the same: leave room.
#
# "Measured elsewhere" rather than "measured a few times": a runtime with no
# measurement of its own borrows another's cells, and telling that user their
# numbers rest on a few measured runs would claim a measurement nobody took.
# Elsewhere is true of every case this note prints in — too few samples, a
# different model, a different effort, or nothing measured at all.
UNDERSTATED_NOTE: str = (
    "The run can take longer and use more than the ranges show. They rest on a handful of "
    "runs measured elsewhere, not on yours."
)

# Money, which is the "could this hurt me?" the numbers cannot answer. It does
# not mention the subscription allowance: that is readable on some hosts
# (:mod:`glite_english_audit.subscription`) and the run skill shows it at the
# preflight, where announcing its absence is already ruled out as noise. Saying
# "quota and price are unavailable" here claimed both were unreadable, and the
# more useful half of that was false.
NO_PRICE_NOTE: str = "No price is available, so the cost of this run in money is unknown."


class WindowCounts(BaseModel):
    """Candidate volume attributed to one period window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    words: int = Field(ge=0)
    utterances: int = Field(ge=0)
    undated_instances: int = Field(ge=0)


class StepUnits(BaseModel):
    """Units each semantic step processes for one window.

    Steps c and d count utterances; step e counts session files, because that
    is what one of its agents opens and what the measurement priced.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    judge_authorship: int = Field(ge=0)
    find_mistakes: int = Field(ge=0)
    confirm_confidentiality: int = Field(ge=0)

    @property
    def total(self) -> int:
        """Units across all three model steps."""
        return self.judge_authorship + self.find_mistakes + self.confirm_confidentiality


class RuntimeSteps(BaseModel):
    """The three calibrated semantic cells used for one runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    judge_authorship: TokenUsageProfileEntry
    find_mistakes: TokenUsageProfileEntry
    confirm_confidentiality: TokenUsageProfileEntry

    def entries(self) -> tuple[TokenUsageProfileEntry, ...]:
        """The three entries in pipeline order."""
        return (
            self.judge_authorship,
            self.find_mistakes,
            self.confirm_confidentiality,
        )


class SessionModel(BaseModel):
    """The model this session is running, beside the ones that were measured.

    Two different facts, and the preflight has to state both. ``model`` and
    ``effort`` are observations of the session that will do the work — the
    per-file agents inherit it, and nothing in this product selects one.
    ``measured_models`` and ``measured_efforts`` describe where the numbers in
    this report come from. ``measured_elsewhere`` is true when they are not the
    same, which is the case the user has to be told about in plain words.

    ``model`` is ``None`` when detection found nothing. That is reported as not
    known, never filled in with the profile's model: naming a model the run
    does not choose is the defect this field exists to end.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None
    effort: str | None
    measured_models: tuple[str, ...]
    measured_efforts: tuple[str, ...]
    measured_elsewhere: bool


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
    session: SessionModel
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

    Only authorship judgment sees every candidate message; mistake-finding sees
    what that judgment kept. The confidentiality check runs once per session
    file, empty or not, so it is counted in sessions — the measured run found
    no relation between a session's record count and its cost.
    """
    analyzed = math.ceil(utterances * AUTHORED_UTTERANCE_RETENTION)
    return StepUnits(
        judge_authorship=utterances,
        find_mistakes=analyzed,
        confirm_confidentiality=math.ceil(utterances / ASSUMED_MESSAGES_PER_SESSION),
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
        judge_authorship=_most_expensive_entry(
            profile, step=STEP_JUDGE_AUTHORSHIP, runtime=runtime
        ),
        find_mistakes=_most_expensive_entry(profile, step=STEP_FIND_MISTAKES, runtime=runtime),
        confirm_confidentiality=_most_expensive_entry(
            profile, step=STEP_CONFIRM_CONFIDENTIALITY, runtime=runtime
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

    Authorship judgment is fed every candidate word, because deciding which
    words the learner wrote requires reading all of them. Mistake-finding is
    fed only the share that judgment keeps. The confidentiality check reads
    mistake records rather than source text, so it is estimated at its own
    calibrated average length, and one of its calls covers one session file.
    """
    units = step_units(counts.utterances)
    analyzed_words = round(counts.words * AUTHORED_WORD_RETENTION)
    confidentiality = steps.confirm_confidentiality
    return estimate_run(
        [
            estimate_step(counts.words, units.judge_authorship, steps.judge_authorship),
            estimate_step(analyzed_words, units.find_mistakes, steps.find_mistakes),
            estimate_step(
                round(units.confirm_confidentiality * confidentiality.average_words_per_message),
                units.confirm_confidentiality,
                confidentiality,
                messages_per_call=CONFIDENTIALITY_UNITS_PER_CALL,
            ),
        ]
    )


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


def distinct_rows(rows: Sequence[PresetRow]) -> list[PresetRow]:
    """The rows worth printing: one per distinct window.

    When someone's oldest message is three months old, the last-three-months,
    last-year, and everything rows carry identical numbers, and a table that
    prints all three offers the same run three times. It used to print them and
    add a caveat explaining that they were identical on purpose — a note whose
    only job was to defend the rows above it.

    Everything is the row kept, for two reasons: it is the only one counted
    rather than interpolated, and the narrower labels promise a limit that does
    not happen. Every preset stays in the machine-readable output, so a caller
    that is handed "last-year" can still price it.
    """
    everything = next((row for row in rows if row.preset == "everything"), None)
    if everything is None or everything.words == 0:
        return list(rows)
    return [
        row
        for row in rows
        if row.preset == "everything"
        or row.words != everything.words
        or row.utterances != everything.utterances
    ]


def build_notes(
    *, steps: RuntimeSteps, session: SessionModel, undated_instances: int
) -> tuple[str, ...]:
    """The caveats that must reach the user with the numbers.

    Three, or four when a source cannot be placed in time. Each one is here
    because a number above it is read wrongly without it, and because the reader
    can do something about it: read the Words column for what it counts, leave
    room above the ranges, and stop waiting for a price.
    """
    notes = [ESTIMATE_NOTE]
    if undated_instances:
        # One source takes a singular noun, verb, and pronoun. The count drives
        # the whole sentence, so nothing here may be left in the plural.
        if undated_instances == 1:
            notes.append(
                "1 source reports no dates, so it counts in full in every period and "
                "overstates the short ones."
            )
        else:
            notes.append(
                f"{undated_instances} sources report no dates, so they count in full in "
                "every period and overstate the short ones."
            )
    if steps_confidence(steps) is EstimateConfidence.LOW or session.measured_elsewhere:
        notes.append(UNDERSTATED_NOTE)
    notes.append(NO_PRICE_NOTE)
    return tuple(notes)


def describe_session(steps: RuntimeSteps) -> SessionModel:
    """The running session beside the cells these numbers were measured on.

    The calibration profile is keyed by model and effort, and nothing compared
    either against the session actually running. On the machine this was written
    the profile assumed one model at medium effort while the session ran a
    different model at xhigh, so every hour and token described a run the user
    would not get. Neither the profile nor anything else chooses that model:
    the per-file agents inherit it from the session, which is why this is
    reported and not resolved.

    ``measured_elsewhere`` drives one user-facing note and one preflight
    sentence. It used to be a note naming both models and both efforts. Model
    IDs and an effort level are this repository's vocabulary, not the user's,
    and the user cannot pick either at the period question; what the mismatch
    means to them there is that the numbers may be off, which is what
    :data:`UNDERSTATED_NOTE` says. At the preflight it means something else and
    the model is named: the user is about to agree to let a model read
    everything they have written, and which model that is is the most
    privacy-relevant fact in the run.

    Sample count alone would not do as the trigger: once every cell is measured
    ten times the run is called calibrated, and a profile measured on another
    model would then say so silently.

    The comparison is per cell, not against the set of everything measured. The
    profile holds cells measured on more than one model, and asking only
    whether the running model appears somewhere in that set would call an
    estimate calibrated when two of its three steps were measured elsewhere —
    the caveat would disappear exactly as it started to matter.

    Not a mismatch when detection finds nothing: an unknown session is not
    evidence of one.
    """
    running_model = detect_model()
    running_effort = detect_effort()
    wrong_model = running_model is not None and any(
        entry.model != running_model for entry in steps.entries()
    )
    wrong_effort = running_effort is not None and any(
        entry.effort != running_effort for entry in steps.entries()
    )
    return SessionModel(
        model=running_model,
        effort=running_effort,
        measured_models=tuple(sorted({entry.model for entry in steps.entries()})),
        measured_efforts=tuple(sorted({entry.effort for entry in steps.entries()})),
        measured_elsewhere=wrong_model or wrong_effort,
    )


def render_table(rows: list[PresetRow], *, notes: tuple[str, ...]) -> str:
    """The specification 2.4 table for these rows, with its caveats attached."""
    presets = [
        PresetEstimate(period=row.label, words=row.words, time=row.minutes)
        for row in distinct_rows(rows)
    ]
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
        msg = "no source was selected, so there is nothing to estimate"
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
    session = describe_session(steps)
    notes = build_notes(steps=steps, session=session, undated_instances=undated)
    return EstimateReport(
        runtime=runtime_id,
        producer_version=PRODUCER_VERSION,
        profile_schema_version=profile.schema_version,
        computed_at=moment,
        selected_instances=len(records),
        undated_instances=undated,
        concurrent_batches=concurrent_batches,
        session=session,
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
        help="defaults to the inventory that discovery left pending",
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
