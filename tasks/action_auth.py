"""Eylem API erisim denetimi.

Sorun: uygulama internete acik (ornegin Render) calisirken URL'yi bilen herkes /tasks/actions yollarini cagirabilirdi.
Kural (varsayilan KAPALI-HATA): /tasks/actions altindaki HER yol (liste, ayrinti, denetim, meta, onay, ret, iptal) iki yoldan
biriyle erisilebilir:

1. Onay belirteci AYARLIYSA (TASKS_ACTIONS_APPROVAL_TOKEN, en az 24 karakter): istek `X-Approval-Token: <belirtec>` ya da
   `Authorization: Bearer <belirtec>` tasimak ZORUNDADIR (sabit zamanli karsilastirma). Yerel istekler de dahil.
2. Belirtec AYARLI DEGILSE: yalnizca YEREL istek kabul edilir, yani istemci adresi loopback olmali VE istek bir vekil
   sunucudan (X-Forwarded-For, Forwarded, X-Real-IP ...) gelmemis olmali. Termux'ta localhost kullanimi boylece sifir ayarla
   calisir; Render gibi vekil arkasindaki kurulumda belirtec olmadan HICBIR istek kabul edilmez.

Belirtec cok kisaysa (24 karakterden az) YOK sayilir (asla daha zayif bir koruma olusmaz, yalnizca yerel kural gecerli olur).
Belirtec her cagrida ortamdan okunur (kill switch gibi, yeniden baslatma gerekmez). Mesajlar belirteci veya ayarlari ifsa etmez.
"""
import hmac
import ipaddress
import logging

from flask import request

from . import config
from .manager import TaskError

log = logging.getLogger('tasks')

MIN_TOKEN_CHARS = 24
_FORWARD_HEADERS = ('X-Forwarded-For', 'Forwarded', 'X-Real-IP', 'X-Forwarded-Host', 'True-Client-IP', 'CF-Connecting-IP')


class ActionAuthError(TaskError):
    http = 403
    code = 'yetkisiz'


def token_state():
    """'none' (ayarsiz) | 'short' (ayarli ama cok kisa: yok sayilir) | 'ok'."""
    t = config.actions_approval_token()
    if not t:
        return 'none'
    return 'ok' if len(t) >= MIN_TOKEN_CHARS else 'short'


def _loopback(addr):
    try:
        ip = ipaddress.ip_address(str(addr).split('%', 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def is_local_request():
    """Dogrudan, vekilsiz, loopback adresinden gelen istek mi? (Belirsizse False: guvenli taraf.)"""
    if not request.remote_addr or not _loopback(request.remote_addr):
        return False
    return not any(h in request.headers for h in _FORWARD_HEADERS)


def _presented():
    out = []
    v = request.headers.get('X-Approval-Token')
    if v and v.strip():
        out.append(v.strip())
    a = request.headers.get('Authorization') or ''
    if a[:7].lower() == 'bearer ' and a[7:].strip():
        out.append(a[7:].strip())
    return out


def _who():
    return str(request.headers.get('X-Forwarded-For') or request.remote_addr)[:60]


def authorize():
    """Donus: 'token' | 'local' (denetim izine yazilir). Yetkisizse ActionAuthError."""
    if token_state() == 'ok':
        secret = config.actions_approval_token().encode('utf-8')
        for cand in _presented():
            if hmac.compare_digest(cand.encode('utf-8'), secret):
                return 'token'
        log.warning('eylem API: yetkisiz istek reddedildi (%s %s, uzak=%r)', request.method, request.path, _who())
        raise ActionAuthError('Yetkisiz: geçerli bir onay belirteci gerekli.')
    if is_local_request():
        return 'local'
    log.warning('eylem API: yerel olmayan istek belirteçsiz reddedildi (%s %s, uzak=%r)', request.method, request.path, _who())
    raise ActionAuthError('Yetkisiz: bu istek yerel değil. Uzaktan erişim için sunucuda TASKS_ACTIONS_APPROVAL_TOKEN '
                          '(en az %d karakter) ayarlayın ve X-Approval-Token başlığıyla gönderin.' % MIN_TOKEN_CHARS)
