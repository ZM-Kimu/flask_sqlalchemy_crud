"""核心类型与别名导出模块。

用于集中 re-export CRUD / CRUDQuery / SQLStatus 及相关类型别名，方便上层统一导入。
"""

from __future__ import annotations

from .crud import CRUD
from .query import CRUDQuery
from .status import SQLStatus
from .types import ErrorLogger, EntityTypeVar, ModelTypeVar, ResultTypeVar

__all__ = [
    "CRUD",
    "CRUDQuery",
    "SQLStatus",
    "ErrorLogger",
    "ModelTypeVar",
    "ResultTypeVar",
    "EntityTypeVar",
]
