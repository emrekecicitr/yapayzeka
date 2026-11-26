"""Hafiza REST API (ayri Flask Blueprint). Mevcut /tasks yollarini DEGISTIRMEZ; yalnizca yeni yollar ekler:

  /tasks/memory/stats | reindex | projects[/<id>] | items[/<id>] | search      (yeni)
  /tasks/<id>/project   (gorevi projeye bagla/ayir)     /tasks/<id>/remember  (son sonucu hafizaya yaz)

Guvenlik ve yanit bicimi api.py ile AYNIDIR (ayni-kaynak/CSRF korumasi, JSON zorunlulugu, {"ok":..} zarfi, ic hata ayrintisi yok).
"""
from flask import Blueprint, request

from . import api, config
from .manager import TaskError, ValidationFailed

bp = Blueprint('gorevler_memory_api', __name__)
bp.before_request(api._guard)
bp.errorhandler(TaskError)(api._h_task)
bp.errorhandler(api._Unavailable)(api._h_unavail)
bp.errorhandler(api._TooLarge)(api._h_large)
bp.errorhandler(Exception)(api._h_any)


def _mem():
    m = api.STATE.get('memory')
    if m is None:
        raise api._Unavailable()
    return m


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


def _flag(name):
    return request.args.get(name, '').lower() in ('1', 'true', 'evet', 'yes')


@bp.route('/tasks/memory/stats', methods=['GET'])
def stats():
    return api._ok(_mem().stats())


@bp.route('/tasks/memory/reindex', methods=['POST'])
def reindex():
    return api._ok({'indexed': _mem().rebuild_index()})


# ------------------------------------------------------------------ projeler
@bp.route('/tasks/memory/projects', methods=['GET'])
def list_projects():
    return api._ok(_mem().list_projects(include_archived=_flag('archived')))


@bp.route('/tasks/memory/projects', methods=['POST'])
def create_project():
    b = api._body()
    return api._ok(_mem().create_project(b.get('name'), b.get('description', ''), b.get('status', 'active')), 201)


@bp.route('/tasks/memory/projects/<int:pid>', methods=['GET'])
def get_project(pid):
    return api._ok(_mem().get_project(pid))


@bp.route('/tasks/memory/projects/<int:pid>', methods=['PATCH'])
def patch_project(pid):
    return api._ok(_mem().update_project(pid, api._body()))


@bp.route('/tasks/memory/projects/<int:pid>', methods=['DELETE'])
def delete_project(pid):
    return api._ok(_mem().delete_project(pid))


# ------------------------------------------------------------------ ogeler
@bp.route('/tasks/memory/items', methods=['GET'])
def list_items():
    return api._ok(_mem().list_items(
        project_id=_qint('project_id', lo=1), kind=request.args.get('kind') or None,
        status=request.args.get('status') or None, limit=_qint('limit', 50, 1, 200), offset=_qint('offset', 0, 0)))


@bp.route('/tasks/memory/items', methods=['POST'])
def create_item():
    b = api._body()
    return api._ok(_mem().add_item(
        b.get('kind'), b.get('title'), b.get('body', ''), project_id=b.get('project_id'), task_id=b.get('task_id'),
        tags=b.get('tags'), supersedes_id=b.get('supersedes_id')), 201)


@bp.route('/tasks/memory/items/<int:iid>', methods=['GET'])
def get_item(iid):
    return api._ok(_mem().get_item(iid))


@bp.route('/tasks/memory/items/<int:iid>', methods=['PATCH'])
def patch_item(iid):
    return api._ok(_mem().update_item(iid, api._body()))


@bp.route('/tasks/memory/items/<int:iid>', methods=['DELETE'])
def delete_item(iid):
    return api._ok(_mem().delete_item(iid))


@bp.route('/tasks/memory/search', methods=['GET'])
def search():
    hits = _mem().search(request.args.get('q', ''), project_id=_qint('project_id', lo=1),
                         kind=request.args.get('kind') or None, include_superseded=_flag('superseded'),
                         limit=_qint('limit', config.MEMORY_SEARCH_LIMIT, 1, 50))
    return api._ok(hits)


# ------------------------------------------------------------------ gorev baglari
@bp.route('/tasks/<int:task_id>/project', methods=['GET'])
def task_project(task_id):
    m = _mem()
    api._svc()['mgr'].get_row(task_id)                 # yoksa 404
    pid = m.project_of_task(task_id)
    return api._ok(m.get_project(pid) if pid is not None else None)


@bp.route('/tasks/<int:task_id>/project', methods=['PUT'])
def set_task_project(task_id):
    b = api._body()
    if 'project_id' not in b:
        raise ValidationFailed('project_id gerekli (ayırmak için null).', 'project_id')
    return api._ok(_mem().attach_task(task_id, b['project_id']))


@bp.route('/tasks/<int:task_id>/remember', methods=['POST'])
def remember_task(task_id):
    return api._ok(_mem().remember_task(task_id), 201)
