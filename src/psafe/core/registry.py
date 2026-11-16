"""이름 -> 클래스 조회표. config.yaml에서 구현체를 고를 수 있게 한다 (DESIGN.md §5.3)."""

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
        """`name`에 해당하는 클래스를 설정의 `options`로 생성한다.

        `inject`에는 실행 중 의존성(예: clock)이 들어 있다. 각 값은 클래스
        생성자에 같은 이름의 매개변수가 있을 때만 전달되므로, 구현체는
        필요한 것만 요청할 수 있다.
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
