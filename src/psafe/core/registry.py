"""Name -> class lookup so config.yaml can pick implementations (DESIGN.md §5.3)."""

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

    def create(self, name: str, **options: Any) -> T:
        if name not in self._items:
            raise KeyError(f"unknown {self.kind} '{name}'. available: {sorted(self._items)}")
        return self._items[name](**options)

    def names(self) -> list[str]:
        return sorted(self._items)
