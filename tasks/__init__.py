"""Otonom Gorev ve Akilli Otomasyon Sistemi.

app.py'den tek cagri ile baglanir:  init_task_system(app, AppBridge(sys.modules[__name__]))
Hata durumunda uygulamanin geri kalani etkilenmez (app.py cagriyi try/except ile sarar).
"""
import logging
import logging.handlers
import os
import time

from . import api, config
from .executor import Executor
from .manager import TaskManager
from .notifications import Notifications
from .runs import Runs
from .storage import Database
from .worker import TaskRuntime

_FORK_HOOK = {'done': False}


class Services:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _setup_logging():
    lg = logging.getLogger('tasks')
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    lg.propagate = False
    try:
        h = logging.handlers.RotatingFileHandler(
            config.LOG_PATH, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUPS, encoding='utf-8')
    except OSError:
        h = logging.StreamHandler()
    h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    lg.addHandler(h)
    return lg


def build_services(bridge, db_path=None, clock=time.time, max_concurrent=None, tick_seconds=None):
    """Servisleri kurar (blueprint/thread baslatmaz). Testler dogrudan bunu kullanir."""
    db = Database(db_path or config.DB_PATH)
    db.migrate()
    runs = Runs(db, clock)
    models = [m['id'] for m in bridge.models()] if hasattr(bridge, 'models') else []
    mgr = TaskManager(db, runs, clock, allowed_models=models)
    notifier = Notifications(db, clock)
    executor = Executor(mgr, runs, notifier, bridge, clock)
    runtime = TaskRuntime(mgr, runs, notifier, executor, clock, max_concurrent=max_concurrent,
                          tick_seconds=tick_seconds)
    return Services(db=db, runs=runs, mgr=mgr, notifier=notifier, executor=executor, runtime=runtime,
                    bridge=bridge, clock=clock)


def init_task_system(app, bridge, db_path=None, start=True):
    _setup_logging()
    svc = build_services(bridge, db_path)
    api.STATE.clear()
    api.STATE.update(mgr=svc.mgr, runs=svc.runs, notifier=svc.notifier, runtime=svc.runtime, bridge=bridge)
    if 'gorevler_api' not in app.blueprints:
        app.register_blueprint(api.bp)
    if start:
        svc.runtime.start()
        if hasattr(os, 'register_at_fork') and not _FORK_HOOK['done']:
            _FORK_HOOK['done'] = True
            os.register_at_fork(after_in_child=lambda: _restart_after_fork(svc))
    logging.getLogger('tasks').info('gorev sistemi hazir (db=%s)', svc.db.path)
    return svc


def _restart_after_fork(svc):
    try:
        svc.db.close_all()
        svc.runtime._thread = None
        svc.runtime.start()
    except Exception:
        pass
