"""Regression guard for the backend image's installed Python packages."""

from __future__ import annotations

from pathlib import Path


_DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def test_backend_image_installs_and_imports_declared_packages() -> None:
    lines = [line.strip() for line in _DOCKERFILE.read_text().splitlines()]
    source_copy = lines.index("COPY . .")
    install = lines.index("RUN pip install --no-cache-dir .")
    import_check = lines.index(
        'RUN /usr/local/bin/python -I -B -c "import app.cli; import mcp_server"'
    )

    assert source_copy < install < import_check
