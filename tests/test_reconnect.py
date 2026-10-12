import asyncio
import pytest
from bridge import meshcore_io as io, state


def test_create_serial_none_sets_reason(monkeypatch):
    async def none_create(*a, **k):
        return None
    monkeypatch.setattr(io.os.path, 'exists', lambda p: True)
    monkeypatch.setattr(io.MeshCore, 'create_serial', none_create)
    with pytest.raises(RuntimeError, match='no response'):
        asyncio.run(io._connect_once())


def test_missing_port_reason(monkeypatch):
    monkeypatch.setattr(io.os.path, 'exists', lambda p: False)
    with pytest.raises(RuntimeError, match='not present'):
        asyncio.run(io._connect_once())


def test_reconnected_event_restores_flag():
    state.serial_connected = False
    class E: payload = {'reconnected': True}
    io._on_connected(E())
    assert state.serial_connected is True
    class D: payload = {'reason': 'x'}
    io._on_disconnect(D())
    assert state.serial_connected is False
    assert 'link lost' in state.last_serial_error[0]
