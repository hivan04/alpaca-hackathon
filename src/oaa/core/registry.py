"""A tiny generic plugin registry.

Used for strategies, brokers, agents and partner adapters, so adding any of
them is a decorator plus a config entry - never an edit to the core pipeline.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterator
from typing import Generic, TypeVar

from oaa.core.errors import ConfigError

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, type[T]] = {}

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        def decorator(cls: type[T]) -> type[T]:
            key = name.strip().lower()
            if key in self._items and self._items[key] is not cls:
                raise ConfigError(f"duplicate {self.kind} registered under '{key}'")
            self._items[key] = cls
            cls.registry_name = key
            return cls

        return decorator

    def add(self, name: str, cls: type[T]) -> None:
        self._items[name.strip().lower()] = cls

    def get(self, name: str) -> type[T]:
        key = name.strip().lower()
        if key not in self._items:
            known = ", ".join(sorted(self._items)) or "<none>"
            raise ConfigError(f"unknown {self.kind} '{name}'. Registered: {known}")
        return self._items[key]

    def names(self) -> list[str]:
        return sorted(self._items)

    def __contains__(self, name: str) -> bool:
        return name.strip().lower() in self._items

    def __iter__(self) -> Iterator[tuple[str, type[T]]]:
        return iter(sorted(self._items.items()))

    def __len__(self) -> int:
        return len(self._items)

    def autoload(self, package: str) -> None:
        """Import every module in a package so its decorators run."""
        try:
            mod = importlib.import_module(package)
        except ModuleNotFoundError:
            return
        for _, name, _ in pkgutil.iter_modules(mod.__path__):
            if name.startswith("_"):
                continue
            importlib.import_module(f"{package}.{name}")
