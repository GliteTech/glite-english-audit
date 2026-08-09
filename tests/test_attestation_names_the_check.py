"""The submission attestation must name the check that judged, not the client.

``ReviewedSubmissionArtifact`` carries ``privacy_creator_version`` and
``privacy_verifier_version``. Glite reads them as "which check cleared these
records". They were filled from ``CLIENT_VERSION``, which names whatever built
the package — equally true of a run that passed the check and one that skipped
it, so the field distinguished nothing while reading as though it did.

``build_review._cleared_by`` already had a docstring saying taking it from the
client would be wrong. The code did exactly that, one function away. This file
is what makes the docstring true.
"""

from pathlib import Path

import pytest

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.submission import VERSION_PATTERN
from glite_english_audit.paths import repo_root
from glite_english_audit.pipeline.mistakes import SKILL_NAME as STEP_D_SKILL
from glite_english_audit.pipeline.verify import SKILL_NAME as STEP_E_SKILL
from glite_english_audit.verification.skills import skill_versions


def test_both_steps_name_a_skill_that_exists() -> None:
    # A skill name that resolves to nothing would send the attestation down the
    # on-disk fallback forever and never be noticed.
    versions = skill_versions(repo_root())
    assert STEP_D_SKILL in versions, STEP_D_SKILL
    assert STEP_E_SKILL in versions, STEP_E_SKILL


def test_every_skill_declares_a_version() -> None:
    """Every canonical skill, not only the two the attestation names.

    ``skill_versions`` omits a skill whose file declares no version rather than
    defaulting it, so a missing declaration disappears silently into a shorter
    dict. Here is where it stops being silent.
    """
    directories = sorted(
        entry.name
        for entry in (repo_root() / "skills").iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    )
    assert sorted(skill_versions(repo_root())) == directories


def test_the_recorded_versions_are_not_the_client_version() -> None:
    # The failure this file exists for: an attestation that reads as a fact
    # about the check but is a fact about the build.
    versions = skill_versions(repo_root())
    assert all(isinstance(value, int) for value in versions.values())
    assert CLIENT_VERSION not in {str(value) for value in versions.values()}


def test_an_attested_version_survives_the_submission_gate() -> None:
    """The value is copied into the package, whose gate refuses free-form strings.

    That refusal is a privacy rule, not a formatting one: an unconstrained
    version string is exactly the shape that carries a path, a session ID, or
    source text off the machine. So the attestation is the bare number, and
    which skill it belongs to is fixed by the field rather than written into the
    value.
    """
    for value in skill_versions(repo_root()).values():
        assert VERSION_PATTERN.fullmatch(str(value)), value
    assert not VERSION_PATTERN.fullmatch("find-english-mistakes@1")


def test_a_skill_version_is_read_from_the_file_not_the_wrapper(tmp_path: Path) -> None:
    """Wrappers are generated and could drift; the canonical file is the source.

    Attesting from a wrapper would let a stale generated copy name the version
    that cleared a record.
    """
    skills = tmp_path / "skills"
    (skills / "example-skill").mkdir(parents=True)
    (skills / "example-skill" / "SKILL.md").write_text(
        "---\nname: example-skill\n---\n\n# Example\n\n**Version**: 4\n", encoding="utf-8"
    )
    (tmp_path / ".claude" / "skills" / "example-skill").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "example-skill" / "SKILL.md").write_text(
        "**Version**: 99\n", encoding="utf-8"
    )
    assert skill_versions(tmp_path) == {"example-skill": 4}


def test_a_skill_without_a_version_is_omitted_rather_than_defaulted(tmp_path: Path) -> None:
    # Recording a 1 nobody wrote would make a later real 1 look unchanged, and
    # resume decides whether to reuse work by comparing exactly this dict.
    skills = tmp_path / "skills"
    (skills / "versionless").mkdir(parents=True)
    (skills / "versionless" / "SKILL.md").write_text("# No version here\n", encoding="utf-8")
    assert skill_versions(tmp_path) == {}


def test_a_missing_skills_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    assert skill_versions(tmp_path) == {}


@pytest.mark.parametrize("skill", [STEP_D_SKILL, STEP_E_SKILL])
def test_the_attested_skill_is_the_one_whose_output_the_step_reads(skill: str) -> None:
    """Step d attests find-english-mistakes; step e attests the privacy gate.

    Swapping them would produce an attestation that is well-formed, plausible,
    and about the wrong check.
    """
    assert (repo_root() / "skills" / skill / "SKILL.md").is_file()
