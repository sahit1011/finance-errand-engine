"""Shared test setup, including the rule that no test may open a connection off this machine."""

import ipaddress
import socket

import pytest

_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        host = address[0]
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise AssertionError(f"test attempted a connection off this machine: {address}")
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def block_off_machine_connections(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
