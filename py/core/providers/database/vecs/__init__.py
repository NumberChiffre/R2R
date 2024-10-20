from . import exc
from .client import Client
from .collection import Collection

__project__ = "vecs"
__version__ = "0.4.2"


__all__ = [
    "Collection",
    "Client",
    "exc",
]


def create_client(connection_string: str, *args, **kwargs) -> Client:
    """Creates a client from a Postgres connection string"""
    return Client(connection_string, *args, **kwargs)
