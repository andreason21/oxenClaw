"""Static assets shipped with oxenclaw — dashboard SPA + supporting files."""

from importlib.resources import files


def _read_text(name: str) -> str:
    return (files(__package__) / name).read_text(encoding="utf-8")


def dashboard_html() -> str:
    """Compatibility helper — returns the canonical app shell HTML."""
    return _read_text("app.html")


def app_html() -> str:
    return _read_text("app.html")


def app_css() -> str:
    return _read_text("app.css")


def app_js() -> str:
    return _read_text("app.js")
