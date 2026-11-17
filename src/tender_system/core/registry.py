"""Instance-local, typed registries for adapters, recipe steps and transforms."""


class Registry[T]:
    def __init__(self) -> None:
        self._plugins: dict[str, T] = {}

    def register(self, name: str, plugin: T) -> None:
        if not name or not name.strip() or name != name.strip():
            raise ValueError("plugin name must be nonblank and trimmed")
        if name in self._plugins:
            raise ValueError(f"plugin already registered: {name}")
        self._plugins[name] = plugin

    def get(self, name: str) -> T:
        try:
            return self._plugins[name]
        except KeyError:
            raise KeyError(f"unknown plugin: {name}") from None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins))
