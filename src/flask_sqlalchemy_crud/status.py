from enum import IntEnum


class SQLStatus(IntEnum):
    """SQLAlchemy 操作状态。"""

    OK = 0
    SQL_ERR = 1
    INTERNAL_ERR = 2
    NOT_FOUND = 5
