"""Save-time DNS/MX validation removed (issue #524, #516 finding 2).

Recipients of a dead-man's switch are contacted months or years after
configuration save; a save-time DNS/MX result predicts nothing about
release-time deliverability. `services/vigil/dns_check.py` (issue #454
P2.1) is removed rather than kept unreachable.
"""

from __future__ import annotations

import importlib

import pytest


def test_dns_check_module_is_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.vigil.dns_check")
