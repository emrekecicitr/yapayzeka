"""Zamanlama matematigi (saf fonksiyonlar, veritabani ve thread'den bagimsiz; kolay test edilir).

Kurallar
- Kullanici zamani, secilen saat diliminin DUVAR saati olarak girer ("her gun 09:00" = o bolgenin 09:00'u).
- Hesaplama yerel tarihlerle yapilir, sonuc UTC epoch'a cevrilir; yaz/kis saati gecislerinde:
    * olmayan saat (ileri alma): bosluk kadar ileriye kayar (02:30 yoksa 03:30 olur),
    * iki kez yasanan saat (geri alma): ilk olusum kullanilir, ayni gun iki kez calismaz.
- Europe/Istanbul icin tz veritabani yoksa sabit UTC+3 kullanilir (Turkiye 2016'dan beri kalici UTC+3).
- Hesaplanan sonraki zaman daima "after" aninda KESIN buyuktur; boylece bir gorev ayni anda tekrar tetiklenmez.
"""
import calendar
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover
    ZoneInfo = None

    class ZoneInfoNotFoundError(Exception):
        pass

from . import config

_TZ_NAME_RE = re.compile(r'^[A-Za-z0-9_+\-/]{1,64}$')
_HM_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
_TR_DAYS = ['Pazartesi', 'Salı', 'Çarşamba', 'Perşembe', 'Cuma', 'Cumartesi', 'Pazar']
_FREQS = ('daily', 'weekdays', 'weekly', 'monthly', 'interval', 'cron')


class ScheduleError(ValueError):
    def __init__(self, message, field='schedule_config'):
        super().__init__(message)
        self.message = message
        self.field = field


# --------------------------------------------------------------------------
# saat dilimi
# --------------------------------------------------------------------------
def get_tz(name):
    name = (name or config.DEFAULT_TIMEZONE)
    if not isinstance(name, str) or not _TZ_NAME_RE.match(name):
        raise ScheduleError('Geçersiz saat dilimi.', 'timezone')
    try:
        if ZoneInfo is None:
            raise ZoneInfoNotFoundError(name)
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        if name == 'Europe/Istanbul':
            return timezone(timedelta(hours=3), 'Europe/Istanbul')
        if name == 'UTC':
            return timezone.utc
        raise ScheduleError('Geçersiz saat dilimi.', 'timezone')


def local_to_epoch(naive, tz):
    """Saat dilimsiz yerel zamani UTC epoch'a cevirir (DST kurallari modul aciklamasinda)."""
    aware = naive.replace(tzinfo=tz, fold=0)
    return aware.astimezone(timezone.utc).timestamp()


def epoch_to_local(epoch, tz):
    return datetime.fromtimestamp(epoch, tz)


def _parse_hm(value, field='time'):
    m = _HM_RE.match(str(value or '').strip())
    if not m:
        raise ScheduleError('Saat SS:DD biçiminde olmalı (örn. 09:30).', field)
    return int(m.group(1)), int(m.group(2))


def _hm_str(h, m):
    return f'{h:02d}:{m:02d}'


def parse_local_datetime(value, field='run_at'):
    s = str(value or '').strip().replace(' ', 'T')
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ScheduleError('Tarih ve saat geçerli bir biçimde olmalı.', field)


# --------------------------------------------------------------------------
# cron benzeri ifade (5 alan: dk saat gun ay haftanin_gunu)
# --------------------------------------------------------------------------
_CRON_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _parse_cron_field(text, lo, hi):
    out = set()
    for item in text.split(','):
        if not item:
            raise ScheduleError('Cron ifadesi geçersiz.', 'cron')
        step = 1
        base = item
        if '/' in item:
            base, _, st = item.partition('/')
            if not st.isdigit() or int(st) < 1:
                raise ScheduleError('Cron adım değeri geçersiz.', 'cron')
            step = int(st)
        if base == '*':
            a, b = lo, hi
        elif '-' in base:
            x, _, y = base.partition('-')
            if not (x.isdigit() and y.isdigit()):
                raise ScheduleError('Cron aralığı geçersiz.', 'cron')
            a, b = int(x), int(y)
        elif base.isdigit():
            a = int(base)
            b = hi if '/' in item else a
        else:
            raise ScheduleError('Cron ifadesi yalnızca sayı, *, -, , ve / içerebilir.', 'cron')
        if a < lo or b > hi or a > b:
            raise ScheduleError('Cron değeri izin verilen aralığın dışında.', 'cron')
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr):
    if not isinstance(expr, str) or len(expr) > 100:
        raise ScheduleError('Cron ifadesi geçersiz.', 'cron')
    parts = expr.split()
    if len(parts) != 5:
        raise ScheduleError('Cron ifadesi 5 alan içermeli: dakika saat gün ay haftanın-günü.', 'cron')
    sets = []
    for p, (lo, hi) in zip(parts, _CRON_RANGES):
        sets.append(_parse_cron_field(p, lo, hi))
    minutes, hours, doms, months, dows = sets
    dows = {0 if d == 7 else d for d in dows}
    return {
        'minutes': sorted(minutes), 'hours': sorted(hours), 'doms': doms, 'months': months, 'dows': dows,
        'dom_any': parts[2].startswith('*'), 'dow_any': parts[4].startswith('*'),
    }


def _cron_day_ok(c, d):
    if d.month not in c['months']:
        return False
    dom_ok = d.day in c['doms']
    dow_ok = ((d.weekday() + 1) % 7) in c['dows']
    if c['dom_any'] or c['dow_any']:
        return dom_ok and dow_ok
    return dom_ok or dow_ok


def _next_cron(cfg, tz, after):
    c = parse_cron(cfg.get('cron'))
    start = epoch_to_local(after, tz).date()
    for i in range(0, 366 * 8):
        d = start + timedelta(days=i)
        if not _cron_day_ok(c, d):
            continue
        for h in c['hours']:
            for m in c['minutes']:
                ep = local_to_epoch(datetime(d.year, d.month, d.day, h, m), tz)
                if ep > after:
                    return ep
    return None


# --------------------------------------------------------------------------
# sonraki calisma zamani
# --------------------------------------------------------------------------
def _dows(cfg):
    raw = cfg.get('days_of_week')
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ScheduleError('En az bir gün seçilmeli.', 'days_of_week')
    out = set()
    for x in raw:
        if isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 6:
            raise ScheduleError('Gün değerleri 0 (Pazartesi) ile 6 (Pazar) arasında olmalı.', 'days_of_week')
        out.add(x)
    return out


def _next_interval(cfg, tz, after):
    minutes = cfg.get('interval_minutes')
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < config.MIN_INTERVAL_MINUTES or minutes > 7 * 1440:
        raise ScheduleError(f'Aralık en az {config.MIN_INTERVAL_MINUTES} dakika olmalı.', 'interval_minutes')
    window = cfg.get('window')
    dows = _dows(cfg) if cfg.get('days_of_week') else None
    if not window and not dows:
        return after + minutes * 60
    if window:
        ws = _parse_hm(window.get('start'), 'window')
        we = _parse_hm(window.get('end'), 'window')
        if ws == we:
            raise ScheduleError('Saat aralığının başlangıcı ve bitişi farklı olmalı.', 'window')
    else:
        ws, we = (0, 0), (23, 59)
    overnight = we < ws
    start = epoch_to_local(after, tz).date() - timedelta(days=1)
    for i in range(0, 400):
        d = start + timedelta(days=i)
        if dows is not None and d.weekday() not in dows:
            continue
        s_dt = datetime(d.year, d.month, d.day, ws[0], ws[1])
        e_day = d + timedelta(days=1) if overnight else d
        e_dt = datetime(e_day.year, e_day.month, e_day.day, we[0], we[1])
        t = s_dt
        while t <= e_dt:
            ep = local_to_epoch(t, tz)
            if ep > after:
                return ep
            t += timedelta(minutes=minutes)
    return None


def next_recurring(cfg, tz_name, after):
    """Tekrarlayan kural icin 'after' aninden KESIN sonraki calisma zamani (epoch)."""
    tz = get_tz(tz_name)
    freq = cfg.get('frequency')
    if freq not in _FREQS:
        raise ScheduleError('Geçersiz tekrar türü.', 'frequency')
    if freq == 'cron':
        return _next_cron(cfg, tz, after)
    if freq == 'interval':
        return _next_interval(cfg, tz, after)
    h, m = _parse_hm(cfg.get('time'))
    start = epoch_to_local(after, tz).date()
    if freq == 'weekly':
        dows = _dows(cfg)
    elif freq == 'monthly':
        dom = cfg.get('day_of_month')
        if isinstance(dom, bool) or not isinstance(dom, int) or not 1 <= dom <= 31:
            raise ScheduleError('Ayın günü 1 ile 31 arasında olmalı.', 'day_of_month')
    for i in range(0, 800):
        d = start + timedelta(days=i)
        if freq == 'weekdays' and d.weekday() > 4:
            continue
        if freq == 'weekly' and d.weekday() not in dows:
            continue
        if freq == 'monthly':
            last = calendar.monthrange(d.year, d.month)[1]
            if d.day != min(dom, last):
                continue
        ep = local_to_epoch(datetime(d.year, d.month, d.day, h, m), tz)
        if ep > after:
            return ep
    return None


# --------------------------------------------------------------------------
# dogrulama + normalizasyon
# --------------------------------------------------------------------------
def validate_schedule(task_type, cfg, tz_name, now, allow_past=False):
    """Gelen schedule_config'i dogrular ve normallestirir. Hata: ScheduleError."""
    get_tz(tz_name)
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ScheduleError('Zamanlama ayarı geçersiz.')
    if task_type == 'conditional':
        return _validate_conditional_timing(cfg)
    if task_type == 'multistep' and not cfg:
        return {}
    if task_type == 'multistep' and cfg.get('manual'):
        return {}
    if task_type == 'multistep':
        task_type = 'recurring' if cfg.get('frequency') else 'once'
    if task_type == 'once':
        if cfg.get('when') == 'now':
            return {'when': 'now'}
        dt = parse_local_datetime(cfg.get('run_at'))
        tz = get_tz(tz_name)
        ep = local_to_epoch(dt, tz)
        if not allow_past and ep < now - 60:
            raise ScheduleError('Geçmiş bir zaman seçilemez.', 'run_at')
        return {'run_at': dt.strftime('%Y-%m-%dT%H:%M')}
    # recurring
    freq = cfg.get('frequency')
    if freq not in _FREQS:
        raise ScheduleError('Tekrar türü seçilmeli.', 'frequency')
    out = {'frequency': freq}
    if freq in ('daily', 'weekdays', 'weekly', 'monthly'):
        h, m = _parse_hm(cfg.get('time'))
        out['time'] = _hm_str(h, m)
    if freq == 'weekly':
        out['days_of_week'] = sorted(_dows(cfg))
    if freq == 'monthly':
        dom = cfg.get('day_of_month')
        if isinstance(dom, bool) or not isinstance(dom, int) or not 1 <= dom <= 31:
            raise ScheduleError('Ayın günü 1 ile 31 arasında olmalı.', 'day_of_month')
        out['day_of_month'] = dom
    if freq == 'interval':
        minutes = cfg.get('interval_minutes')
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise ScheduleError('Aralık dakika cinsinden tam sayı olmalı.', 'interval_minutes')
        out['interval_minutes'] = minutes
        w = cfg.get('window')
        if w:
            if not isinstance(w, dict):
                raise ScheduleError('Saat aralığı geçersiz.', 'window')
            ws = _parse_hm(w.get('start'), 'window')
            we = _parse_hm(w.get('end'), 'window')
            out['window'] = {'start': _hm_str(*ws), 'end': _hm_str(*we)}
        if cfg.get('days_of_week'):
            out['days_of_week'] = sorted(_dows(cfg))
    if freq == 'cron':
        parse_cron(cfg.get('cron'))
        out['cron'] = ' '.join(str(cfg.get('cron')).split())
    nxt = next_recurring(out, tz_name, now)
    if nxt is None:
        raise ScheduleError('Bu kural için gelecekte bir çalışma zamanı bulunamadı.')
    if freq in ('cron', 'interval'):
        t = now
        prev = None
        for _ in range(6):
            t = next_recurring(out, tz_name, t)
            if t is None:
                break
            if prev is not None and (t - prev) < config.MIN_INTERVAL_MINUTES * 60 - 1:
                raise ScheduleError(f'Çalışma sıklığı en az {config.MIN_INTERVAL_MINUTES} dakika olmalı.')
            prev = t
    return out


def _validate_conditional_timing(cfg):
    minutes = cfg.get('check_interval_minutes', 60)
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ScheduleError('Kontrol aralığı dakika cinsinden tam sayı olmalı.', 'check_interval_minutes')
    if minutes < config.MIN_CHECK_INTERVAL_MINUTES or minutes > 30 * 1440:
        raise ScheduleError(
            f'Kontrol aralığı en az {config.MIN_CHECK_INTERVAL_MINUTES} dakika olmalı (API kredisini korumak için).',
            'check_interval_minutes')
    return {'check_interval_minutes': minutes}


def first_run_at(task_type, cfg, tz_name, now):
    """Yeni/duzenlenen gorev icin ilk next_run_at (epoch) ya da None (elle calistirilacak)."""
    if task_type == 'conditional':
        return now + 30  # ilk kontrol hemen sonra; sonrakiler aralikla
    eff = task_type
    if task_type == 'multistep':
        if not cfg or cfg.get('manual'):
            return None
        eff = 'recurring' if cfg.get('frequency') else 'once'
    if eff == 'once':
        if cfg.get('when') == 'now':
            return now
        return local_to_epoch(parse_local_datetime(cfg['run_at']), get_tz(tz_name))
    return next_recurring(cfg, tz_name, now)


def following_run_at(task_type, cfg, tz_name, after):
    """Bir calistirma bittikten sonra sonraki zaman (epoch) ya da None (tekrar yok)."""
    if task_type == 'conditional':
        return after + int(cfg.get('check_interval_minutes', 60)) * 60
    if task_type == 'multistep' and cfg.get('frequency'):
        return next_recurring(cfg, tz_name, after)
    if task_type == 'recurring':
        return next_recurring(cfg, tz_name, after)
    return None


def is_repeating(task_type, cfg):
    return task_type in ('recurring', 'conditional') or (task_type == 'multistep' and bool(cfg.get('frequency')))


# --------------------------------------------------------------------------
# insan okunur aciklama (Turkce)
# --------------------------------------------------------------------------
def describe(task_type, cfg, tz_name):
    try:
        if task_type == 'conditional':
            return f"Koşul her {_dur(cfg.get('check_interval_minutes', 60))} kontrol edilir"
        if task_type == 'multistep' and (not cfg or cfg.get('manual')):
            return 'Elle başlatılır'
        eff = task_type
        if task_type == 'multistep':
            eff = 'recurring' if cfg.get('frequency') else 'once'
        if eff == 'once':
            if cfg.get('when') == 'now':
                return 'Hemen, tek sefer'
            dt = parse_local_datetime(cfg.get('run_at'))
            return f"{dt.day:02d}.{dt.month:02d}.{dt.year} {dt.hour:02d}:{dt.minute:02d}, tek sefer"
        f = cfg.get('frequency')
        if f == 'daily':
            return f"Her gün {cfg['time']}"
        if f == 'weekdays':
            return f"Hafta içi her gün {cfg['time']}"
        if f == 'weekly':
            days = ', '.join(_TR_DAYS[d] for d in cfg['days_of_week'])
            return f"Her hafta {days} {cfg['time']}"
        if f == 'monthly':
            return f"Her ayın {cfg['day_of_month']}. günü {cfg['time']}"
        if f == 'interval':
            s = f"Her {_dur(cfg['interval_minutes'])}"
            if cfg.get('window'):
                s += f" ({cfg['window']['start']} ile {cfg['window']['end']} arasında)"
            if cfg.get('days_of_week'):
                s += ', ' + ', '.join(_TR_DAYS[d] for d in cfg['days_of_week'])
            return s
        if f == 'cron':
            return f"Cron: {cfg['cron']}"
    except Exception:
        pass
    return ''


def _dur(minutes):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return '?'
    if minutes % 1440 == 0:
        return f'{minutes // 1440} gün'
    if minutes % 60 == 0:
        return f'{minutes // 60} saat'
    return f'{minutes} dakika'
