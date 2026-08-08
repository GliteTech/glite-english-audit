"""The review URL the skill tells the user must be the one the server serves.

A skill that announces a different address sends the user to a 404 at the last
step of the audit, after every other stage has already succeeded.
"""

import re
from pathlib import Path

_SKILL = Path(__file__).resolve().parent.parent / "skills" / "prepare-glite-submission" / "SKILL.md"
_TOKEN_PATH_FORM = re.compile(r"http://127\.0\.0\.1:\d+/t/[\w-]+/")


def test_server_url_uses_the_token_path_form() -> None:
    from glite_english_audit.review_server.server import _LOOPBACK_HOST

    assert _LOOPBACK_HOST == "127.0.0.1"
    source = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "glite_english_audit"
        / "review_server"
        / "server.py"
    ).read_text(encoding="utf-8")
    assert 'f"http://{_LOOPBACK_HOST}:{self.port}/t/{self.token}/"' in source


def test_skill_example_matches_the_served_form() -> None:
    text = _SKILL.read_text(encoding="utf-8")
    assert _TOKEN_PATH_FORM.search(text), "the skill must show the /t/<token>/ address form"


def test_skill_does_not_teach_the_query_string_form() -> None:
    text = _SKILL.read_text(encoding="utf-8")
    assert "?token=" not in text, "the query-string address form is not served and returns 404"
