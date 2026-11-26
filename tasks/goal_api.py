"""Hedef (orkestrasyon) REST API'si: ayri Flask Blueprint. Mevcut yollari DEGISTIRMEZ; yalnizca /tasks/goals altina ekler.

  GET  /tasks/goals/meta                  sinirlar, durum, uyarilar
  GET  /tasks/goals[?status=]             liste (ozet)
  POST /tasks/goals                       {description, title?, internet?, agent?, project_id?}  -> planlama baslar
  GET  /tasks/goals/<id>                  ayrinti: plan, plan_hash, dugumler, sonuclar, olaylar
  POST /tasks/goals/<id>/approve          {plan_hash}   plan onayi (onaysiz hicbir gorev olusmaz)
  POST /tasks/goals/<id>/cancel
  POST /tasks/goals/<id>/retry            basarisiz hedefi basarisiz/atlanan dugumlerden surdurur
  DELETE /tasks/goals/<id>                yalnizca sonlanmis hedef

ERISIM: eylem API'siyle AYNI kural (action_auth.py): HER yol yerel istek ya da onay belirteci ister; muaf yol yok.
Yanit bicimi, CSRF ve hata zarfi api.py ile aynidir.
"""
from flask import Blueprint, g, request

from . import action_api, api
from .manager import TaskError, ValidationFailed

bp = Blueprint('gorevler_goals_api', __name__)
bp.before_request(api._guard)
bp.before_request(action_api._auth_guard)
bp.errorhandler(TaskError)(api._h_task)
bp.errorhandler(api._Unavailable)(api._h_unavail)
bp.errorhandler(api._TooLarge)(api._h_large)
bp.errorhandler(Exception)(api._h_any)


def _goals():
    s = api.STATE.get('goals')
    if s is None:
        raise api._Unavailable()
    return s


def _limit():
    v = request.args.get('limit')
    if v in (None, ''):
        return 50
    try:
        n = int(v)
    except ValueError:
        raise ValidationFailed('Geçersiz sayı.', 'limit')
    if not 1 <= n <= 200:
        raise ValidationFailed('Sayı izin verilen aralıkta değil.', 'limit')
    return n


@bp.route('/tasks/goals/meta', methods=['GET'])
def meta():
    return api._ok(_goals().meta())


@bp.route('/tasks/goals', methods=['GET'])
def list_goals():
    return api._ok(_goals().list(status=request.args.get('status') or None, limit=_limit()))


@bp.route('/tasks/goals', methods=['POST'])
def create_goal():
    b = api._body()
    return api._ok(_goals().create(b.get('description'), title=b.get('title'), internet=b.get('internet', False),
                                   agent=b.get('agent', True), project_id=b.get('project_id')), 201)


@bp.route('/tasks/goals/<int:gid>', methods=['GET'])
def get_goal(gid):
    return api._ok(_goals().get(gid))


@bp.route('/tasks/goals/<int:gid>/approve', methods=['POST'])
def approve_goal(gid):
    b = api._body()
    return api._ok(_goals().approve(gid, b.get('plan_hash'), via=g.get('actions_via')))


@bp.route('/tasks/goals/<int:gid>/cancel', methods=['POST'])
def cancel_goal(gid):
    return api._ok(_goals().cancel(gid))


@bp.route('/tasks/goals/<int:gid>/retry', methods=['POST'])
def retry_goal(gid):
    return api._ok(_goals().retry(gid))


@bp.route('/tasks/goals/<int:gid>', methods=['DELETE'])
def delete_goal(gid):
    return api._ok(_goals().delete(gid))
