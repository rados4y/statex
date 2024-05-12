import typing as t
from statex import SxField


# test support class
class ChangeTracker:
    tracked_value: dict[t.Any, t.Any] = {}
    tracked_src: dict[t.Any, t.Any] = {}

    def track(self, key: t.Any, sx: SxField):
        def on_change(source: t.Any):
            self.tracked_value[key] = sx.get()
            self.tracked_src[key] = source

        sx.on_change(on_change)

    def assert_set(self, key: t.Any, value: t.Any, source: t.Any = ...):
        assert self.tracked_value.get(key) == value
        if source is not ...:
            assert self.tracked_src.get(key) is source
        self.tracked_value.pop(key)
        self.tracked_src.pop(key)
