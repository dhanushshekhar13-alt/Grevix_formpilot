import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.helpers import start_portal  # noqa: E402

PORT = 5077


@pytest.fixture(scope="session")
def portal_url():
    srv = start_portal(PORT)
    yield f"http://127.0.0.1:{PORT}/"
    srv.shutdown()


@pytest.fixture
def settings(tmp_path):
    from agent.config import Settings
    return Settings(coord_mode="pixels", record_video=False, runs_dir=tmp_path, max_steps=40)


@pytest.fixture
def spec(portal_url):
    from agent.config import load_task
    t = load_task("sbee_exam")
    t.url = portal_url
    return t


class Sink:
    def __init__(self):
        self.events = []

    async def __call__(self, e):
        self.events.append(e)

    def of(self, t):
        return [e for e in self.events if e["type"] == t]


@pytest.fixture
def sink():
    return Sink()
