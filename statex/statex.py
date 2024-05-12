from enum import Enum
import functools
from typing import Any, Callable, Optional, Type, TypeVar, Union
import types
import wrapt  # type:ignore
from pyee import EventEmitter
import datetime
import inspect
import threading


class Observer:
    def make_dirty(self): ...


def not_set(_: Any):
    raise NotImplementedError("no set method")


class SxManager:
    """dictates clearing dirty sxs"""

    ...

    def add_dirty(self, sx: "SxField"):
        sx.clear()


global_sx_manager = SxManager()


class SxField(Observer):
    def __init__(
        self,
        key: str | None,
        fget: Callable[[], Any],
        fset: Callable[[Any], None] | None = None,
        deps: Union[list["SxField"], "SxField", None] = None,
        annotation: Type[Any] | None = None,
        sx_manager: SxManager | None = None,
    ):
        self.key = key
        self._get = fget
        self._set = fset or not_set
        self._ee = None
        self.make_dirty_sxs: set[SxField] = set()  # weakref.WeakSet[SxField]()
        self.is_dirty = False
        self.dirty_src: object | None = None
        self.annotation = annotation
        self.sx_manager = sx_manager or global_sx_manager
        if deps:
            deps = [deps] if isinstance(deps, SxField) else deps
            for dep in deps:
                self.add_dependency(dep)

    def get(self) -> Any:
        return self._get()

    def set(self, value: Any) -> None:
        self._set(value)

    def add_dependency(self, sx: "SxField") -> None:
        # if dependency is dirty, make this sx dirty
        sx.make_dirty_sxs.add(self)

    def make_dirty(self, src: object | None = None):
        self.is_dirty = True
        self.dirty_src = src
        for sx in self.make_dirty_sxs:
            sx.make_dirty(src)
        # emit changes to observers
        self.sx_manager.add_dirty(self)
        self.clear()

    def on_change(self, callable: Callable[[Any], None]) -> Callable[[], None]:
        if self._ee is None:
            self._ee = EventEmitter()
        self._ee.on("change", callable)
        return lambda: self._ee.remove_listener("change", callable)  # type:ignore

    def del_change(self, callable: Callable[[Any], None]) -> None:
        if self._ee is None:
            return
        self._ee.remove_listener("change", callable)  # type:ignore

    def clear(self):
        if self.is_dirty:
            if self._ee is not None:
                self._ee.emit("change", self.dirty_src)
            self.is_dirty = False
            self.dirty_src = None

    @property
    def value(self) -> Any:
        return self.get()

    @value.setter
    def value(self, value: Any) -> None:
        self.set(value)

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        fget = functools.partial(self._get, *args, **kwds)
        fset = (
            functools.partial(self._set, *args, **kwds)
            if self._set is not not_set
            else None
        )
        sx = SxField(
            key=f"{str(self.key)}({str(args)},{str(kwds)})",
            fget=fget,
            fset=fset,
        )
        sx.add_dependency(self)
        return sx

    def map(self, func: Callable[[Any, int], Any]) -> "SxField":
        def fget():
            return [func(v, i) for i, v in enumerate(self.get())]

        sx = SxField(
            key=f"{str(self.key)}.map({str(func)})",
            fget=fget,
        )
        sx.add_dependency(self)
        return sx

    def do(self, func: Callable[[Any], Any]) -> "SxField":
        sx = SxField(
            key=f"{str(self.key)}.call({str(func)})",
            fget=lambda: func(self.get()),
        )
        sx.add_dependency(self)
        return sx

    def eq(self, value: Any) -> "SxField":
        sx = SxField(
            key=f"{str(self.key)}.eq({str(value)})",
            fget=lambda: self.get() == value,
        )
        sx.add_dependency(self)
        return sx

    def __repr__(self) -> str:
        return self.get()


class BaseObservable(wrapt.ObjectProxy):  # type:ignore
    def __init__(self, source: Any, root: Optional["BaseObservable"] = None):
        super().__init__(source)  # type:ignore
        self._self_sx_factory = SxFactory(self)
        self._self_root = root or self
        self._self_ee: EventEmitter = EventEmitter()
        # used on root only
        self._self_context: Any = None
        self._self_open_call: Callable[[], None] | None = None
        self._self_close_call: Callable[[], None] | None = None

    def _on_dirty(self, __key: str | None, observer: Observer | Callable[[], None]):
        if __key is None:
            __key = "."
        if isinstance(observer, Observer):
            self._self_ee.on(__key, observer.make_dirty)
        else:
            self._self_ee.on(__key, observer)

    def _make_dirty(self, __key: str | None = None):
        if __key is None:
            __key = "."
        self._self_ee.emit(__key)

    def _change_value(self, value: Any, key: str | None = None) -> Any:
        if value is None:
            return None
        if isinstance(value, BaseObservable):
            return value
        if isinstance(value, dict):
            do = DictObservable(value, root=self)  # type:ignore
            # if dictionary changes, its base object field must be dirty
            if key is not None:
                do._on_dirty(None, lambda: self._make_dirty(key))
            return do
        if isinstance(value, list):
            lo = ListObservable(value, root=self)  # type:ignore
            # if list changes, its base object field must be dirty
            if key is not None:
                lo._on_dirty(None, lambda: self._make_dirty(key))
            return lo
        if isinstance(value, ObjectObservable.SKIP_TYPES):
            return value  # no proxy
        return ObjectObservable(value, root=self)


# thread local storage for current call context
_state_call_stack_local = threading.local()  # TODO: support async?


def proxy_call_wrapper(func: Callable[[Any], None], observable: BaseObservable):
    # replace bounded self in function
    r_func = types.MethodType(func.__func__, observable)  # type:ignore

    @functools.wraps(r_func)
    def wrapper(*args: Any, **kwargs: Any):
        if getattr(_state_call_stack_local, "value", None) is None:
            _state_call_stack_local.value = []
        state_call_stack: list[str] = _state_call_stack_local.value  # type:ignore
        if not state_call_stack:
            if func_ := observable._self_root._self_open_call:  # type:ignore
                func_()
        state_call_stack.append(func.__name__)
        try:
            return r_func(*args, **kwargs)
        finally:
            state_call_stack.pop()
            if not state_call_stack:
                if func_ := observable._self_root._self_close_call:  # type:ignore
                    func_()

    return wrapper

    return r_func


class ObjectObservable(BaseObservable):
    SKIP_TYPES = (
        str,
        int,
        float,
        bool,
        bytes,
        datetime.datetime,
        datetime.date,
        type,
        Enum,
    )

    def __init__(self, source: Any, root: Optional["BaseObservable"] = None):
        super().__init__(source, root)  # type:ignore
        # wrap all object atrributes
        for attr_name, attr_value in source.__dict__.items():
            if (
                attr_name.startswith("_")  # type:ignore
                or attr_value is None
                or isinstance(attr_value, ObjectObservable.SKIP_TYPES)
                or isinstance(attr_value, BaseObservable)
            ):
                continue
            attr_value = self._change_value(attr_value, attr_name)  # type:ignore
            setattr(source, attr_name, attr_value)  # type:ignore
        # wrap class attributes and methods
        for attr_name, attr_value in type(source).__dict__.items():
            if attr_name.startswith("_") or isinstance(
                attr_value, (staticmethod, classmethod)
            ):
                continue
            if inspect.isfunction(attr_value):
                func = getattr(source, attr_name)
                setattr(
                    source,
                    attr_name,
                    proxy_call_wrapper(func, self),
                )
            # class attribute #TODO: not sure if to keep
            else:
                new_value = self._change_value(attr_value, attr_name)  # type:ignore
                if type(new_value) is not type(attr_value):
                    setattr(type(source), attr_name, new_value)  # type:ignore

    def __setattr__(self, name: str, value: Any) -> None:
        """set object attribute"""
        if name.startswith("_"):
            return super().__setattr__(name, value)  # type:ignore
        value = self._change_value(value, name)
        setattr(self.__wrapped__, name, value)  # type:ignore
        self._make_dirty(name)


class DictObservable(BaseObservable):
    def __init__(self, source: Any, root: Optional["BaseObservable"] = None):
        super().__init__(source, root)  # type:ignore
        for key, value in source.items():
            value = self._change_value(value)  # type:ignore
            source[key] = value

    def __setitem__(self, __key: str, value: Any):
        value = self._change_value(value)
        self.__wrapped__[__key] = value  # type:ignore
        self._make_dirty()


class ListObservable(BaseObservable):
    def __init__(self, source: Any, root: Optional["BaseObservable"] = None):
        super().__init__(source, root)  # type:ignore
        for id, value in enumerate(source):
            value = self._change_value(value)  # type:ignore
            source[id] = value

    def __setitem__(self, index: int, value: Any):
        value = self._change_value(value)
        self.__wrapped__[index] = value  # type:ignore
        self._make_dirty()

    def __delitem__(self, index: int):
        self.__wrapped__.__delitem__(index)  # type:ignore
        self._make_dirty()

    def append(self, value: Any):
        value = self._change_value(value)
        self.__wrapped__.append(value)  # type:ignore
        self._make_dirty()

    def pop(self, index: int = -1) -> Any:
        value: Any = self.__wrapped__.pop(index)  # type:ignore
        self._make_dirty()
        return value

    def remove(self, value: Any):
        self.__wrapped__.remove(value)  # type:ignore
        self._make_dirty()


class SxFactory:
    def __init__(self, source: ObjectObservable) -> None:
        self._source = source
        self._type_hints = getattr(
            type(source.__wrapped__), "__annotations__", {}  # type:ignore
        )
        self._sxs: dict[str, SxField] = {}

    def __getattr__(self, __key: str) -> SxField:
        return self.get(__key)

    def get(self, __key: str) -> SxField:
        if __key not in self._sxs:
            value = getattr(self._source, __key)
            if callable(value):
                func = value
                if not hasattr(func, "_ef_sx"):
                    raise TypeError(f"{__key} method is not decorated as sx field")
                sf = SxField(
                    key=__key,
                    fget=func,
                    annotation=inspect.signature(func).return_annotation,
                )
                deps = getattr(value, "_ef_sx_deps", [])
                if isinstance(deps, str):
                    deps = [deps]
                for dep in deps:
                    sf.add_dependency(self.get(dep))
            else:
                sf = SxField(
                    key=__key,
                    fget=lambda: getattr(self._source, __key),
                    fset=lambda v: setattr(self._source, __key, v),
                    annotation=self._type_hints.get(__key),
                )
                self._source._on_dirty(__key, sf)  # type:ignore
            self._sxs[__key] = sf
        return self._sxs[__key]


T = TypeVar("T")


def use_state(cls: Type[T]) -> T:
    state = ObjectObservable(cls())
    return state  # type:ignore


def sx(obj: object) -> SxFactory:
    if not isinstance(obj, BaseObservable):
        raise TypeError("obj must be an instance of BaseObservable")
    return obj._self_sx_factory  # type:ignore


def use_calc(
    fget: Callable[[], Any], deps: list[SxField] | SxField | None = None
) -> SxField:
    sx = SxField(key=f"_use_calc({id(fget)})", fget=fget, deps=deps)
    return sx


def use_sx(
    name: str,
    value: Any | None = None,
    deps: list[SxField] | SxField | None = None,
    *,
    annotation: Type[Any] | None = None,
) -> SxField:
    value_holder = [value]

    def fset(value: Any) -> None:
        value_holder[0] = value  # type:ignore
        sx.make_dirty()

    sx = SxField(
        key=f"use_sx({name})",
        fget=lambda: value_holder[0],
        fset=fset,
        deps=deps,
        annotation=annotation or type(value),
    )
    return sx


def set_sx(sx: SxField, value: Any, src: object | None = None):
    sx.set(value)
    sx.make_dirty(src)


S = TypeVar("S")


def def_sx(deps: str | list[str] | None = None):
    def decorator(func: Callable[..., S]) -> Callable[..., S]:
        setattr(func, "_ef_sx", True)
        setattr(func, "_ef_sx_deps", deps)
        return func

    return decorator