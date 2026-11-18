import pytest

from agent.store import Store


@pytest.fixture
async def store(tmp_path):
    result = Store(tmp_path / "state.sqlite")
    await result.open()
    try:
        yield result
    finally:
        await result.close()
