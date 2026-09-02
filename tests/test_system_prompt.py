"""The system prompt describes the tools the model actually has.

It used to be one fixed block listing every tool group unconditionally —
roughly a thousand tokens explaining a browser bridge, a calendar and a reminder
store to a model that had none of them. Since the default groups no longer
include the assistant surface, an unconditional list is not merely wasteful: a
model told it can call `reminder_add` will try, and burn a turn finding out it
cannot.

The same block said "code" once and "test" not at all, which is a strange thing
for a tool whose main job is editing files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cagentic.engine import fetch_system_prompt_parts
from cagentic.state import AppState
from cagentic.tools import ASSISTANT_GROUPS, DEFAULT_GROUPS


def prompt_for(groups=None) -> str:
    state = AppState(
        workspace=Path.cwd(),
        home=Path.home(),
        tool_groups=set(groups) if groups else None,
    )
    return fetch_system_prompt_parts(state)


BULLETS = {
    "notes": "- **Knowledge base**",
    "reminders": "- **Reminders**",
    "life": "- **Personal OS**",
    "browser": "- **Browser**",
    "files": "- **Files**",
    "coding": "- **Coding**",
}


@pytest.mark.parametrize("group", sorted(ASSISTANT_GROUPS & set(BULLETS)))
def test_disabled_assistant_groups_are_not_described(group):
    assert BULLETS[group] not in prompt_for(DEFAULT_GROUPS)


@pytest.mark.parametrize("group", sorted(ASSISTANT_GROUPS & set(BULLETS)))
def test_enabling_a_group_brings_its_description_back(group):
    assert BULLETS[group] in prompt_for(set(DEFAULT_GROUPS) | {group})


def test_default_groups_are_still_described():
    text = prompt_for(DEFAULT_GROUPS)
    assert BULLETS["files"] in text
    assert BULLETS["coding"] in text


def test_a_files_only_setup_gets_files_and_nothing_else():
    text = prompt_for({"files"})
    assert BULLETS["files"] in text
    for group in ("browser", "life", "reminders", "notes"):
        assert BULLETS[group] not in text


def test_the_coding_workflow_is_present_when_editing_is_possible():
    text = prompt_for(DEFAULT_GROUPS)
    assert "Working on code:" in text
    # The specific instincts that were missing entirely.
    assert "Read a file before you change it" in text
    assert "Verify before you claim" in text


def test_the_coding_workflow_is_absent_without_the_tools():
    assert "Working on code:" not in prompt_for({"web"})


def test_trimming_the_prompt_did_not_cost_the_identity():
    text = prompt_for(DEFAULT_GROUPS)
    assert "personal assistant" in text
    assert "Working principles:" in text
    assert "Environment:" in text


def test_the_default_prompt_is_smaller_than_the_all_groups_one():
    from cagentic.services.compact import approx_tokens

    def size(text: str) -> int:
        return approx_tokens([{"role": "system", "content": text}])

    lean = size(prompt_for(DEFAULT_GROUPS))
    full = size(prompt_for(set(DEFAULT_GROUPS) | set(ASSISTANT_GROUPS)))
    assert lean < full, "describing fewer tools should not cost more tokens"
