"""The Minecraft bench's programs (scripts/mcbench/tasks) use only names the program API and their inputs give them:
the check the handheld makes before anything moves, so an API change can't break the bench unnoticed."""

import ast
from pathlib import Path

import pytest

from relaymcp.device import behave

TASKS = Path(__file__).resolve().parents[1] / "scripts" / "mcbench" / "tasks"
INPUTS = {  # what scripts/mcbench/bench.py and mcgym.py pass each program (params.args)
    "step": {"STEPS", "PITCHES"},
    "pursuit": {"RATE", "PITCH", "DUR", "ACQUIRE", "ANGLE_LAG"},
    "reaction": {"KIND", "WAIT_S", "HOLD"},
    "course": {"WPS", "TOL", "STOP_MS", "MODE"},
    "house": set(),
}


@pytest.mark.parametrize("task", sorted(INPUTS))
def test_bench_programs_use_only_the_program_api_and_their_inputs(task):
    tree = ast.parse((TASKS / f"{task}.py").read_text(), f"{task}.py")
    behave.preflight(tree, set(behave.Program.NAMES) | INPUTS[task], what=task)


def test_a_misspelled_api_name_is_caught():
    with pytest.raises(ValueError, match="did you mean 'face_point'"):
        behave.preflight(ast.parse("face_pint(1, 2, 3)"), set(behave.Program.NAMES))


def test_every_task_is_checked():
    assert {p.stem for p in TASKS.glob("*.py")} == set(INPUTS)
