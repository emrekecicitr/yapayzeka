"""Eylem araclari: her arac bir ActionSpec kaydidir. YENI ARAC EKLEMEK = bu dosyaya (ya da bu kayit defterine kaydolan baska bir
dosyaya) tek bir ActionSpec tanimi eklemek; baska hicbir yere dokunmak gerekmez.

ActionSpec sozlesmesi
- validate(args, env) -> normallestirilmis args. Idempotent olmali: validate(validate(x)) == validate(x). Hata: ActionError.
  Calistirma aninda YENIDEN cagrilir (TOCTOU) ve sonucun args_hash ile ayni cikmasi gerekir.
- preview(args, env) -> insan okunur metin, TAM parametreleri gosterir (onay ekrani bunu gosterir).
- execute(args, env) -> kisa sonuc metni. YALNIZCA sistem kodundan, onayli kayit uzerinden cagrilir.
- verify(args, env) -> True (etki var ve ayni) | False/None (dogrulanamadi). Yalnizca idempotent araclar icin; cokme sonrasi kullanilir.
- risk: 'low' | 'medium' | 'high'.  Burada AG CAGRISI YAPAN arac yoktur ve eklenmemelidir.
"""
import datetime as dt
import email.message
import email.policy
import hashlib
import json
import re
import unicodedata

from . import config, fsafe
from .fsafe import ActionError


class ActionEnv:
    """Araclara verilen ortam: saat ve calisma klasoru."""

    def __init__(self, clock, workspace=None):
        self.clock = clock
        self.workspace = workspace or config.ACTIONS_WORKSPACE

    def now(self):
        return float(self.clock())

    def root(self, create=False):
        return fsafe.workspace_root(self.workspace, create=create)

    @property
    def display_root(self):
        return self.workspace


class ActionSpec:
    def __init__(self, name, doc, args_doc, validate, preview, execute, risk='low', idempotent=False, verify=None,
                 risk_fn=None):
        self.name = name
        self.doc = doc
        self.args_doc = args_doc
        self.validate = validate
        self.preview = preview
        self.execute = execute
        self.risk = risk
        self.risk_fn = risk_fn            # args -> risk (arguman bagimli risk), yoksa sabit risk
        self.idempotent = bool(idempotent)
        self.verify = verify

    def risk_of(self, args):
        return self.risk_fn(args) if self.risk_fn else self.risk


REGISTRY = {}


def register(spec):
    if spec.name in REGISTRY:
        raise ValueError('arac zaten kayitli: ' + spec.name)
    REGISTRY[spec.name] = spec
    return spec


def get(name):
    return REGISTRY.get(name)


def names():
    return sorted(REGISTRY)


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def args_hash_of(args):
    return hashlib.sha256(canonical_json(args).encode('utf-8')).hexdigest()


# ======================================================================
# ortak yardimcilar
# ======================================================================
_TR = str.maketrans({'ı': 'i', 'İ': 'i', 'ş': 's', 'Ş': 's', 'ğ': 'g', 'Ğ': 'g', 'ü': 'u', 'Ü': 'u', 'ö': 'o', 'Ö': 'o',
                     'ç': 'c', 'Ç': 'c'})


def slug(text, default):
    t = unicodedata.normalize('NFKD', str(text or '').translate(_TR)).encode('ascii', 'ignore').decode('ascii').lower()
    t = re.sub(r'[^a-z0-9]+', '-', t).strip('-')[:30].strip('-')
    return t or default


def _clean_line(v, max_chars, field, required=True):
    """Tek satirlik metin: CR/LF/NUL/denetim/satir ayiricilari bosluga cevrilir (baslik enjeksiyonu), bos olamaz."""
    if v is None and not required:
        return ''
    if not isinstance(v, str):
        raise ActionError(f'{field} metin olmalı.')
    out = []
    for ch in v:
        cat = unicodedata.category(ch)
        if ch in '\r\n\t\x0b\x0c\x85' or cat in ('Zl', 'Zp'):
            out.append(' ')
        elif cat == 'Cc' or cat == 'Cf' or cat == 'Cs':
            continue
        else:
            out.append(ch)
    s = re.sub(r' +', ' ', ''.join(out)).strip()
    if required and not s:
        raise ActionError(f'{field} boş olamaz.')
    if len(s) > max_chars:
        raise ActionError(f'{field} en fazla {max_chars} karakter olabilir.')
    return s


def _body_text(v, max_chars, field, required=False):
    """Cok satirli metin: NUL ve diger denetim karakterleri atilir, satir sonlari \\n'e normallestirilir."""
    if v is None and not required:
        return ''
    if not isinstance(v, str):
        raise ActionError(f'{field} metin olmalı.')
    if '\x00' in v:
        raise ActionError(f'{field} NUL karakteri içeremez.')
    s = v.replace('\r\n', '\n').replace('\r', '\n').replace('\u2028', '\n').replace('\u2029', '\n').replace('\x85', '\n')
    s = ''.join(ch for ch in s if ch in '\n\t' or unicodedata.category(ch) not in ('Cc', 'Cs'))
    try:
        s.encode('utf-8')
    except UnicodeEncodeError:
        raise ActionError(f'{field} geçersiz karakter içeriyor.')
    if required and not s.strip():
        raise ActionError(f'{field} boş olamaz.')
    if len(s) > max_chars:
        raise ActionError(f'{field} en fazla {max_chars} karakter olabilir.')
    return s


def _only_keys(args, allowed):
    if not isinstance(args, dict):
        raise ActionError('args bir nesne olmalı.')
    extra = [k for k in args if k not in allowed]
    if extra:
        raise ActionError('Bilinmeyen alan: ' + ', '.join(sorted(str(k)[:30] for k in extra)[:3]) + '. İzinli alanlar: ' + ', '.join(allowed) + '.')


# ======================================================================
# file_write
# ======================================================================
def _fw_validate(args, env):
    _only_keys(args, ('path', 'content', 'overwrite'))
    parts = fsafe.split_relpath(args.get('path'), config.ACTIONS_FILE_EXTS)
    content = _body_text(args.get('content'), config.ACTIONS_FILE_MAX_BYTES, 'content', required=False)
    if len(content.encode('utf-8')) > config.ACTIONS_FILE_MAX_BYTES:
        raise ActionError(f'İçerik en fazla {config.ACTIONS_FILE_MAX_BYTES} bayt olabilir.')
    ow = args.get('overwrite', False)
    if not isinstance(ow, bool):
        raise ActionError('overwrite true ya da false olmalı.')
    return {'path': '/'.join(parts), 'content': content, 'overwrite': ow}


def _fw_preview(args, env):
    n = len(args['content'].encode('utf-8'))
    mode = ('MEVCUT DOSYANIN ÜZERİNE YAZILIR (varsa eski içerik kaybolur)' if args['overwrite']
            else 'Hedef dosya zaten varsa işlem hata verir, üzerine yazılmaz')
    return (f"Dosya yazılacak: {env.display_root}/{args['path']}\nBoyut: {n} bayt. {mode}.\n"
            f"--- içerik başı ---\n{args['content']}\n--- içerik sonu ---")


def _fw_execute(args, env):
    root = env.root(create=True)
    parts = args['path'].split('/')
    n = fsafe.write_file(root, parts, args['content'].encode('utf-8'), overwrite=args['overwrite'])
    return f"Dosya yazıldı: {args['path']} ({n} bayt)"


def _fw_verify(args, env):
    try:
        root = env.root(create=False)
        h = fsafe.file_hash(root, args['path'].split('/'))
    except ActionError:
        return None
    return h is not None and h == fsafe.bytes_hash(args['content'].encode('utf-8'))


register(ActionSpec(
    'file_write',
    'Çalışma klasörü içine metin dosyası yazar (varsayılan: mevcut dosyanın üzerine YAZMAZ).',
    '{"path": "alt/klasor/not.md", "content": "metin", "overwrite": false}  path göreli olmalı; uzantı: '
    + ', '.join(config.ACTIONS_FILE_EXTS) + '.',
    _fw_validate, _fw_preview, _fw_execute, risk='low', idempotent=True, verify=_fw_verify,
    risk_fn=lambda a: 'medium' if a.get('overwrite') else 'low'))


# ======================================================================
# email_draft (GONDERMEZ)
# ======================================================================
_ADDR_RE = re.compile(r'^[^\s<>@,;:"()\[\]\\]{1,64}@[^\s<>@,;:"()\[\]\\]{1,190}\.[^\s<>@,;:"()\[\]\\]{2,}$')


def _addr_list(v, field, required):
    if v is None or v == '' or v == []:
        if required:
            raise ActionError(f'{field} gerekli.')
        return []
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or len(items) > 10:
        raise ActionError(f'{field} en fazla 10 adres içeren bir liste (ya da tek adres) olmalı.')
    out = []
    for a in items:
        if not isinstance(a, str):
            raise ActionError(f'{field} adresleri metin olmalı.')
        s = a.strip()
        if any(unicodedata.category(c)[0] == 'C' or c in ' \u2028\u2029' for c in s) or len(s) > 254 or not _ADDR_RE.match(s):
            raise ActionError(f'{field}: geçersiz e-posta adresi.')
        if s.lower() not in [x.lower() for x in out]:
            out.append(s)
    if required and not out:
        raise ActionError(f'{field} gerekli.')
    return out


def _ed_validate(args, env):
    _only_keys(args, ('to', 'cc', 'subject', 'body', 'format', 'filename'))
    to = _addr_list(args.get('to'), 'to', True)
    cc = _addr_list(args.get('cc'), 'cc', False)
    subject = _clean_line(args.get('subject'), 200, 'subject')
    body = _body_text(args.get('body'), config.ACTIONS_FILE_MAX_BYTES // 2, 'body', required=True)
    fmt = args.get('format', 'eml')
    if fmt not in ('eml', 'txt'):
        raise ActionError('format "eml" ya da "txt" olmalı.')
    core = {'to': to, 'cc': cc, 'subject': subject, 'body': body, 'format': fmt}
    h8 = args_hash_of(core)[:8]
    core['filename'] = f"taslaklar/{slug(subject, 'taslak')}-{h8}.{fmt}"      # her zaman yeniden uretilir (disaridan gelen yok sayilir)
    try:
        _ed_render(core)
    except Exception:  # noqa: BLE001 - uretilemeyen taslak oneri asamasinda reddedilir
        raise ActionError('E-posta taslağı bu içerikle oluşturulamadı (adres ya da konu geçersiz karakter içeriyor).')
    return core


def _ed_render(args):
    if args['format'] == 'txt':
        text = ('TASLAK (gönderilmedi)\nKime: ' + ', '.join(args['to']) + '\n'
                + ('Bilgi: ' + ', '.join(args['cc']) + '\n' if args['cc'] else '')
                + 'Konu: ' + args['subject'] + '\n\n' + args['body'] + '\n')
        return text.encode('utf-8')
    m = email.message.EmailMessage(policy=email.policy.SMTP)
    m['To'] = ', '.join(args['to'])
    if args['cc']:
        m['Cc'] = ', '.join(args['cc'])
    m['Subject'] = args['subject']
    m['X-Draft'] = 'not-sent'
    m.set_content(args['body'], charset='utf-8')
    return m.as_bytes()


def _ed_preview(args, env):
    return (f"E-posta TASLAĞI yazılacak (GÖNDERİLMEZ): {env.display_root}/{args['filename']}\n"
            f"Kime: {', '.join(args['to'])}\n" + (f"Bilgi: {', '.join(args['cc'])}\n" if args['cc'] else '')
            + f"Konu: {args['subject']}\n--- gövde ---\n{args['body']}\n--- gövde sonu ---")


def _ed_execute(args, env):
    root = env.root(create=True)
    n = fsafe.write_file(root, args['filename'].split('/'), _ed_render(args), overwrite=False)
    return f"Taslak dosyası yazıldı (gönderilmedi): {args['filename']} ({n} bayt)"


def _ed_verify(args, env):
    try:
        h = fsafe.file_hash(env.root(create=False), args['filename'].split('/'))
    except ActionError:
        return None
    return h is not None and h == fsafe.bytes_hash(_ed_render(args))


register(ActionSpec(
    'email_draft',
    'E-posta TASLAĞI dosyası yazar. E-posta GÖNDERMEZ.',
    '{"to": ["a@b.com"], "cc": [], "subject": "konu", "body": "metin", "format": "eml|txt"}',
    _ed_validate, _ed_preview, _ed_execute, risk='low', idempotent=True, verify=_ed_verify))


# ======================================================================
# calendar_proposal (.ics)
# ======================================================================
_DT_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$')
_D_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _zone(name):
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name)
    except Exception:  # noqa: BLE001 - tzdata yok / gecersiz ad
        raise ActionError('Geçersiz saat dilimi (örn. Europe/Istanbul) ya da sistemde saat dilimi verisi yok.')


def _to_utc(local_str, zone):
    try:
        naive = dt.datetime.strptime(local_str, '%Y-%m-%dT%H:%M')
    except ValueError:
        raise ActionError('Tarih/saat YYYY-MM-DDTHH:MM biçiminde olmalı.')
    a = naive.replace(tzinfo=zone, fold=0)
    b = naive.replace(tzinfo=zone, fold=1)
    if a.utcoffset() != b.utcoffset():
        raise ActionError('Bu yerel saat saat dilimi geçişinde iki kez yaşanıyor (belirsiz); farklı bir saat seçin.')
    utc = a.astimezone(dt.timezone.utc)
    if utc.astimezone(zone).replace(tzinfo=None) != naive:
        raise ActionError('Bu yerel saat saat dilimi geçişinde hiç yaşanmıyor (geçersiz); farklı bir saat seçin.')
    return utc


def _ics_text(s):
    s = str(s).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')
    return s


def _fold(line):
    """RFC 5545: 75 oktetten uzun satirlar CRLF+bosluk ile katlanir; UTF-8 karakterler bolunmez."""
    raw = line.encode('utf-8')
    if len(raw) <= 75:
        return [line]
    out, cur, size, first = [], '', 0, True
    for ch in line:
        n = len(ch.encode('utf-8'))
        limit = 75 if first else 74
        if size + n > limit:
            out.append(cur if first else ' ' + cur)
            cur, size, first = '', 0, False
        cur += ch
        size += n
    out.append(cur if first else ' ' + cur)
    return out


def _cal_validate(args, env):
    _only_keys(args, ('title', 'start', 'end', 'timezone', 'location', 'description', 'all_day', 'stamp', 'filename'))
    title = _clean_line(args.get('title'), 120, 'title')
    all_day = args.get('all_day', False)
    if not isinstance(all_day, bool):
        raise ActionError('all_day true ya da false olmalı.')
    tz = args.get('timezone') or config.DEFAULT_TIMEZONE
    if not isinstance(tz, str) or len(tz) > 60 or not re.match(r'^[A-Za-z0-9_+\-/]+$', tz):
        raise ActionError('Geçersiz saat dilimi adı.')
    zone = _zone(tz)
    start, end = args.get('start'), args.get('end')
    if all_day:
        if not isinstance(start, str) or not _D_RE.match(start):
            raise ActionError('Tüm gün etkinliğinde start YYYY-MM-DD biçiminde olmalı.')
        try:
            d0 = dt.date.fromisoformat(start)
            d1 = dt.date.fromisoformat(end) if end else d0
        except (ValueError, TypeError):
            raise ActionError('Geçersiz tarih.')
        if d1 < d0:
            raise ActionError('Bitiş başlangıçtan önce olamaz.')
        if (d1 - d0).days > 60:
            raise ActionError('Etkinlik en fazla 60 gün sürebilir.')
        start, end = d0.isoformat(), d1.isoformat()
    else:
        if not isinstance(start, str) or not _DT_RE.match(start):
            raise ActionError('start YYYY-MM-DDTHH:MM biçiminde olmalı.')
        s_utc = _to_utc(start, zone)
        if end:
            if not isinstance(end, str) or not _DT_RE.match(end):
                raise ActionError('end YYYY-MM-DDTHH:MM biçiminde olmalı.')
            e_utc = _to_utc(end, zone)
        else:
            e_utc = s_utc + dt.timedelta(hours=1)
            end = e_utc.astimezone(zone).strftime('%Y-%m-%dT%H:%M')
        if e_utc <= s_utc:
            raise ActionError('Bitiş, başlangıçtan sonra olmalı.')
        if e_utc - s_utc > dt.timedelta(days=14):
            raise ActionError('Etkinlik en fazla 14 gün sürebilir.')
    stamp = args.get('stamp')
    if not (isinstance(stamp, str) and re.match(r'^\d{8}T\d{6}Z$', stamp)):
        stamp = dt.datetime.fromtimestamp(env.now(), dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    core = {'title': title, 'start': start, 'end': end, 'timezone': tz, 'all_day': all_day, 'stamp': stamp,
            'location': _clean_line(args.get('location'), 200, 'location', required=False),
            'description': _body_text(args.get('description'), 2000, 'description')}
    h = args_hash_of({k: v for k, v in core.items() if k != 'stamp'})
    core['filename'] = f"takvim/{slug(title, 'etkinlik')}-{h[:8]}.ics"
    core['_uid'] = h[:32] + '@gorevler.local'
    try:
        _cal_ics(core)
    except ActionError:
        raise
    except Exception:  # noqa: BLE001
        raise ActionError('Takvim dosyası bu içerikle oluşturulamadı.')
    return core


def _cal_ics(a):
    zone = _zone(a['timezone'])
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//ai-gorevler//eylem//TR', 'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
             'BEGIN:VEVENT', 'UID:' + a['_uid'], 'DTSTAMP:' + a['stamp']]
    if a['all_day']:
        d0 = dt.date.fromisoformat(a['start'])
        d1 = dt.date.fromisoformat(a['end']) + dt.timedelta(days=1)        # DTEND tum gunde dislayicidir
        lines += ['DTSTART;VALUE=DATE:' + d0.strftime('%Y%m%d'), 'DTEND;VALUE=DATE:' + d1.strftime('%Y%m%d')]
    else:
        fmt = '%Y%m%dT%H%M%SZ'
        lines += ['DTSTART:' + _to_utc(a['start'], zone).strftime(fmt), 'DTEND:' + _to_utc(a['end'], zone).strftime(fmt)]
    lines.append('SUMMARY:' + _ics_text(a['title']))
    if a['location']:
        lines.append('LOCATION:' + _ics_text(a['location']))
    if a['description']:
        lines.append('DESCRIPTION:' + _ics_text(a['description']))
    lines += ['END:VEVENT', 'END:VCALENDAR']
    out = []
    for ln in lines:
        out.extend(_fold(ln))
    return ('\r\n'.join(out) + '\r\n').encode('utf-8')


def _cal_validate_keep_internal(args, env):
    """Depolanan args '_uid' icerir; yeniden dogrulamada gelen '_uid' yok sayilir (yeniden uretilir)."""
    a = dict(args) if isinstance(args, dict) else args
    if isinstance(a, dict):
        a.pop('_uid', None)
    return _cal_validate(a, env)


def _cal_preview(a, env):
    when = (f"{a['start']} (tüm gün" + (f", {a['end']} dahil" if a['end'] != a['start'] else '') + ')') if a['all_day'] \
        else f"{a['start']} ile {a['end']} arası, saat dilimi {a['timezone']} (dosyada UTC olarak yazılır)"
    return (f"Takvim dosyası (.ics) yazılacak: {env.display_root}/{a['filename']}\nBaşlık: {a['title']}\nZaman: {when}\n"
            + (f"Yer: {a['location']}\n" if a['location'] else '') + (f"Açıklama: {a['description']}\n" if a['description'] else '')
            + 'Takvime EKLENMEZ; dosyayı siz açıp içe aktarırsınız.')


def _cal_execute(a, env):
    root = env.root(create=True)
    n = fsafe.write_file(root, a['filename'].split('/'), _cal_ics(a), overwrite=False)
    return f"Takvim dosyası yazıldı: {a['filename']} ({n} bayt)"


def _cal_verify(a, env):
    try:
        h = fsafe.file_hash(env.root(create=False), a['filename'].split('/'))
    except ActionError:
        return None
    return h is not None and h == fsafe.bytes_hash(_cal_ics(a))


register(ActionSpec(
    'calendar_proposal',
    'Takvim etkinliği için .ics dosyası yazar (takvime kendisi eklemez).',
    '{"title": "Toplantı", "start": "2026-12-01T09:00", "end": "2026-12-01T10:00", "timezone": "Europe/Istanbul", '
    '"location": "", "description": "", "all_day": false}',
    _cal_validate_keep_internal, _cal_preview, _cal_execute, risk='low', idempotent=True, verify=_cal_verify))
