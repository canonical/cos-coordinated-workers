#!/usr/bin/env python3
# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Interfaces module."""

import importlib

__all__ = [
    "ClusterProvider",
    "ClusterRequirer",
]

current_package = __package__

class _LazyModule:
    def __init__(self, module_name: str):
        self.module_name = module_name
        self.module = None

    def _load(self):
        if self.module is None:
            self.module = importlib.import_module(self.module_name, current_package)
        return self.module

    def __getattr__(self, item: str):
        module = self._load()
        return getattr(module, item)

ClusterProvider = _LazyModule(".cluster")
ClusterRequirer = _LazyModule(".cluster")
