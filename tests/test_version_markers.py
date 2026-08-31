"""The spec version is written down in three places; they have to agree.

Found while adding the state cache: `__init__.__spec_version__` said
0.6.0 while `client._SPEC_VERSION` and `pyproject.toml` said 0.8.0. The
middle one is what goes out on every request as the User-Agent, so the
value a server sees and the value a reader sees had drifted apart with
nothing to notice.
"""
from __future__ import annotations

import pathlib
import tomllib

import dmzagent
from dmzagent import client as client_module

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _pinned() -> str:
    with (_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["tool"]["dmzagent"]["spec-version"]


def test_the_three_spec_version_markers_agree():
    assert dmzagent.__spec_version__ == client_module._SPEC_VERSION == _pinned()


def test_the_user_agent_carries_the_pinned_version():
    """This is the marker a server actually sees."""
    cx = dmzagent.DMZAgent(api_key="ck_test_x")
    try:
        assert cx._client.headers["User-Agent"] == f"dmzagent-python/{_pinned()}"
    finally:
        cx.close()
