"""The guards in docker/postgres/init.sh, which run before anything can fail.

These are the checks that decide whether a deployment ends up with a role that
works, and every one of them exists because the failure it prevents is silent.
A skipped CREATE ROLE produces a database that migrates cleanly and then
refuses to start; a reader left with BYPASSRLS looks scoped while returning
every workspace's rows. None of those say which step went wrong, so the script
has to refuse loudly instead, and these tests are what keeps it refusing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "postgres" / "init.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None, reason="no POSIX shell available"
)


def run_init(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the script's validation with a given environment.

    Only the guards are exercised: every case below is expected to be
    rejected before the script reaches psql, so no database is contacted. The
    environment is emptied first because a developer's own .env must not decide
    whether a guard fires.
    """
    return subprocess.run(
        ["sh", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


BASE: dict[str, str] = {
    "PATH": "/usr/bin:/bin",
    "PGHOST": "postgres",
    "POSTGRES_USER": "retriv",
    "POSTGRES_PASSWORD": "admin-password",
    "POSTGRES_DB": "retriv",
    "RETRIV_ENVIRONMENT": "local",
    "RETRIV_SQL_READER_USER": "retriv_reader",
    "RETRIV_SQL_READER_PASSWORD": "reader-password",
}


def test_the_script_is_syntactically_valid_posix_shell() -> None:
    # A syntax error here would surface only as a container that exits with a
    # parse error, after compose has already reported the role as created.
    assert subprocess.run(["sh", "-n", str(SCRIPT)], check=False).returncode == 0


def test_a_missing_reader_password_stops_before_connecting() -> None:
    """Unset is how compose reports a missing required variable, and it must
    not be read as "no password, carry on"."""
    env = {key: value for key, value in BASE.items() if key != "RETRIV_SQL_READER_PASSWORD"}
    result = run_init(env)
    # `${VAR:?}` exits 1, not the 2 the explicit guards use. Both are non-zero,
    # which is the part that stops a build; only the variable name is asserted.
    assert result.returncode != 0
    assert "RETRIV_SQL_READER_PASSWORD" in result.stderr


@pytest.mark.parametrize("field", ["POSTGRES_USER", "POSTGRES_DB", "RETRIV_SQL_READER_USER"])
def test_a_missing_identifier_stops_before_connecting(field: str) -> None:
    env = {key: value for key, value in BASE.items() if key != field}
    result = run_init(env)
    assert result.returncode != 0
    assert field in result.stderr


def test_the_reader_must_not_be_the_superuser() -> None:
    """POSTGRES_USER is a superuser, and the whole design rests on the reader
    not being one. Pointing both at the same role would leave workspace scoping
    decorative while every query looked properly filtered."""
    result = run_init({**BASE, "RETRIV_SQL_READER_USER": "retriv"})
    assert result.returncode == 2
    assert "superuser" in result.stderr


@pytest.mark.parametrize(
    "identifier",
    ["retriv reader", 'retriv"reader', "retriv-reader", "retriv;drop", "retriv.reader"],
)
def test_identifiers_are_validated_rather_than_escaped(identifier: str) -> None:
    """Identifiers cannot be parameterised. A quote in one would end the
    statement early and turn a typo into a partial success that reports
    itself as a complete one."""
    result = run_init({**BASE, "RETRIV_SQL_READER_USER": identifier})
    assert result.returncode == 2
    assert "Invalid PostgreSQL identifier" in result.stderr


@pytest.mark.parametrize(
    "password",
    ["change-this", "replace-me", "CHANGE_ME", "development"],
)
def test_production_rejects_development_placeholder_passwords(password: str) -> None:
    """The same guard minio-init applies to its own credentials. A placeholder
    reaching production is a known-value login on a role that can read every
    CSV the deployment holds."""
    result = run_init(
        {**BASE, "RETRIV_ENVIRONMENT": "production", "RETRIV_SQL_READER_PASSWORD": password}
    )
    assert result.returncode == 2
    assert "placeholder" in result.stderr


def test_an_empty_identifier_is_rejected() -> None:
    """Empty is caught by the required-value check before validation sees it,
    so it is asserted only as rejected rather than as a validation failure."""
    result = run_init({**BASE, "RETRIV_SQL_READER_USER": ""})
    assert result.returncode != 0
    assert "RETRIV_SQL_READER_USER" in result.stderr


def test_production_accepts_a_real_password() -> None:
    """Only the placeholder check is production-specific; a real password must
    not be rejected for being unremarkable."""
    result = run_init(
        {
            **BASE,
            "RETRIV_ENVIRONMENT": "production",
            "RETRIV_SQL_READER_PASSWORD": "k3y-with-enough-entropy-here",
        }
    )
    # Reaches psql and fails to connect, which is the point: the guard passed.
    assert "placeholder" not in result.stderr


def test_the_script_never_echoes_a_password() -> None:
    """The container logs are readable by anyone who can run docker, so a
    secret in the output would be a secret in a log file."""
    secret = "reader-password"
    source = SCRIPT.read_text()
    # The only interpolations of the password are into psql --set arguments and
    # %L, never into anything printed.
    for line in source.splitlines():
        if "reader_password" in line and not line.lstrip().startswith("#"):
            assert "echo" not in line and "printf" not in line, line
    assert secret not in source