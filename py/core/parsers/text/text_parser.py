# type: ignore
from typing import AsyncGenerator

from core.base.abstractions import DataType
from core.base.parsers.base_parser import AsyncParser


class TextParser(AsyncParser[DataType]):
    """A parser for raw text data."""

    async def ingest(
        self, data: DataType, *args, **kwargs
    ) -> AsyncGenerator[DataType, None]:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        yield data
