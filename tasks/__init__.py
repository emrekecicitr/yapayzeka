"""Otonom Gorev ve Akilli Otomasyon Sistemi.

app.py'den tek cagri ile baglanir:  init_task_system(app, AppBridge(sys.modules[__name__]))
Hata durumunda uygulamanin geri kalani etkilenmez (app.py cagriyi try/except ile sarar).
"""
import logging
import logging.handlers
import os
import time

from . import action_api, api, config, goal_api
from . import memory_api
from .actions import Actions
from .executor import Executor
from .goals import Goals
from .manager import TaskManager
from .memory import Memory
from .notifications import Notifications
from .runs import Runs
from .storage import Database
from . import util
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
    try:
        memory = Memory(db, clock)
    except Exception as e:  # noqa: BLE001 - hafiza bir iyilestirmedir: kurulamazsa gorev sistemi hafizasiz calismaya devam eder
        logging.getLogger('tasks').error('kalici hafiza baslatilamadi, hafizasiz devam ediliyor: %s: %s',
                                         type(e).__name__, e)
        memory = None
    try:
        actions = Actions(db, clock, notifier, memory=memory)
        if config.actions_enabled():
            try:
                from . import action_env
                for w in action_env.warnings(db.path):
                    logging.getLogger('tasks').warning('eylem ortami: %s', w)
            except Exception:  # noqa: BLE001 - uyari uretimi sistemi bozmaz
                pass
    except Exception as e:  # noqa: BLE001 - eylem yetenegi bir iyilestirmedir: kurulamazsa gorev sistemi eylemsiz calismaya devam eder
        logging.getLogger('tasks').error('eylem yetenegi baslatilamadi, eylemsiz devam ediliyor: %s: %s',
                                         type(e).__name__, util.redact(e))
        actions = None
    try:
        goals = Goals(db, clock, mgr, bridge, memory=memory)
        if config.goals_enabled():
            try:
                from . import goals as goals_mod
                for w in goals_mod.environment_warnings(db.path):
                    logging.getLogger('tasks').warning('hedef ortami: %s', w)
            except Exception:  # noqa: BLE001 - uyari uretimi sistemi bozmaz
                pass
    except Exception as e:  # noqa: BLE001 - orkestrasyon bir iyilestirmedir: kurulamazsa gorev sistemi hedefsiz calismaya devam eder
        logging.getLogger('tasks').error('orkestrasyon baslatilamadi, hedefsiz devam ediliyor: %s: %s',
                                         type(e).__name__, util.redact(e))
        goals = None
    executor = Executor(mgr, runs, notifier, bridge, clock, memory=memory, actions=actions)
    runtime = TaskRuntime(mgr, runs, notifier, executor, clock, max_concurrent=max_concurrent,
                          tick_seconds=tick_seconds, actions=actions, goals=goals)
    return Services(db=db, runs=runs, mgr=mgr, notifier=notifier, executor=executor, runtime=runtime,
                    bridge=bridge, clock=clock, memory=memory, actions=actions, goals=goals)


def init_task_system(app, bridge, db_path=None, start=True):
    _setup_logging()
    svc = build_services(bridge, db_path)
    api.STATE.clear()
    api.STATE.update(mgr=svc.mgr, runs=svc.runs, notifier=svc.notifier, runtime=svc.runtime, bridge=bridge,
                     memory=svc.memory, actions=svc.actions, goals=svc.goals)
    if 'gorevler_api' not in app.blueprints:
        app.register_blueprint(api.bp)
    if 'gorevler_memory_api' not in app.blueprints:
        app.register_blueprint(memory_api.bp)
    if 'gorevler_actions_api' not in app.blueprints:
        app.register_blueprint(action_api.bp)
    if 'gorevler_goals_api' not in app.blueprints:
        app.register_blueprint(goal_api.bp)
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
