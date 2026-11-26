"""Dagitim ortami uyarilari (bilgilendirme; ASLA engellemez, ASLA istisna firlatmaz).

- Render: kalici disk yoksa dosya sistemi her dagitimda sifirlanir; eylem klasoru ve veritabani (onay/denetim kayitlari)
  kaybolur. Sezgi: yolun bulundugu dosya sistemi '/' ile ayniysa ya da /tmp, /dev/shm altindaysa kalici degildir.
- Termux: paylasilan depolama (/sdcard, ~/storage/shared) sert baglanti (link) ve sembolik bag desteklemez; guvenli yazma orada
  calismaz. Eylem klasoru uygulamanin kendi ev dizininde olmali.
- Render'da onay belirteci yoksa onaylar reddedilir (action_auth.py); bu da acikca belirtilir.
- dir_fd/O_NOFOLLOW olmayan platform (Windows): guvenli yazma yoktur, eylemler yazmaz.
"""
import os

from . import config, fsafe
from . import action_auth as auth


def on_render():
    return bool(os.environ.get('RENDER') or os.environ.get('RENDER_SERVICE_ID'))


def on_termux():
    return 'com.termux' in (os.environ.get('PREFIX') or '') or 'com.termux' in (os.environ.get('HOME') or '')


def _anchor(path):
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def _dev(path):
    return os.stat(_anchor(path)).st_dev


def _in(path, prefixes):
    p = os.path.abspath(path)
    return any(p == q or p.startswith(q.rstrip('/') + '/') for q in prefixes)


def warnings(db_path):
    out = []
    try:
        ws = os.path.abspath(os.path.expanduser(config.ACTIONS_WORKSPACE))
        if not fsafe.secure_write_supported():
            out.append('Bu platformda güvenli dosya yazma (dir_fd ve O_NOFOLLOW) desteklenmiyor: dosya eylemleri yazmaz, '
                       'başarısız olur. Linux, macOS ya da Termux kullanın.')
        if on_render():
            try:
                root_dev = os.stat('/').st_dev
                for label, p, env in (('Eylem çalışma klasörü', ws, 'TASKS_WORKSPACE'),
                                      ('Görev veritabanı (onay ve denetim kayıtları dahil)', db_path, 'TASKS_DB_PATH')):
                    if _in(p, ('/tmp', '/dev/shm', '/var/tmp')) or _dev(p) == root_dev:
                        out.append(f'{label} kalıcı bir diskte görünmüyor; Render her dağıtımda sıfırlar ve veriler kaybolur. '
                                   f'Kalıcı disk bağlayıp yolu ({env}) o diske taşıyın.')
            except OSError:
                pass
            if auth.token_state() != 'ok':
                out.append('Render ortamında onay belirteci yok ya da çok kısa: eylem API\'si tüm istekleri reddedecek. '
                           f'TASKS_ACTIONS_APPROVAL_TOKEN ayarlayın (en az {auth.MIN_TOKEN_CHARS} karakter, rastgele).')
        if (on_termux() and _in(ws, ('/sdcard', '/storage'))) or '/storage/shared' in ws:
            out.append('Eylem çalışma klasörü Android paylaşımlı depolamada görünüyor; orada sert bağlantı desteklenmez ve '
                       'güvenli yazma başarısız olur. Klasörü Termux ev dizininde (~/ai_workspace) tutun.')
        if auth.token_state() == 'short':
            out.append(f'TASKS_ACTIONS_APPROVAL_TOKEN {auth.MIN_TOKEN_CHARS} karakterden kısa; yok sayılıyor.')
    except Exception:  # noqa: BLE001 - uyari uretimi hicbir zaman sistemi bozmaz
        pass
    return out
