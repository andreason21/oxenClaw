"""Unit tests for sampyclaw.canvas.store.CanvasStore."""

from __future__ import annotations

import pytest

from sampyclaw.canvas.store import CanvasStore


def test_present_creates_state() -> None:
    store = CanvasStore()
    s = store.present("a", html="<p>hi</p>", title="Hello")
    assert s.html == "<p>hi</p>"
    assert s.title == "Hello"
    assert s.version == 1
    assert s.hidden is False
    assert len(store) == 1


def test_present_increments_version() -> None:
    store = CanvasStore()
    store.present("a", html="<p>1</p>")
    s2 = store.present("a", html="<p>2</p>")
    assert s2.version == 2


def test_hide_sets_hidden_flag() -> None:
    store = CanvasStore()
    store.present("a", html="<p>x</p>")
    s = store.hide("a")
    assert s is not None
    assert s.hidden is True


def test_hide_missing_returns_none() -> None:
    store = CanvasStore()
    assert store.hide("nope") is None


def test_get_returns_state() -> None:
    store = CanvasStore()
    store.present("a", html="<p>x</p>", title="T")
    state = store.get("a")
    assert state is not None
    assert state.title == "T"


def test_lru_evicts_oldest() -> None:
    store = CanvasStore(capacity=2)
    store.present("a", html="<p>a</p>")
    store.present("b", html="<p>b</p>")
    store.present("c", html="<p>c</p>")
    assert store.get("a") is None
    assert store.get("b") is not None
    assert store.get("c") is not None


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        CanvasStore(capacity=0)


def test_clear_removes_state() -> None:
    store = CanvasStore()
    store.present("a", html="<p>x</p>")
    store.clear("a")
    assert store.get("a") is None


def test_to_dict_round_trip_keys() -> None:
    store = CanvasStore()
    s = store.present("a", html="<p>x</p>", title="t")
    d = s.to_dict()
    assert set(d) == {"html", "title", "version", "updated_at", "hidden"}
