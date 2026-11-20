"""Task Manager: gorev olusturma/yonetme, durum makinesi, atomik gorev alma (lease) ve bitirme.

Temel garantiler
- Durum gecisleri ALLOWED tablosuyla kontrol edilir; gecersiz gecis InvalidTransition (HTTP 409).
- Tum durum degisiklikleri "WHERE status = beklenen" kosulu (compare-and-swap) ile yapilir:
  iki istek/surec ayni anda ayni gorevi degistirmeye calisirsa yalnizca biri basarili olur.
- Gorev alma (claim) tek bir UPDATE ... WHERE ile yapilir; SQLite yazmalari seri oldugu icin birden fazla
  surec/worker/scheduler ayni gorevi ayni anda ALAMAZ. Alan, lease_owner + lease_until ile sahipligini isaretler.
- Girdiler dogrulanir; metinler kontrol karakterlerinden temizlenir; tum SQL parametrelidir.
"""
import re
import time

from . import config, schedule as sch
from .storage import jdump, jload

ALLOWED = {
    'pending': {'scheduled', 'running', 'paused', 'cancelled', 'awaiting_approval'},
    'scheduled': {'scheduled', 'running', 'paused', 'cancelled', 'awaiting_approval', 'pending'},
    'running': {'completed', 'failed', 'scheduled', 'paused', 'cancelled', 'awaiting_approval'},
    'paused': {'scheduled', 'pending', 'cancelled'},
    'completed': {'scheduled', 'pending', 'running', 'cancelled'},
    'failed': {'scheduled', 'pending', 'running', 'cancelled'},
    'cancelled': {'pending', 'scheduled'},
    'awaiting_approval': {'scheduled', 'running', 'cancelled', 'paused'},
}

CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


class TaskError(Exception):
    http = 400
    code = 'hata'

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class ValidationFailed(TaskError):
    http = 400
    code = 'gecersiz_girdi'


class NotFound(TaskError):
    http = 404
    code = 'bulunamadi'


class InvalidTransition(TaskError):
    http = 409
    code = 'gecersiz_gecis'


class Conflict(TaskError):
    http = 409
    code = 'cakisma'


def can_transition(a, b):
    return b in ALLOWED.get(a, ())


def _bool(v, field, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    raise ValidationFailed('Bu alan doğru/yanlış olmalı.', field)


def _text(v, max_len, field, required=True, single_line=False):
    if v is None:
        v = ''
    if not isinstance(v, str):
        raise ValidationFailed('Bu alan metin olmalı.', field)
    s = CONTROL_RE.sub('', v).replace('\r\n', '\n').replace('\r', '\n').strip()
    if single_line:
        s = re.sub(r'\s+', ' ', s)
    if required and not s:
        raise ValidationFailed('Bu alan boş bırakılamaz.', field)
    if len(s) > max_len:
        raise ValidationFailed(f'En fazla {max_len} karakter olabilir.', field)
    return s


def _choice(v, allowed, field, default):
    if v is None or v == '':
        return default
    if v not in allowed:
        raise ValidationFailed('Geçersiz seçim.', field)
    return v


class TaskManager:
    def __init__(self, db, runs, clock=time.time, allowed_models=None, default_timezone=None):
        self.db = db
        self.runs = runs
        self.clock = clock
        self.allowed_models = list(allowed_models or [])
        self.default_timezone = default_timezone or config.DEFAULT_TIMEZONE

    # ------------------------------------------------------------------
    # okuma
    # ------------------------------------------------------------------
    def _row(self, task_id):
        return self.db.one('SELECT * FROM tasks WHERE id=?', (task_id,))

    def get_row(self, task_id):
        row = self._row(task_id)
        if not row:
            raise NotFound('Görev bulunamadı.')
        return row

    def get(self, task_id):
        return self.serialize(self.get_row(task_id))

    def list(self, filter_name='all', limit=200):
        where, params = '', []
        if filter_name == 'active':
            where = "WHERE status IN ('scheduled','pending','running','awaiting_approval')"
        elif filter_name == 'completed':
            where = "WHERE status='completed'"
        elif filter_name == 'failed':
            where = "WHERE last_run_ok=0 AND status!='cancelled'"
        elif filter_name == 'paused':
            where = "WHERE status IN ('paused','cancelled')"
        elif filter_name not in ('all', None, ''):
            raise ValidationFailed('Geçersiz filtre.', 'filter')
        rows = self.db.all(
            f"SELECT * FROM tasks {where} ORDER BY CASE status WHEN 'running' THEN 0 "
            "WHEN 'awaiting_approval' THEN 1 ELSE 2 END, updated_at DESC, id DESC LIMIT ?",
            tuple(params) + (int(limit),))
        return [self.serialize(r) for r in rows]

    def stats(self):
        r = self.db.one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status IN ('scheduled','pending','running','awaiting_approval') THEN 1 ELSE 0 END) AS active, "
            "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running, "
            "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed, "
            "SUM(CASE WHEN last_run_ok=0 AND status!='cancelled' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN status='awaiting_approval' THEN 1 ELSE 0 END) AS awaiting_approval, "
            "SUM(CASE WHEN status='paused' THEN 1 ELSE 0 END) AS paused "
            "FROM tasks")
        out = {k: int(r.get(k) or 0) for k in
               ('total', 'active', 'running', 'completed', 'failed', 'awaiting_approval', 'paused')}
        nxt = self.db.one(
            "SELECT id, title, next_run_at FROM tasks WHERE status='scheduled' AND next_run_at IS NOT NULL "
            "ORDER BY next_run_at ASC LIMIT 1")
        out['next_task'] = ({'id': nxt['id'], 'title': nxt['title'], 'next_run_at': _iso(nxt['next_run_at'])}
                            if nxt else None)
        return out

    # ------------------------------------------------------------------
    # serilestirme
    # ------------------------------------------------------------------
    def serialize(self, row):
        cfg = jload(row.get('schedule_config'), {}) or {}
        opts = jload(row.get('options'), {}) or {}
        state = jload(row.get('state'), {}) or {}
        st = row['status']
        running = st == 'running'
        out = {
            'id': row['id'],
            'title': row['title'],
            'description': row['description'],
            'task_type': row['task_type'],
            'status': st,
            'schedule_config': cfg,
            'schedule_text': sch.describe(row['task_type'], cfg, row['timezone']),
            'options': {k: opts.get(k) for k in
                        ('research_depth', 'output_format', 'output_length', 'notify_pref', 'condition',
                         'agent_mode', 'agent_budget')
                        if k in opts},
            'timezone': row['timezone'],
            'created_at': _iso(row['created_at']),
            'updated_at': _iso(row['updated_at']),
            'next_run_at': _iso(row['next_run_at']),
            'last_run_at': _iso(row['last_run_at']),
            'last_success_at': _iso(row['last_success_at']),
            'execution_count': row['execution_count'],
            'retry_count': row['retry_count'],
            'selected_model': row['selected_model'],
            'internet_search_enabled': bool(row['internet_search_enabled']),
            'task_priority': row['task_priority'],
            'approval_required': bool(row['approval_required']),
            'current_step': row['current_step'],
            'progress': row['progress'],
            'result': row['result'],
            'last_error': row['last_error'],
            'last_run_ok': None if row['last_run_ok'] is None else bool(row['last_run_ok']),
            'pause_requested': bool(row['pause_requested']),
            'cancel_requested': bool(row['cancel_requested']),
            'is_active': st in ('scheduled', 'pending', 'running', 'awaiting_approval'),
            'condition_state': {k: state.get(k) for k in ('last_check_at', 'last_check_note', 'last_value')
                                if k in state},
        }
        if out['condition_state'].get('last_check_at'):
            out['condition_state']['last_check_at'] = _iso(out['condition_state']['last_check_at'])
        out['can'] = {
            'run': st in ('pending', 'scheduled', 'completed', 'failed', 'cancelled'),
            'pause': st in ('pending', 'scheduled', 'awaiting_approval') or (running and not row['pause_requested']),
            'resume': st == 'paused',
            'retry': st == 'failed',
            'cancel': st in ('pending', 'scheduled', 'paused', 'awaiting_approval', 'failed') or running,
            'edit': not running,
            'delete': not running,
            'approve': st == 'awaiting_approval',
        }
        return out

    # ------------------------------------------------------------------
    # dogrulama
    # ------------------------------------------------------------------
    def _validate_common(self, p, existing=None):
        """Ortak alan dogrulamasi. 'existing' varsa yalnizca gelen alanlar degistirilir (PATCH)."""
        out = {}
        partial = existing is not None
        if not partial or 'title' in p:
            out['title'] = _text(p.get('title'), config.MAX_TITLE_CHARS, 'title', single_line=True)
        if not partial or 'description' in p:
            out['description'] = _text(p.get('description'), config.MAX_DESCRIPTION_CHARS, 'description')
        if not partial or 'task_type' in p:
            tt = p.get('task_type')
            if tt not in config.TASK_TYPES:
                raise ValidationFailed('Görev türü seçilmeli.', 'task_type')
            out['task_type'] = tt
        if not partial or 'timezone' in p:
            tzn = p.get('timezone') or self.default_timezone
            try:
                sch.get_tz(tzn)
            except sch.ScheduleError as e:
                raise ValidationFailed(e.message, 'timezone')
            out['timezone'] = tzn
        if not partial or 'selected_model' in p:
            m = p.get('selected_model') or None
            if m is not None:
                if not isinstance(m, str) or (self.allowed_models and m not in self.allowed_models):
                    raise ValidationFailed('Geçersiz model.', 'selected_model')
            out['selected_model'] = m
        if not partial or 'internet_search_enabled' in p:
            out['internet_search_enabled'] = int(_bool(p.get('internet_search_enabled'), 'internet_search_enabled'))
        if not partial or 'task_priority' in p:
            pr = p.get('task_priority', 2)
            if isinstance(pr, bool) or not isinstance(pr, int) or pr not in (1, 2, 3):
                raise ValidationFailed('Öncelik 1 (yüksek), 2 (normal) veya 3 (düşük) olmalı.', 'task_priority')
            out['task_priority'] = pr
        if not partial or 'approval_required' in p:
            out['approval_required'] = int(_bool(p.get('approval_required'), 'approval_required'))
        return out

    def _validate_options(self, p, existing_opts=None):
        opts = dict(existing_opts or {})
        if 'research_depth' in p or existing_opts is None:
            opts['research_depth'] = _choice(p.get('research_depth'), config.RESEARCH_DEPTHS, 'research_depth', 'normal')
        if 'output_format' in p or existing_opts is None:
            opts['output_format'] = _choice(p.get('output_format'), config.OUTPUT_FORMATS, 'output_format', 'rapor')
        if 'output_length' in p or existing_opts is None:
            opts['output_length'] = _choice(p.get('output_length'), config.OUTPUT_LENGTHS, 'output_length', 'orta')
        if 'notify_pref' in p or existing_opts is None:
            opts['notify_pref'] = _choice(p.get('notify_pref'), config.NOTIFY_PREFS, 'notify_pref', 'hepsi')
        if 'agent_mode' in p or existing_opts is None:
            opts['agent_mode'] = _bool(p.get('agent_mode'), 'agent_mode')
        if 'agent_budget' in p or existing_opts is None:
            opts['agent_budget'] = _choice(p.get('agent_budget'), config.AGENT_BUDGETS, 'agent_budget', 'normal')
        return opts

    def _task_count_check(self):
        n = self.db.one('SELECT COUNT(*) AS n FROM tasks')['n']
        if n >= config.MAX_TASKS:
            raise Conflict(f'En fazla {config.MAX_TASKS} görev oluşturulabilir. Eski görevleri silin.')

    # ------------------------------------------------------------------
    # olusturma / guncelleme / silme
    # ------------------------------------------------------------------
    def create(self, p):
        if not isinstance(p, dict):
            raise ValidationFailed('Geçersiz istek.')
        now = self.clock()
        common = self._validate_common(p)
        tt = common['task_type']
        tzn = common['timezone']
        opts = self._validate_options(p)
        if tt == 'conditional':
            from . import conditions
            opts['condition'] = conditions.validate_condition(p.get('condition'))
            if opts['condition']['type'] in ('news', 'search_condition'):
                common['internet_search_enabled'] = 1
        try:
            cfg = sch.validate_schedule(tt, p.get('schedule_config'), tzn, now)
            nxt = sch.first_run_at(tt, cfg, tzn, now)
        except sch.ScheduleError as e:
            raise ValidationFailed(e.message, e.field)
        self._task_count_check()
        status = 'scheduled' if nxt is not None else 'pending'
        with self.db.tx() as c:
            cur = c.execute(
                'INSERT INTO tasks(title, description, task_type, status, schedule_config, options, state, timezone, '
                'created_at, updated_at, next_run_at, selected_model, internet_search_enabled, task_priority, '
                'approval_required, progress) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)',
                (common['title'], common['description'], tt, status, jdump(cfg), jdump(opts), '{}', tzn,
                 now, now, nxt, common['selected_model'], common['internet_search_enabled'],
                 common['task_priority'], common['approval_required']))
            tid = cur.lastrowid
        return self.get(tid)

    def update(self, task_id, p):
        if not isinstance(p, dict):
            raise ValidationFailed('Geçersiz istek.')
        row = self.get_row(task_id)
        if row['status'] == 'running':
            raise Conflict('Çalışan görev düzenlenemez. Önce duraklatın veya bitmesini bekleyin.')
        now = self.clock()
        common = self._validate_common(p, existing=row)
        opts = self._validate_options(p, jload(row['options'], {}) or {})
        tt = common.get('task_type', row['task_type'])
        tzn = common.get('timezone', row['timezone'])
        old_cfg = jload(row['schedule_config'], {}) or {}
        cfg = old_cfg
        sets = dict(common)
        timing_changed = any(k in p for k in ('schedule_config', 'task_type', 'timezone'))
        if tt == 'conditional':
            from . import conditions
            if 'condition' in p or row['task_type'] != 'conditional':
                opts['condition'] = conditions.validate_condition(p.get('condition', opts.get('condition')))
            if opts.get('condition', {}).get('type') in ('news', 'search_condition'):
                sets['internet_search_enabled'] = 1
        else:
            opts.pop('condition', None)
        if timing_changed:
            try:
                cfg = sch.validate_schedule(tt, p.get('schedule_config', old_cfg), tzn, now)
            except sch.ScheduleError as e:
                raise ValidationFailed(e.message, e.field)
            sets['schedule_config'] = jdump(cfg)
        sets['options'] = jdump(opts)
        status = row['status']
        if timing_changed:
            try:
                nxt = sch.first_run_at(tt, cfg, tzn, now)
            except sch.ScheduleError as e:
                raise ValidationFailed(e.message, e.field)
            if status in ('paused', 'cancelled'):
                sets['next_run_at'] = nxt  # durum korunur; devam ettirilince kullanilir
            else:
                sets['next_run_at'] = nxt
                new_status = 'scheduled' if nxt is not None else 'pending'
                if status != 'awaiting_approval' and can_transition(status, new_status):
                    sets['status'] = new_status
                    sets['retry_count'] = 0
                    sets['resume_run_id'] = None
                    sets['state'] = '{}'  # kosul tabani (baseline) sifirlanir
        sets['updated_at'] = now
        cols = ', '.join(f'{k}=?' for k in sets)
        with self.db.tx() as c:
            cur = c.execute(f'UPDATE tasks SET {cols} WHERE id=? AND status!=\'running\'',
                            tuple(sets.values()) + (task_id,))
            if cur.rowcount != 1:
                raise Conflict('Görev şu anda düzenlenemiyor (çalışmaya başlamış olabilir).')
        return self.get(task_id)

    def delete(self, task_id):
        with self.db.tx() as c:
            row = c.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise NotFound('Görev bulunamadı.')
            if row['status'] == 'running':
                raise Conflict('Çalışan görev silinemez. Önce iptal edin.')
            c.execute('DELETE FROM tasks WHERE id=?', (task_id,))
        return {'deleted': task_id}

    # ------------------------------------------------------------------
    # kullanici eylemleri (durum gecisleri)
    # ------------------------------------------------------------------
    def _cas(self, task_id, expect_status, sets, extra_where=''):
        sets = dict(sets)
        sets['updated_at'] = self.clock()
        cols = ', '.join(f'{k}=?' for k in sets)
        cur = self.db.execute(
            f'UPDATE tasks SET {cols} WHERE id=? AND status=? {extra_where}',
            tuple(sets.values()) + (task_id, expect_status))
        return cur.rowcount == 1

    def _need(self, row, targets, message):
        for t in targets:
            if can_transition(row['status'], t):
                return
        raise InvalidTransition(message)

    def run_now(self, task_id):
        row = self.get_row(task_id)
        if row['status'] not in ('pending', 'scheduled', 'completed', 'failed', 'cancelled'):
            raise InvalidTransition(_why_not('çalıştırılamaz', row['status']))
        now = self.clock()
        ok = self._cas(task_id, row['status'], {
            'status': 'scheduled', 'next_run_at': now, 'run_trigger': 'manual', 'resume_run_id': None,
            'approved': 1, 'retry_count': 0, 'pause_requested': 0, 'cancel_requested': 0,
        })
        if not ok:
            raise Conflict('Görevin durumu aynı anda değişti, tekrar deneyin.')
        return self.get(task_id)

    def pause(self, task_id):
        row = self.get_row(task_id)
        st = row['status']
        if st == 'running':
            if row['pause_requested']:
                return self.get(task_id)
            self.db.execute("UPDATE tasks SET pause_requested=1, updated_at=? WHERE id=? AND status='running'",
                            (self.clock(), task_id))
            return self.get(task_id)
        if st not in ('pending', 'scheduled', 'awaiting_approval'):
            raise InvalidTransition(_why_not('duraklatılamaz', st))
        if not self._cas(task_id, st, {'status': 'paused'}):
            raise Conflict('Görevin durumu aynı anda değişti, tekrar deneyin.')
        return self.get(task_id)

    def resume(self, task_id):
        row = self.get_row(task_id)
        if row['status'] != 'paused':
            raise InvalidTransition(_why_not('devam ettirilemez', row['status']))
        now = self.clock()
        cfg = jload(row['schedule_config'], {}) or {}
        tt, tzn = row['task_type'], row['timezone']
        sets = {'pause_requested': 0, 'cancel_requested': 0}
        if row['resume_run_id']:
            sets.update(status='scheduled', next_run_at=now, run_trigger='resume')
        else:
            nxt = row['next_run_at']
            try:
                if nxt is None or nxt < now:
                    if sch.is_repeating(tt, cfg):
                        nxt = sch.following_run_at(tt, cfg, tzn, now)
                    elif tt == 'once' or (tt == 'multistep' and cfg and not cfg.get('manual')):
                        nxt = now
                    else:
                        nxt = None
            except sch.ScheduleError:
                nxt = None
            sets.update(status='scheduled' if nxt is not None else 'pending', next_run_at=nxt)
        if not self._cas(task_id, 'paused', sets):
            raise Conflict('Görevin durumu aynı anda değişti, tekrar deneyin.')
        return self.get(task_id)

    def cancel(self, task_id):
        row = self.get_row(task_id)
        st = row['status']
        if st == 'running':
            self.db.execute("UPDATE tasks SET cancel_requested=1, updated_at=? WHERE id=? AND status='running'",
                            (self.clock(), task_id))
            return self.get(task_id)
        if st not in ('pending', 'scheduled', 'paused', 'awaiting_approval', 'failed'):
            raise InvalidTransition(_why_not('iptal edilemez', st))
        with self.db.tx() as c:
            if row['resume_run_id']:
                c.execute("UPDATE task_runs SET status='cancelled', finished_at=? WHERE id=? AND status!='succeeded'",
                          (self.clock(), row['resume_run_id']))
            ok = self._cas(task_id, st, {
                'status': 'cancelled', 'next_run_at': None, 'resume_run_id': None,
                'current_step': 'İptal edildi', 'pause_requested': 0, 'cancel_requested': 0})
            if not ok:
                raise Conflict('Görevin durumu aynı anda değişti, tekrar deneyin.')
        return self.get(task_id)

    def retry(self, task_id):
        row = self.get_row(task_id)
        if row['status'] != 'failed':
            raise InvalidTransition(_why_not('tekrar denenemez', row['status']))
        now = self.clock()
        if not self._cas(task_id, 'failed', {
                'status': 'scheduled', 'next_run_at': now, 'retry_count': 0, 'run_trigger': 'retry',
                'approved': 1, 'pause_requested': 0, 'cancel_requested': 0}):
            raise Conflict('Görevin durumu aynı anda değişti, tekrar deneyin.')
        return self.get(task_id)

    def approve(self, task_id):
        row = self.get_row(task_id)
        if row['status'] != 'awaiting_approval':
            raise InvalidTransition('Bu görev onay beklemiyor.')
        trig = 'condition' if row['run_trigger'] == 'condition' else 'schedule'
        if not self._cas(task_id, 'awaiting_approval', {
                'status': 'scheduled', 'next_run_at': self.clock(), 'approved': 1, 'run_trigger': trig}):
            raise Conflict('Görevin durumu aynı anda değişti, tekrar deneyin.')
        return self.get(task_id)

    def reject(self, task_id):
        row = self.get_row(task_id)
        if row['status'] != 'awaiting_approval':
            raise InvalidTransition('Bu görev onay beklemiyor.')
        now = self.clock()
        cfg = jload(row['schedule_config'], {}) or {}
        if sch.is_repeating(row['task_type'], cfg):
            nxt = sch.following_run_at(row['task_type'], cfg, row['timezone'], now)
            sets = {'status': 'scheduled', 'next_run_at': nxt, 'approved': 0, 'current_step': 'Bu çalıştırma reddedildi'}
        else:
            sets = {'status': 'cancelled', 'next_run_at': None, 'approved': 0, 'current_step': 'Onay reddedildi'}
        if not self._cas(task_id, 'awaiting_approval', sets):
            raise Conflict('Görevin durumu aynı anda değişti, tekrar deneyin.')
        return self.get(task_id)

    # ------------------------------------------------------------------
    # zamanlayici tarafi: onaya ayirma + atomik gorev alma
    # ------------------------------------------------------------------
    def promote_approvals(self, now):
        """Zamani gelmis ve onay gerektiren gorevleri onay bekliyor durumuna alir. Donus: [task_id]."""
        ids = []
        with self.db.tx() as c:
            rows = c.execute(
                "SELECT id FROM tasks WHERE status='scheduled' AND next_run_at<=? AND approval_required=1 "
                "AND approved=0 AND task_type!='conditional'", (now,)).fetchall()
            for r in rows:
                cur = c.execute(
                    "UPDATE tasks SET status='awaiting_approval', current_step='Onayınız bekleniyor', "
                    "updated_at=? WHERE id=? AND status='scheduled' AND approval_required=1 AND approved=0 "
                    "AND task_type!='conditional'", (now, r['id']))
                if cur.rowcount == 1:
                    ids.append(r['id'])
        return ids

    def skip_missed(self, now):
        """Tekrarlayan gorevlerde asiri gecikmis (bekleme suresini asan) calistirmalari atlayip ileri alir."""
        skipped = []
        rows = self.db.all(
            "SELECT id, task_type, schedule_config, timezone, next_run_at FROM tasks WHERE status='scheduled' "
            "AND next_run_at IS NOT NULL AND next_run_at<? AND (run_trigger IS NULL OR run_trigger='schedule' "
            "OR run_trigger='condition')", (now - config.MISSED_GRACE_SECONDS,))
        for r in rows:
            cfg = jload(r['schedule_config'], {}) or {}
            if r['task_type'] == 'conditional' or not sch.is_repeating(r['task_type'], cfg):
                continue  # tek seferlik gorev gec de olsa calistirilir; kosul kontrolleri zaten ileri alinir
            try:
                nxt = sch.following_run_at(r['task_type'], cfg, r['timezone'], now)
            except sch.ScheduleError:
                continue
            cur = self.db.execute(
                "UPDATE tasks SET next_run_at=?, updated_at=? WHERE id=? AND status='scheduled' AND next_run_at=?",
                (nxt, now, r['id'], r['next_run_at']))
            if cur.rowcount == 1:
                skipped.append(r['id'])
        return skipped

    def claim_due(self, owner, limit, now):
        """Zamani gelen gorevlerden en fazla 'limit' tanesini ATOMIK olarak alir. Donus: [{'task','run'}]."""
        if limit <= 0:
            return []
        out = []
        ids = [r['id'] for r in self.db.all(
            "SELECT id FROM tasks WHERE status='scheduled' AND next_run_at<=? "
            "AND (approval_required=0 OR approved=1 OR (task_type='conditional' AND "
            "(run_trigger IS NULL OR run_trigger='schedule'))) "
            "ORDER BY task_priority ASC, next_run_at ASC, id ASC LIMIT ?",
            (now, int(limit)))]
        for tid in ids:
            claim = self._claim_one(tid, owner, now)
            if claim:
                out.append(claim)
        return out

    def _claim_one(self, tid, owner, now):
        lease = now + config.LEASE_SECONDS
        with self.db.tx() as c:
            row = c.execute(
                "SELECT * FROM tasks WHERE id=? AND status='scheduled' AND next_run_at<=?", (tid, now)).fetchone()
            if not row:
                return None
            row = dict(row)
            late = max(0.0, now - (row['next_run_at'] or now))
            cur = c.execute(
                "UPDATE tasks SET status='running', lease_owner=?, lease_until=?, last_run_at=?, updated_at=?, "
                "next_run_at=NULL, approved=0, pause_requested=0, cancel_requested=0, current_step='Başlatılıyor', "
                "progress=0 WHERE id=? AND status='scheduled' AND next_run_at<=?",
                (owner, lease, now, now, tid, row['next_run_at'] if row['next_run_at'] is not None else now))
            if cur.rowcount != 1:
                return None
            trigger = row['run_trigger'] or 'schedule'
            run = None
            resume = row['resume_run_id']
            if resume:
                prev = c.execute('SELECT * FROM task_runs WHERE id=? AND task_id=?', (resume, tid)).fetchone()
                if prev and prev['status'] != 'succeeded':
                    c.execute("UPDATE task_runs SET status='running', attempt=attempt+1, finished_at=NULL WHERE id=?",
                              (resume,))
                    run = dict(c.execute('SELECT * FROM task_runs WHERE id=?', (resume,)).fetchone())
            is_condition_check = (run is None and row['task_type'] == 'conditional'
                                  and trigger not in ('manual', 'condition', 'retry', 'resume', 'recovery'))
            if run is None and not is_condition_check:
                run_no = row['execution_count'] + 1
                rid = c.execute(
                    'INSERT INTO task_runs(task_id, run_no, trigger, status, attempt, started_at, late_seconds) '
                    "VALUES(?,?,?,?,1,?,?)", (tid, run_no, trigger, 'running', now, late)).lastrowid
                run = dict(c.execute('SELECT * FROM task_runs WHERE id=?', (rid,)).fetchone())
            task = dict(c.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        return {'task': task, 'run': run, 'trigger': trigger, 'late': late, 'check_only': is_condition_check}

    def open_condition_run(self, task_id, owner, note=''):
        """Kosul saglandi: bu kontrol icin bir calistirma kaydi acar ve gorevi 'condition' tetikli isaretler."""
        now = self.clock()
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=? AND status='running' AND lease_owner=?",
                            (task_id, owner)).fetchone()
            if not row:
                return None
            rid = c.execute(
                'INSERT INTO task_runs(task_id, run_no, trigger, status, attempt, started_at) VALUES(?,?,?,?,1,?)',
                (task_id, row['execution_count'] + 1, 'condition', 'running', now)).lastrowid
            c.execute("UPDATE tasks SET run_trigger='condition', resume_run_id=?, updated_at=? WHERE id=?",
                      (rid, now, task_id))
            return dict(c.execute('SELECT * FROM task_runs WHERE id=?', (rid,)).fetchone())

    # ------------------------------------------------------------------
    # calisma sirasinda: heartbeat, ilerleme, istekleri okuma
    # ------------------------------------------------------------------
    def heartbeat(self, task_id, owner):
        now = self.clock()
        cur = self.db.execute(
            "UPDATE tasks SET lease_until=? WHERE id=? AND status='running' AND lease_owner=?",
            (now + config.LEASE_SECONDS, task_id, owner))
        return cur.rowcount == 1

    def set_progress(self, task_id, owner, current_step=None, progress=None):
        sets, params = ['updated_at=?'], [self.clock()]
        if current_step is not None:
            sets.append('current_step=?')
            params.append(str(current_step)[:160])
        if progress is not None:
            sets.append('progress=?')
            params.append(max(0, min(99, int(progress))))
        params += [task_id, owner]
        cur = self.db.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id=? AND status='running' AND lease_owner=?", tuple(params))
        return cur.rowcount == 1

    def control_flags(self, task_id, owner):
        r = self.db.one(
            "SELECT pause_requested, cancel_requested, status, lease_owner FROM tasks WHERE id=?", (task_id,))
        if not r or r['status'] != 'running' or r['lease_owner'] != owner:
            return {'lost': True, 'pause': False, 'cancel': False}
        return {'lost': False, 'pause': bool(r['pause_requested']), 'cancel': bool(r['cancel_requested'])}

    def update_state(self, task_id, patch):
        """Kosul izleme durumunu (baseline, son kontrol) birlestirerek kaydeder."""
        with self.db.tx() as c:
            row = c.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return
            st = jload(row['state'], {}) or {}
            st.update(patch)
            c.execute('UPDATE tasks SET state=? WHERE id=?', (jdump(st), task_id))

    def get_state(self, task_id):
        row = self.db.one('SELECT state FROM tasks WHERE id=?', (task_id,))
        return (jload(row['state'], {}) if row else {}) or {}

    # ------------------------------------------------------------------
    # bitirme (hepsi lease sahibi + running kosuluyla; bayat worker bir kurtarilmis gorevi ezemez)
    # ------------------------------------------------------------------
    def finish_success(self, task_id, run_id, owner, result, model=None):
        now = self.clock()
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=? AND status='running' AND lease_owner=?",
                            (task_id, owner)).fetchone()
            if not row:
                return False
            cfg = jload(row['schedule_config'], {}) or {}
            repeating = sch.is_repeating(row['task_type'], cfg)
            nxt = None
            if repeating:
                try:
                    nxt = sch.following_run_at(row['task_type'], cfg, row['timezone'], now)
                except sch.ScheduleError:
                    nxt = None
            status = 'scheduled' if nxt is not None else 'completed'
            c.execute("UPDATE task_runs SET status='succeeded', finished_at=?, result=?, error=NULL, model_used=COALESCE(?, model_used) WHERE id=?",
                      (now, result, model, run_id))
            c.execute(
                'UPDATE tasks SET status=?, next_run_at=?, last_success_at=?, last_run_ok=1, last_error=NULL, '
                'execution_count=execution_count+1, retry_count=0, result=?, progress=100, current_step=?, '
                'resume_run_id=NULL, run_trigger=NULL, lease_owner=NULL, lease_until=NULL, pause_requested=0, '
                'cancel_requested=0, updated_at=? WHERE id=?',
                (status, nxt, now, result, 'Tamamlandı' if status == 'completed' else 'Tamamlandı, sonraki çalışma bekleniyor',
                 now, task_id))
        self.runs.prune(task_id)
        return True

    def backoff_seconds(self, attempt_no):
        import random
        base = min(config.BACKOFF_CAP_SECONDS, config.BACKOFF_BASE_SECONDS * (2 ** max(0, attempt_no)))
        return base + random.uniform(0, base * 0.1)

    def finish_failure(self, task_id, run_id, owner, error, retriable):
        """Donus: 'retry' (geri cekilmeyle yeniden planlandi) veya 'failed' (kalici) veya None (sahiplik kayip)."""
        now = self.clock()
        error = str(error)[:600]
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=? AND status='running' AND lease_owner=?",
                            (task_id, owner)).fetchone()
            if not row:
                return None
            run = c.execute('SELECT attempt FROM task_runs WHERE id=?', (run_id,)).fetchone()
            attempt = run['attempt'] if run else 1
            if retriable and row['retry_count'] < config.MAX_RETRIES and attempt < config.MAX_RUN_ATTEMPTS:
                delay = self.backoff_seconds(row['retry_count'])
                c.execute("UPDATE task_runs SET status='retry_wait', error=? WHERE id=?", (error, run_id))
                c.execute(
                    "UPDATE tasks SET status='scheduled', next_run_at=?, retry_count=retry_count+1, last_error=?, "
                    "last_run_ok=0, run_trigger='retry', resume_run_id=?, lease_owner=NULL, lease_until=NULL, "
                    "current_step=?, updated_at=? WHERE id=?",
                    (now + delay, error, run_id, f'Hata: yeniden denenecek ({int(delay)} sn sonra)', now, task_id))
                return 'retry'
            cfg = jload(row['schedule_config'], {}) or {}
            repeating = sch.is_repeating(row['task_type'], cfg)
            nxt = None
            if repeating:
                try:
                    nxt = sch.following_run_at(row['task_type'], cfg, row['timezone'], now)
                except sch.ScheduleError:
                    nxt = None
            c.execute("UPDATE task_runs SET status='failed', error=?, finished_at=? WHERE id=?", (error, now, run_id))
            if nxt is not None:
                c.execute(
                    "UPDATE tasks SET status='scheduled', next_run_at=?, last_error=?, last_run_ok=0, retry_count=0, "
                    "resume_run_id=NULL, run_trigger=NULL, lease_owner=NULL, lease_until=NULL, "
                    "current_step='Başarısız, sonraki çalışma bekleniyor', updated_at=? WHERE id=?",
                    (nxt, error, now, task_id))
            else:
                c.execute(
                    "UPDATE tasks SET status='failed', next_run_at=NULL, last_error=?, last_run_ok=0, "
                    "resume_run_id=?, run_trigger=NULL, lease_owner=NULL, lease_until=NULL, "
                    "current_step='Başarısız', updated_at=? WHERE id=?",
                    (error, run_id, now, task_id))
            return 'failed'

    def finish_paused(self, task_id, run_id, owner):
        now = self.clock()
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE tasks SET status='paused', resume_run_id=?, pause_requested=0, lease_owner=NULL, "
                "lease_until=NULL, current_step='Duraklatıldı', updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=?", (run_id, now, task_id, owner))
            if cur.rowcount != 1:
                return False
            if run_id:
                c.execute("UPDATE task_runs SET status='paused' WHERE id=?", (run_id,))
        return True

    def finish_cancelled(self, task_id, run_id, owner):
        now = self.clock()
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE tasks SET status='cancelled', next_run_at=NULL, resume_run_id=NULL, cancel_requested=0, "
                "pause_requested=0, lease_owner=NULL, lease_until=NULL, current_step='İptal edildi', updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=?", (now, task_id, owner))
            if cur.rowcount != 1:
                return False
            if run_id:
                c.execute("UPDATE task_runs SET status='cancelled', finished_at=? WHERE id=?", (now, run_id))
        return True

    def await_approval(self, task_id, owner, note='Koşul sağlandı, onayınız bekleniyor'):
        """Kosullu gorevde kosul saglandi ama onay gerekiyor: gorevi onay bekliyor durumuna alir."""
        now = self.clock()
        cur = self.db.execute(
            "UPDATE tasks SET status='awaiting_approval', run_trigger='condition', resume_run_id=NULL, "
            "lease_owner=NULL, lease_until=NULL, approved=0, current_step=?, updated_at=? "
            "WHERE id=? AND status='running' AND lease_owner=?", (str(note)[:160], now, task_id, owner))
        return cur.rowcount == 1

    def release_check(self, task_id, owner, note):
        """Kosul henuz saglanmadi: gorev sonraki kontrole kadar beklemeye doner (calistirma kaydi acilmaz)."""
        now = self.clock()
        row = self.get_row(task_id)
        cfg = jload(row['schedule_config'], {}) or {}
        nxt = now + int(cfg.get('check_interval_minutes', 60)) * 60
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE tasks SET status='scheduled', next_run_at=?, lease_owner=NULL, lease_until=NULL, "
                "current_step=?, updated_at=? WHERE id=? AND status='running' AND lease_owner=?",
                (nxt, str(note)[:160], now, task_id, owner))
            return cur.rowcount == 1

    def requeue_after_check_error(self, task_id, owner, error):
        """Kosul kontrolu teknik hata verdi: hatayi kaydet, sonraki kontrole kadar bekle (gorevi oldurme)."""
        now = self.clock()
        row = self.get_row(task_id)
        cfg = jload(row['schedule_config'], {}) or {}
        nxt = now + int(cfg.get('check_interval_minutes', 60)) * 60
        cur = self.db.execute(
            "UPDATE tasks SET status='scheduled', next_run_at=?, lease_owner=NULL, lease_until=NULL, last_error=?, "
            "current_step='Kontrol hatası, sonraki kontrol bekleniyor', updated_at=? "
            "WHERE id=? AND status='running' AND lease_owner=?", (nxt, str(error)[:600], now, task_id, owner))
        return cur.rowcount == 1

    # ------------------------------------------------------------------
    # bakim
    # ------------------------------------------------------------------
    def cleanup(self):
        now = self.clock()
        cutoff = now - config.RETENTION_DAYS * 86400
        self.db.execute("DELETE FROM tasks WHERE status IN ('completed','cancelled') AND updated_at<?", (cutoff,))
        self.db.execute('DELETE FROM task_notifications WHERE created_at<?',
                        (now - config.NOTIFICATION_KEEP_DAYS * 86400,))
        self.db.execute('DELETE FROM task_cache WHERE expires_at<?', (now,))


def _iso(epoch):
    if epoch is None:
        return None
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(float(epoch)))


def _why_not(what, status):
    names = {
        'pending': 'beklemede', 'scheduled': 'zamanlanmış', 'running': 'çalışıyor', 'paused': 'duraklatılmış',
        'completed': 'tamamlanmış', 'failed': 'başarısız', 'cancelled': 'iptal edilmiş',
        'awaiting_approval': 'onay bekliyor',
    }
    return f'Görev {names.get(status, status)} durumunda olduğu için {what}.'
