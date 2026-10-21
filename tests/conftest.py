import base64
import json
from pathlib import Path

import pytest
from gen_samples import build as build_samples

from kiosk.settings import Settings
from kiosk.wiring import LocalStack

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture(scope="session")
def params():
    return json.loads((ROOT / "config" / "params.example.json").read_text())


@pytest.fixture(scope="session")
def settings(params):
    return Settings.from_params(params)


@pytest.fixture(scope="session")
def samples():
    return {k: v[0] for k, v in build_samples().items()}


@pytest.fixture
def b64(samples):
    return lambda name: base64.b64encode(samples[name]).decode()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def local(settings, clock):
    return LocalStack(settings, clock=clock)


def consent(svc, decision="accepted"):
    n = svc.s.notice()
    return svc.create_session({"consent": decision, "notice_version": n["notice_version"],
                               "notice_sha256": n["notice_sha256"]})
