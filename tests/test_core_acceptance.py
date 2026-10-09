from __future__ import annotations

import pytest

from fractalmem.core.node import HMRNode
from fractalmem.core.tree import HMRTreeEngine
from fractalmem.core.view import View, ViewPersistenceError


def test_no_force_override_and_invalid_append_state() -> None:
    engine = HMRTreeEngine()
    with pytest.raises(TypeError):
        engine.sawtooth_batch_merge(force=True)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        engine.append_message("x", status="bogus")


def test_snapshot_pair_boolean_cannot_impersonate_integer() -> None:
    snapshot = View("default", 1, (HMRNode.create_leaf(0, "x"),)).to_dict()
    snapshot["view_pairs"] = [[False, False]]
    with pytest.raises(ViewPersistenceError):
        View.from_dict(snapshot)
