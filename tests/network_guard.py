"""Deny unmocked Python network access in the unittest process and its children."""
from __future__ import annotations

import os
import socket
import urllib.request
from pathlib import Path
from unittest.mock import patch


class NetworkAccessBlocked(BaseException):
    """Unmocked network access is forbidden during the test suite."""


def _blocked(operation: str):
    raise NetworkAccessBlocked(
        f"network access blocked by unittest guard at {operation}; mock the provider call"
    )


def _blocked_urlopen(*_args, **_kwargs):
    _blocked("urllib.request.urlopen")


def _blocked_socket_connect(_sock, *_args, **_kwargs):
    _blocked("socket.socket.connect")


def _blocked_socket_connect_ex(_sock, *_args, **_kwargs):
    _blocked("socket.socket.connect_ex")


def _blocked_sendto(_sock, *_args, **_kwargs):
    _blocked("socket.socket.sendto")


def _blocked_create_connection(*_args, **_kwargs):
    _blocked("socket.create_connection")


def _blocked_getaddrinfo(*_args, **_kwargs):
    _blocked("socket.getaddrinfo")


def _blocked_gethostbyname(*_args, **_kwargs):
    _blocked("socket.gethostbyname")


def install_network_guard() -> None:
    if getattr(socket, "_rocket_hunter_test_network_guard", False):
        return
    test_dir = str(Path(__file__).resolve().parent)
    inherited_paths = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    if test_dir not in inherited_paths:
        os.environ["PYTHONPATH"] = os.pathsep.join(
            [test_dir, *(path for path in inherited_paths if path)]
        )

    guard_patches = (
        patch.object(urllib.request, "urlopen", _blocked_urlopen),
        patch.object(socket.socket, "connect", _blocked_socket_connect),
        patch.object(socket.socket, "connect_ex", _blocked_socket_connect_ex),
        patch.object(socket.socket, "sendto", _blocked_sendto),
        patch.object(socket, "create_connection", _blocked_create_connection),
        patch.object(socket, "getaddrinfo", _blocked_getaddrinfo),
        patch.object(socket, "gethostbyname", _blocked_gethostbyname),
    )
    for guard_patch in guard_patches:
        guard_patch.start()
    socket._rocket_hunter_test_network_guard = True
