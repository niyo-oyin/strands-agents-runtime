"""定義に書かれた ``pkg.module:attr`` / ``/abs/path.py:attr`` 形式の参照を Python オブジェクトに解決する。"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, TypeVar

from strands_runtime.definitions.loader import is_path_reference, split_reference
from strands_runtime.errors import ReferenceResolutionError

T = TypeVar("T")


def _import_file(path: Path) -> Any:
    """``.py`` ファイルをモジュールとして読み込む。同じファイルは 1 度だけ読み、``sys.modules`` で共有する。"""
    # パスから決まる名前で登録し、2 回目以降は読み込み済みのモジュールを返す（モジュール内の状態を共有するため）。
    module_name = f"_strands_runtime_ref_{abs(hash(str(path)))}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ReferenceResolutionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        # 読み込みに失敗したモジュールを残すと、次回は壊れた状態のものが返るので取り除く。
        del sys.modules[module_name]
        raise ReferenceResolutionError(f"error importing {path}: {exc}") from exc
    return module


def load_attribute(reference: str) -> Any:
    """``module:attr`` が指すオブジェクトを返す。``module`` はモジュールパスか、絶対パスの ``.py`` ファイル。"""
    module_part, attr = split_reference(reference)
    if not module_part or not attr:
        raise ReferenceResolutionError(f"{reference!r} must be of the form 'module:attribute'")
    if is_path_reference(module_part):
        module = _import_file(Path(module_part))
    else:
        try:
            module = importlib.import_module(module_part)
        except ImportError as exc:
            raise ReferenceResolutionError(f"cannot import module {module_part!r}: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise ReferenceResolutionError(f"{module_part!r} has no attribute {attr!r}") from exc


def resolve_instance[T](reference: str, expected: type[T]) -> T:
    """参照を ``expected`` 型の「インスタンス」に解決する。

    参照先はインスタンスそのものでも、引数なしで生成できるクラスでも、引数なしのファクトリ関数でもよい。
    定義ファイル側は名前を書くだけで済み、複雑な設定はコード側で行える。
    """
    obj = load_attribute(reference)
    if isinstance(obj, expected):
        return obj
    if inspect.isclass(obj) or callable(obj):
        try:
            produced = obj()
        except TypeError as exc:
            raise ReferenceResolutionError(f"{reference!r} could not be called without arguments: {exc}") from exc
        if isinstance(produced, expected):
            return produced
        raise ReferenceResolutionError(
            f"{reference!r} returned {type(produced).__name__}, expected {expected.__name__}"
        )
    raise ReferenceResolutionError(f"{reference!r} is a {type(obj).__name__}, expected {expected.__name__}")


def resolve_class[T](reference: str, expected: type[T]) -> type[T]:
    """参照を ``expected`` のサブクラスに解決する（構造化出力の pydantic モデルなどに使う）。"""
    obj = load_attribute(reference)
    if inspect.isclass(obj) and issubclass(obj, expected):
        return obj
    raise ReferenceResolutionError(f"{reference!r} is not a subclass of {expected.__name__}")
