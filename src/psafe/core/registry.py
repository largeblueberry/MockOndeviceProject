"""Name -> class lookup so config.yaml can pick implementations (DESIGN.md §5.3)."""

import inspect
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str):
        self.kind = kind
        self._items: dict[str, type[T]] = {}

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        def deco(cls: type[T]) -> type[T]:
            if name in self._items:
                raise ValueError(f"{self.kind} '{name}' already registered")
            self._items[name] = cls
            return cls
        return deco

    def create(self, name: str, inject: dict[str, Any] | None = None, **options: Any) -> T:
        """Instantiate `name` with config `options`.

        `inject` holds runtime dependencies (e.g. clock). Each one is passed
        only if the class's constructor declares a parameter with that name,
        so implementations ask for what they need and nothing more.
        """
        if name not in self._items:
            raise KeyError(f"unknown {self.kind} '{name}'. available: {sorted(self._items)}")
        cls = self._items[name]
        params = inspect.signature(cls).parameters
        extra = {k: v for k, v in (inject or {}).items() if k in params}
        clash = set(extra) & set(options)
        if clash:
            raise ValueError(f"{self.kind} '{name}': {sorted(clash)} cannot be set from config")
        return cls(**extra, **options)

    def names(self) -> list[str]:
        return sorted(self._items)
