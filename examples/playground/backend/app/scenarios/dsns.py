"""Where the real databases ``scripts/dev-services.sh`` starts actually live.

``tests/conftest.py`` reads ``PSYCH_TEST_POSTGRES_DSN``, ``PSYCH_TEST_MYSQL_DSN``
and ``PSYCH_TEST_DYNAMODB_ENDPOINT`` and skips a test outright when one is
unset -- correct for a test, wrong for a demo a person just asked to run,
which should work the moment ``scripts/dev-services.sh start`` has, with no
extra exporting. So each getter here falls back to exactly what that script
sets up (see its own source: the ``psych_test`` Postgres database, the
``psych``/``psych`` MySQL role, DynamoDB Local on ``:8000``) and only asks the
environment to override it.
"""

from __future__ import annotations

import os

_DEFAULT_POSTGRES_DSN = "postgresql://postgres@127.0.0.1:5432/psych_test"
_DEFAULT_MYSQL_DSN = "mysql://psych:psych@127.0.0.1:3306/psych_test"
_DEFAULT_DYNAMODB_ENDPOINT = "http://127.0.0.1:8000"


def postgres_dsn() -> str:
    return os.environ.get("PSYCH_TEST_POSTGRES_DSN", _DEFAULT_POSTGRES_DSN)


def mysql_dsn() -> str:
    return os.environ.get("PSYCH_TEST_MYSQL_DSN", _DEFAULT_MYSQL_DSN)


def dynamodb_endpoint() -> str:
    return os.environ.get("PSYCH_TEST_DYNAMODB_ENDPOINT", _DEFAULT_DYNAMODB_ENDPOINT)
