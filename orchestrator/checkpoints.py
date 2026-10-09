"""Local Sqlite checkpointer.

Production binding (ADR-001 D-05): Aurora PostgreSQL through
`langgraph.checkpoint.postgres.PostgresSaver`, with `thread_id = run_id`.
This sprint does not open a database cluster. SqliteSaver is the local
checkpointer LangGraph documents for a single machine, so an interrupt
can resume after the process still holds the same database file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.sqlite import SqliteSaver


@contextmanager
def open_checkpointer(path: str) -> Iterator[SqliteSaver]:
    with SqliteSaver.from_conn_string(path) as saver:
        yield saver
