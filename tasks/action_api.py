"""Eylem REST API (ayri Flask Blueprint). Mevcut /tasks yollarini DEGISTIRMEZ; yalnizca yeni yollar ekler:

  GET  /tasks/actions/meta                     eylem sistemi durumu, araclar, sinirlar
  GET  /tasks/actions[?status=&task_id=&limit=]   liste
  GET  /tasks/actions/audit[?action_id=&limit=]   denetim izi (salt okunur)
  GET  /tasks/actions/<id>                     ayrinti (tam argumanlar + denetim izi)
  POST /tasks/actions/<id>/approve             {"args_hash": "...", "confirm_tainted": true|false}  -> onaylar ve SISTEM calistirir
  POST /tasks/actions/<id>/reject | cancel

Guvenlik ve yanit bicimi api.py ile AYNIDIR (ayni-kaynak/CSRF korumasi, JSON zorunlulugu, {"ok":..} zarfi, ic hata ayrintisi yok).
Kill switch (TASKS_ACTIONS_ENABLED=0, varsayilan): onaylama reddedilir; liste/ayrinti/reddet/iptal calismaya devam eder.
ERISIM: bu bloktaki HER yol yerel istek ya da onay belirteci ister (bkz. action_auth.py).
"""
from flask import Blueprint, g, request

from . import action_auth, action_env, action_specs, api, config, fsafe
from .manager import TaskError, ValidationFailed

bp = Blueprint('gorevler_actions_api', __name__)
bp.before_request(api._guard)


@bp.before_request
def _auth_guard():
    """Erisim denetimi (action_auth.py): BU BLUEPRINT'TEKI HER yol yerel istek ya da gecerli belirtec ister (muaf yol yok; yeni
    eklenen yollar da otomatik kapsanir). Yetki, govde/kimlik dogrulamasindan ONCE kontrol edilir: yetkisiz istek eylemin
    varligini bile ogrenemez."""
    try:
        g.actions_via = action_auth.authorize()
    except action_auth.ActionAuthError as e:
        return api._err(e.code, e.message, e.http, e.field)
    return None
bp.errorhandler(TaskError)(api._h_task)
bp.errorhandler(api._Unavailable)(api._h_unavail)
bp.errorhandler(api._TooLarge)(api._h_large)
bp.errorhandler(Exception)(api._h_any)


def _act():
    a = api.STATE.get('actions')
    if a is None:
        raise api._Unavailable()
    return a


def _qint(name, default=None, lo=None, hi=None):
    v = request.args.get(name)
    if v is None or v == '':
        return default
    try:
        n = int(v)
    except ValueError:
        raise ValidationFailed('Geçersiz sayı.', name)
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise ValidationFailed('Sayı izin verilen aralıkta değil.', name)
    return n


@bp.route('/tasks/actions/meta', methods=['GET'])
def meta():
    a = _act()
    tools = []
    for n in action_specs.names():
        s = action_specs.get(n)
        tools.append({'name': n, 'description': s.doc, 'args': s.args_doc, 'risk': s.risk, 'idempotent': s.idempotent})
    return api._ok({
        'enabled': bool(config.actions_enabled()), 'tools': tools, 'workspace': config.ACTIONS_WORKSPACE,
        'auth': {'mode': 'token' if action_auth.token_state() == 'ok' else 'local-only',
                 'token_configured': action_auth.token_state() != 'none', 'token_too_short': action_auth.token_state() == 'short',
                 'min_token_chars': action_auth.MIN_TOKEN_CHARS},
        'secure_write': bool(fsafe.secure_write_supported()),
        'warnings': action_env.warnings(a.db.path),
        'limits': {'per_run': config.ACTIONS_PER_RUN, 'per_day': config.ACTIONS_PER_DAY, 'pending_max': config.ACTIONS_PENDING_MAX,
                   'args_bytes': config.ACTIONS_ARGS_BYTES, 'ttl_seconds': config.ACTIONS_TTL_SECONDS,
                   'file_max_bytes': config.ACTIONS_FILE_MAX_BYTES, 'file_extensions': list(config.ACTIONS_FILE_EXTS)},
        'counts': a.stats()})


@bp.route('/tasks/actions', methods=['GET'])
def list_actions():
    a = _act()
    return api._ok(a.list(status=request.args.get('status') or None, task_id=_qint('task_id', None, 1),
                          limit=_qint('limit', 50, 1, 200)))


@bp.route('/tasks/actions/audit', methods=['GET'])
def audit():
    a = _act()
    return api._ok(a.audit(_qint('action_id', None, 1), _qint('limit', 200, 1, 500)))


@bp.route('/tasks/actions/<int:aid>', methods=['GET'])
def get_action(aid):
    return api._ok(_act().get(aid))


@bp.route('/tasks/actions/<int:aid>/approve', methods=['POST'])
def approve(aid):
    b = api._body()
    return api._ok(_act().approve(aid, b.get('args_hash'), b.get('confirm_tainted', False), via=g.get('actions_via')))


@bp.route('/tasks/actions/<int:aid>/reject', methods=['POST'])
def reject(aid):
    return api._ok(_act().reject(aid))


@bp.route('/tasks/actions/<int:aid>/cancel', methods=['POST'])
def cancel(aid):
    return api._ok(_act().cancel(aid))
