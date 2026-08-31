"""The union merge that makes concurrent data pushes safe."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "commit_data", Path(__file__).parent.parent / "scripts" / "commit_data.py"
)
commit_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(commit_data)
union_csv = commit_data.union_csv

HEADER = "observed_at,route_id,total_price"


def csv(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def test_rows_from_both_writers_survive():
    ours = csv("a,r,1", "b,r,2")
    theirs = csv("a,r,1", "c,r,3")
    merged = union_csv(ours, theirs)
    assert merged == csv("a,r,1", "c,r,3", "b,r,2")


def test_the_remote_ordering_is_preserved_and_ours_appended():
    """History reads chronologically, so replayed rows go on the end."""
    merged = union_csv(csv("new,r,9"), csv("old,r,1", "older,r,2"))
    assert merged.splitlines()[1:] == ["old,r,1", "older,r,2", "new,r,9"]


def test_identical_rows_are_not_duplicated():
    same = csv("a,r,1")
    assert union_csv(same, same) == same


def test_a_new_file_survives_an_empty_remote():
    ours = csv("a,r,1")
    assert union_csv(ours, "") == ours


def test_header_comes_from_the_remote_side():
    merged = union_csv(csv("a,r,1"), csv("b,r,2"))
    assert merged.splitlines()[0] == HEADER


def test_rows_the_remote_already_has_are_not_re_added():
    ours = csv("a,r,1", "b,r,2")
    theirs = csv("a,r,1", "b,r,2", "c,r,3")
    assert union_csv(ours, theirs) == theirs
