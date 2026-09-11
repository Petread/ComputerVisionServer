from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from cv_core import PlatformSettings

_session_factories: dict[str, sessionmaker[Session]] = {}
_session_factory_pids: dict[str, int | None] = {}


def get_session_factory(settings: PlatformSettings) -> sessionmaker[Session]:
    cache_key = settings.service.name
    current_pid = os.getpid()
    existing_factory = _session_factories.get(cache_key)
    owner_pid = _session_factory_pids.get(cache_key)
    if existing_factory is not None and (owner_pid is None or owner_pid == current_pid):
        return existing_factory
    if existing_factory is None or owner_pid != current_pid:
        engine = create_engine(
            settings.database.sqlalchemy_dsn(),
            pool_pre_ping=True,
            pool_size=settings.database.pool_size,
        )
        _session_factories[cache_key] = sessionmaker(bind=engine, expire_on_commit=False)
        _session_factory_pids[cache_key] = current_pid
    return _session_factories[cache_key]


@contextmanager
def session_scope(settings: PlatformSettings) -> Generator[Session, None, None]:
    session = get_session_factory(settings)()
    session.info["audit_root"] = settings.storage.audit_root
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
