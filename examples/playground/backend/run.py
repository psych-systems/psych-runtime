#!/usr/bin/env python
"""Entry point: ``uv run --extra playground python examples/playground/backend/run.py``.

A plain script rather than ``uvicorn app.main:app`` on the command line so a
person does not have to get their shell's working directory or ``PYTHONPATH``
right first: running this file directly puts its own directory
(``examples/playground/backend/``) on ``sys.path``, which is all ``app`` needs
to import, regardless of where the shell's ``cwd`` happens to be.
"""

from __future__ import annotations

import uvicorn
from app.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
