"""Lightweight DOM presence checks on the bundled dashboard SPA."""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "oxenclaw" / "static"


def test_app_html_has_canvas_panel() -> None:
    html = (_STATIC / "app.html").read_text("utf-8")
    assert 'id="canvas-panel"' in html
    assert 'id="canvas-frame"' in html


def test_canvas_iframe_is_sandboxed() -> None:
    html = (_STATIC / "app.html").read_text("utf-8")
    # Sandbox must be present and must NOT include allow-same-origin
    # (that would let agent JS read parent cookies/storage).
    assert 'sandbox="allow-scripts' in html
    assert "allow-same-origin" not in html


def test_canvas_panel_starts_hidden() -> None:
    html = (_STATIC / "app.html").read_text("utf-8")
    assert 'id="canvas-panel" class="canvas-panel" hidden' in html


def test_app_js_binds_canvas_panel() -> None:
    js = (_STATIC / "app.js").read_text("utf-8")
    assert "function bindCanvasPanel(" in js
    assert "canvas.eval_result" in js


def test_canvas_css_present() -> None:
    css = (_STATIC / "app.css").read_text("utf-8")
    assert ".canvas-panel" in css
    assert ".canvas-panel__frame" in css
