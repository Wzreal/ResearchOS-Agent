from __future__ import annotations

import pytest
from phase9_fixtures import make_settings

from researchos.application.doctor import DoctorCheckStatus, RealDoctor
from researchos.application.errors import RunConfigurationError
from researchos.configuration.real_settings import default_tavily_capability


class RecordingSecrets:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.lookups: list[str] = []

    def get_secret(self, secret_id: str) -> str | None:
        self.lookups.append(secret_id)
        return self.value


def test_doctor_default_makes_zero_paid_calls_and_writes() -> None:
    secrets = RecordingSecrets("doctor-secret-canary")
    doctor = RealDoctor(settings=make_settings(), secrets=secrets)
    default = doctor.run()
    assert default.paid_calls == 0
    assert default.remote_writes == 0
    assert any(
        check.status is DoctorCheckStatus.PARTIALLY_VERIFIED
        for check in default.checks
    )
    assert "doctor-secret-canary" not in default.model_dump_json()


def test_doctor_checks_real_capability_secret_without_remote_call() -> None:
    secrets = RecordingSecrets("doctor-secret-canary")
    settings = make_settings(
        capability_settings=(default_tavily_capability(),)
    )
    report = RealDoctor(settings=settings, secrets=secrets).run()
    assert "researchos_tavily_api_key" in secrets.lookups
    assert report.paid_calls == 0
    assert report.remote_writes == 0
    assert "doctor-secret-canary" not in report.model_dump_json()


def test_unimplemented_paid_or_write_probe_fails_explicitly() -> None:
    doctor = RealDoctor(
        settings=make_settings(), secrets=RecordingSecrets("secret-canary")
    )
    with pytest.raises(RunConfigurationError, match="not implemented"):
        doctor.run(probe_paid=True)
    with pytest.raises(RunConfigurationError, match="not implemented"):
        doctor.run(probe_writes=True)


@pytest.mark.parametrize("value", [None, "", " ", "\t\r\n"])
def test_doctor_missing_or_blank_secret_fails_without_exposure(value) -> None:
    report = RealDoctor(
        settings=make_settings(), secrets=RecordingSecrets(value)
    ).run()
    secret = next(
        check for check in report.checks if check.check_id == "secret_presence"
    )
    assert secret.status is DoctorCheckStatus.FAIL
    assert secret.code == "secret_missing"
