"""REST API (Flask Blueprint). Mevcut endpoint'lerle cakismaz; yalnizca /tasks ... yollarini ekler.

Yanit bicimi: {"ok": true, "data": ...}  veya  {"ok": false, "error": {"code", "message", "field"?}}
Guvenlik
- Durum degistiren isteklerde (POST/PATCH/DELETE): Content-Type application/json zorunlu (tarayicida tarayicilar arasi
  basit form saldirisini engeller), Origin/Sec-Fetch-Site ayni kaynakla dogrulanir (CSRF), govde boyutu sinirlidir.
- Gorev ID'leri tam sayi donusturucusuyla alinir; olmayan ID 404 doner.
- Uygulamada kullanici hesabi/oturum modeli YOKTUR (tek kullanicili kisisel uygulama); bu nedenle sahiplik kontrolu
  yerine yukaridaki ayni-kaynak korumasi uygulanir. Cok kullanicili kullanima acilirsa kimlik dogrulama eklenmelidir.
- Istemciye ic hata ayrintisi (stack, yol, anahtar) asla verilmez.
"""
import logging
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request

from . import config, util
from .manager import TaskError, ValidationFailed, NotFound, _iso
from .storage import jload

log = logging.getLogger('tasks')
bp = Blueprint('gorevler_api', __name__)
STATE = {}

TIMEZONES = ['Europe/Istanbul', 'UTC', 'Europe/London', 'Europe/Berlin', 'Europe/Paris', 'Europe/Moscow',
             'Asia/Dubai', 'Asia/Tokyo', 'America/New_York', 'America/Chicago', 'America/Los_Angeles']


def _ok(data=None, status=200):
    return jsonify({'ok': True, 'data': data}), status


def _err(code, message, status, field=None):
    e = {'code': code, 'message': message}
    if field:
        e['field'] = field
    return jsonify({'ok': False, 'error': e}), status


def _svc():
    if not STATE.get('mgr'):
        raise _Unavailable()
    return STATE


class _Unavailable(Exception):
    pass


def _body():
    if request.content_length and request.content_length > config.MAX_BODY_BYTES:
        raise _TooLarge()
    raw = request.get_data(cache=True)
    if not raw:
        return {}
    if len(raw) > config.MAX_BODY_BYTES:
        raise _TooLarge()
    data = request.get_json(silent=True)
    if data is None or not isinstance(data, dict):
        raise ValidationFailed('İstek gövdesi geçerli bir JSON nesnesi olmalı.')
    return data


class _TooLarge(Exception):
    pass


@bp.before_request
def _guard():
    if request.method in ('POST', 'PATCH', 'PUT', 'DELETE'):
        if request.headers.get('Sec-Fetch-Site', '').lower() == 'cross-site':
            return _err('yasak', 'Farklı kaynaktan gelen istek reddedildi.', 403)
        origin = request.headers.get('Origin')
        if origin and origin != 'null':
            try:
                if urlparse(origin).netloc.lower() != request.host.lower():
                    return _err('yasak', 'Farklı kaynaktan gelen istek reddedildi.', 403)
            except ValueError:
                return _err('yasak', 'Geçersiz Origin başlığı.', 403)
        elif origin == 'null':
            return _err('yasak', 'Farklı kaynaktan gelen istek reddedildi.', 403)
        if request.get_data(cache=True) and not request.is_json:
            return _err('desteklenmeyen_tur', 'Content-Type application/json olmalı.', 415)
    return None


@bp.errorhandler(TaskError)
def _h_task(e):
    return _err(e.code, e.message, e.http, e.field)


@bp.errorhandler(_Unavailable)
def _h_unavail(e):
    return _err('hizmet_yok', 'Görev sistemi şu anda kullanılamıyor.', 503)


@bp.errorhandler(_TooLarge)
def _h_large(e):
    return _err('cok_buyuk', 'İstek çok büyük.', 413)


@bp.errorhandler(Exception)
def _h_any(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return _err('http_hatasi', e.description or 'İstek işlenemedi.', e.code or 400)
    log.error('API hatasi (%s): %s: %s', request.path, type(e).__name__, util.redact(e))
    return _err('sunucu_hatasi', 'Beklenmeyen bir hata oluştu. Ayrıntılar günlüğe yazıldı.', 500)


# ------------------------------------------------------------------ gorevler
@bp.route('/tasks', methods=['GET'])
def list_tasks():
    s = _svc()
    return _ok(s['mgr'].list(request.args.get('filter', 'all')))


@bp.route('/tasks', methods=['POST'])
def create_task():
    s = _svc()
    t = s['mgr'].create(_body())
    s['runtime'].wake()
    return _ok(t, 201)


@bp.route('/tasks/<int:task_id>', methods=['GET'])
def get_task(task_id):
    return _ok(_svc()['mgr'].get(task_id))


@bp.route('/tasks/<int:task_id>', methods=['PATCH'])
def patch_task(task_id):
    s = _svc()
    t = s['mgr'].update(task_id, _body())
    s['runtime'].wake()
    return _ok(t)


@bp.route('/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id):
    return _ok(_svc()['mgr'].delete(task_id))


def _action(name):
    def view(task_id):
        s = _svc()
        t = getattr(s['mgr'], name)(task_id)
        s['runtime'].wake()
        return _ok(t)
    view.__name__ = f'action_{name}'
    return view


for _n, _m in (('run', 'run_now'), ('pause', 'pause'), ('resume', 'resume'), ('retry', 'retry'),
               ('cancel', 'cancel'), ('approve', 'approve'), ('reject', 'reject')):
    bp.add_url_rule(f'/tasks/<int:task_id>/{_n}', f'action_{_n}', _action(_m), methods=['POST'])


# ------------------------------------------------------------------ gecmis ve adimlar
def _public_step(st):
    out = st.get('output')
    preview = None
    if st['kind'] == 'search' and out:
        d = jload(out, {}) or {}
        preview = f"{len(d.get('sources') or [])} kaynak bulundu" + (' (önbellekten)' if d.get('cached') else '')
        if d.get('flagged'):
            preview += f", {d['flagged']} şüpheli satır filtrelendi"
    elif st['kind'] == 'plan' and out:
        preview = 'Plan oluşturuldu'
    elif out:
        preview = util.truncate(out, 1200)
    return {'step_no': st['step_no'], 'kind': st['kind'], 'name': st['name'], 'status': st['status'],
            'error': st.get('error'), 'attempts': st.get('attempts'), 'output_preview': preview,
            'started_at': _iso(st.get('started_at')), 'finished_at': _iso(st.get('finished_at'))}


def _public_run(r):
    return {'id': r['id'], 'run_no': r['run_no'], 'trigger': r['trigger'], 'status': r['status'],
            'attempt': r['attempt'], 'started_at': _iso(r['started_at']), 'finished_at': _iso(r.get('finished_at')),
            'error': r.get('error'), 'model_used': r.get('model_used'),
            'late_seconds': r.get('late_seconds'),
            'result_preview': r.get('result_preview'), 'has_result': bool(r.get('has_result'))}


@bp.route('/tasks/<int:task_id>/history', methods=['GET'])
def history(task_id):
    s = _svc()
    s['mgr'].get_row(task_id)
    try:
        limit = max(1, min(100, int(request.args.get('limit', 20))))
    except ValueError:
        raise ValidationFailed('limit sayı olmalı.', 'limit')
    return _ok([_public_run(r) for r in s['runs'].list_runs(task_id, limit)])


@bp.route('/tasks/<int:task_id>/history/<int:run_id>', methods=['GET'])
def history_item(task_id, run_id):
    s = _svc()
    s['mgr'].get_row(task_id)
    run = s['runs'].run_with_steps(task_id, run_id)
    if not run:
        raise NotFound('Çalıştırma kaydı bulunamadı.')
    out = _public_run(run)
    out['result'] = run.get('result')
    out['sources'] = run.get('sources') or []
    out['steps'] = [_public_step(x) for x in run['steps']]
    return _ok(out)


@bp.route('/tasks/<int:task_id>/steps', methods=['GET'])
def steps(task_id):
    s = _svc()
    s['mgr'].get_row(task_id)
    rid = request.args.get('run_id')
    try:
        rid = int(rid) if rid else s['runs'].latest_run_id(task_id)
    except ValueError:
        raise ValidationFailed('run_id sayı olmalı.', 'run_id')
    if not rid:
        return _ok({'run_id': None, 'steps': []})
    run = s['runs'].run_with_steps(task_id, rid)
    if not run:
        raise NotFound('Çalıştırma kaydı bulunamadı.')
    return _ok({'run_id': rid, 'run_status': run['status'], 'steps': [_public_step(x) for x in run['steps']]})


# ------------------------------------------------------------------ bildirimler
@bp.route('/tasks/notifications', methods=['GET'])
def notifications():
    s = _svc()
    try:
        limit = max(1, min(200, int(request.args.get('limit', 50))))
    except ValueError:
        raise ValidationFailed('limit sayı olmalı.', 'limit')
    items = s['notifier'].list(limit, unread_only=request.args.get('unread') == '1')
    return _ok({'items': items, 'unread': s['notifier'].unread_count()})


@bp.route('/tasks/notifications/<int:nid>/read', methods=['POST'])
def notif_read(nid):
    s = _svc()
    if not s['notifier'].mark_read(nid):
        raise NotFound('Bildirim bulunamadı.')
    return _ok({'unread': s['notifier'].unread_count()})


@bp.route('/tasks/notifications/read-all', methods=['POST'])
def notif_read_all():
    s = _svc()
    n = s['notifier'].mark_all_read()
    return _ok({'marked': n, 'unread': 0})


@bp.route('/tasks/preferences', methods=['GET', 'POST'])
def preferences():
    s = _svc()
    if request.method == 'POST':
        b = _body()
        if not isinstance(b.get('notifications_enabled'), bool):
            raise ValidationFailed('notifications_enabled doğru/yanlış olmalı.', 'notifications_enabled')
        s['notifier'].set_prefs(b['notifications_enabled'])
    return _ok({'notifications_enabled': s['notifier'].prefs()['enabled']})


# ------------------------------------------------------------------ istatistik ve bilgi
@bp.route('/tasks/stats', methods=['GET'])
def stats():
    s = _svc()
    st = s['mgr'].stats()
    st['unread_notifications'] = s['notifier'].unread_count()
    st['scheduler_running'] = bool(s['runtime'].is_running())
    return _ok(st)


@bp.route('/tasks/meta', methods=['GET'])
def meta():
    s = _svc()
    return _ok({
        'models': s['bridge'].models(),
        'timezones': TIMEZONES,
        'default_timezone': config.DEFAULT_TIMEZONE,
        'limits': {'title': config.MAX_TITLE_CHARS, 'description': config.MAX_DESCRIPTION_CHARS,
                   'min_interval_minutes': config.MIN_INTERVAL_MINUTES,
                   'min_check_interval_minutes': config.MIN_CHECK_INTERVAL_MINUTES,
                   'max_tasks': config.MAX_TASKS},
    })
