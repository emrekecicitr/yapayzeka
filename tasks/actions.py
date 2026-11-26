"""Eylem servisi: ajanin ONERDIGI, kullanicinin ONAYLADIGI ve SISTEMIN calistirdigi eylemlerin tek sahibi.

Degismezler
- Model hicbir sey calistirmaz: propose() yalnizca kayit olusturur. Calistirma yalnizca onayli kayit uzerinden, burada olur.
- Durum gecisleri TABLOYLA denetlenir ve her gecis compare-and-swap (UPDATE ... WHERE status=beklenen) + ayni islemde denetim kaydidir.
  Ayni eylem iki kez calistirilamaz: yalnizca 'approved -> executing' gecisini kazanan calistirir.
- Onay args_hash'e baglidir; argumanlar degistirilemez (DB trigger'i da engeller); hash her adimda yeniden hesaplanir.
- Calistirma aninda arguman YENIDEN dogrulanir (spec.validate): yol/sembolik bag kontrolu onay aninda degil, calistirma aninda da yapilir.
- 'executing' iken surec olursa eylem ASLA otomatik tekrar calistirilmaz: dogrulanabiliyorsa dogrulanir, degilse 'outcome_unknown'.
- Eylem sistemi bir IYILESTIRMEDIR: dis cagrilar (bildirim, hafiza notu) hata verirse eylem durumu etkilenmez.
"""
import logging
import sqlite3
import time

from . import action_specs as specs
from . import config, util
from .fsafe import ActionError
from .manager import Conflict, InvalidTransition, NotFound, ValidationFailed
from .storage import jload

log = logging.getLogger('tasks')

STATUSES = ('proposed', 'approved', 'rejected', 'executing', 'succeeded', 'failed', 'outcome_unknown', 'expired', 'cancelled')
TERMINAL = ('rejected', 'succeeded', 'failed', 'outcome_unknown', 'expired', 'cancelled')
ALLOWED = {
    'proposed': ('approved', 'rejected', 'cancelled', 'expired'),
    'approved': ('executing', 'cancelled', 'expired'),
    'executing': ('succeeded', 'failed', 'outcome_unknown'),
}
STATUS_LABEL = {'proposed': 'Onay bekliyor', 'approved': 'Onaylandı', 'rejected': 'Reddedildi', 'executing': 'Çalışıyor',
                'succeeded': 'Tamamlandı', 'failed': 'Başarısız', 'outcome_unknown': 'Sonuç belirsiz',
                'expired': 'Süresi doldu', 'cancelled': 'İptal edildi'}
TAINT_WARNING = ('Bu eylem web içeriğinden etkilenmiş olabilir (web, arama, okunan sayfa ya da otomatik hafıza kaydı). '
                 'Önizlemeyi dikkatle okuyun; onaylamak için confirm_tainted=true göndermelisiniz.')


class ActionLimit(Exception):
    """Sinir asimi (modele ve kullaniciya acik hata olarak gosterilir)."""


class Proposal:
    __slots__ = ('id', 'duplicate', 'preview', 'status', 'tainted', 'risk')

    def __init__(self, id, duplicate, preview, status, tainted, risk):
        self.id, self.duplicate, self.preview, self.status, self.tainted, self.risk = id, duplicate, preview, status, tainted, risk


def _hash8(h):
    return str(h)[:12]


class Actions:
    def __init__(self, db, clock, notifier=None, memory=None, workspace=None):
        self.db = db
        self.clock = clock
        self.notifier = notifier
        self.memory = memory
        self.env = specs.ActionEnv(clock, workspace)
        # kurulum denetimi: tablolar yoksa (migration gelmemis) istisna -> build_services eylemsiz devam eder
        self.db.one('SELECT COUNT(*) AS n FROM task_actions')
        self.db.one('SELECT COUNT(*) AS n FROM action_audit')

    # ------------------------------------------------------------------ yardimcilar
    @staticmethod
    def enabled():
        return config.actions_enabled()

    def _audit(self, c, aid, task_id, event, frm=None, to=None, args_hash=None, detail=''):
        c.execute('INSERT INTO action_audit(action_id, task_id, event, status_from, status_to, args_hash, detail, created_at) '
                  'VALUES(?,?,?,?,?,?,?,?)',
                  (aid, task_id, event, frm, to, args_hash, util.redact(str(detail))[:500], self.clock()))

    def _transition(self, aid, frm, to, event, detail='', sets=None, extra_where='', extra_params=()):
        """Atomik CAS gecisi + denetim kaydi. Donus: True (gecis bizim) / False (baskasi once davrandi)."""
        if to not in ALLOWED.get(frm, ()):
            raise RuntimeError(f'gecersiz eylem gecisi {frm}->{to}')
        sets = dict(sets or {})
        sets['status'] = to
        cols = ', '.join(f'{k}=?' for k in sets)
        with self.db.tx() as c:
            cur = c.execute(f'UPDATE task_actions SET {cols} WHERE id=? AND status=? {extra_where}',
                            tuple(sets.values()) + (aid, frm) + tuple(extra_params))
            if cur.rowcount != 1:
                return False
            row = c.execute('SELECT task_id, args_hash FROM task_actions WHERE id=?', (aid,)).fetchone()
            self._audit(c, aid, row['task_id'], event, frm, to, row['args_hash'], detail)
        return True

    def _row(self, aid):
        r = self.db.one('SELECT * FROM task_actions WHERE id=?', (aid,))
        if not r:
            raise NotFound('Eylem bulunamadı.')
        return r

    def _notify(self, task_id, kind, title, body, aid):
        if self.notifier is None:
            return
        try:
            row = self.db.one('SELECT * FROM tasks WHERE id=?', (task_id,))
            if row:
                self.notifier.notify(row, kind, title, body, dedupe_key=f'{kind}:{aid}')
        except Exception as e:  # noqa: BLE001 - bildirim eylemi bozmaz
            log.warning('eylem bildirimi olusturulamadi: %s', type(e).__name__)

    # ------------------------------------------------------------------ oneri
    def propose(self, task, run, tool, args, tainted):
        """Ajanin eylem onerisini KAYDEDER (calistirmaz). Hata: ActionError (gecersiz arguman), ActionLimit (sinir)."""
        if not self.enabled():
            raise ActionLimit('Eylem sistemi şu anda kapalı.')
        spec = specs.get(tool)
        if spec is None:
            raise ActionError('Bilinmeyen eylem aracı. Geçerli araçlar: ' + ', '.join(specs.names()) + '.')
        norm = spec.validate(args, self.env)
        canon = specs.canonical_json(norm)
        if len(canon.encode('utf-8')) > config.ACTIONS_ARGS_BYTES:
            raise ActionLimit(f'Eylem argümanları en fazla {config.ACTIONS_ARGS_BYTES} bayt olabilir.')
        ahash = specs.args_hash_of(norm)
        preview = spec.preview(norm, self.env)
        risk = spec.risk_of(norm)
        run_id = run['id'] if run else None
        idem = specs.args_hash_of({'run': run_id, 'task': task['id'], 'tool': tool, 'h': ahash})
        now = self.clock()
        with self.db.tx() as c:
            ex = c.execute('SELECT id, status, tainted, risk, preview FROM task_actions WHERE idempotency_key=?', (idem,)).fetchone()
            if ex:
                return Proposal(ex['id'], True, ex['preview'], ex['status'], bool(ex['tainted']), ex['risk'])
            n_run = c.execute('SELECT COUNT(*) FROM task_actions WHERE run_id IS ? AND task_id=?', (run_id, task['id'])).fetchone()[0]
            if n_run >= config.ACTIONS_PER_RUN:
                raise ActionLimit(f'Bu çalıştırmada en fazla {config.ACTIONS_PER_RUN} eylem önerilebilir; sınır doldu.')
            n_day = c.execute('SELECT COUNT(*) FROM task_actions WHERE created_at>=?', (now - 86400,)).fetchone()[0]
            if n_day >= config.ACTIONS_PER_DAY:
                raise ActionLimit(f'Son 24 saatte en fazla {config.ACTIONS_PER_DAY} eylem önerilebilir; günlük sınır doldu.')
            n_pend = c.execute("SELECT COUNT(*) FROM task_actions WHERE status IN ('proposed','approved') AND expires_at>?",
                               (now,)).fetchone()[0]
            if n_pend >= config.ACTIONS_PENDING_MAX:
                raise ActionLimit(f'Onay bekleyen en fazla {config.ACTIONS_PENDING_MAX} eylem olabilir; bekleyenler sonuçlanmadan yenisi önerilemez.')
            try:
                cur = c.execute(
                    'INSERT INTO task_actions(task_id, run_id, tool, args, args_hash, preview, status, risk, tainted, '
                    'idempotency_key, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (task['id'], run_id, tool, canon, ahash, preview, 'proposed', risk, 1 if tainted else 0, idem, now,
                     now + config.ACTIONS_TTL_SECONDS))
            except sqlite3.IntegrityError:
                ex = c.execute('SELECT id, status, tainted, risk, preview FROM task_actions WHERE idempotency_key=?', (idem,)).fetchone()
                if ex:
                    return Proposal(ex['id'], True, ex['preview'], ex['status'], bool(ex['tainted']), ex['risk'])
                raise
            aid = cur.lastrowid
            self._audit(c, aid, task['id'], 'proposed', None, 'proposed', ahash,
                        f"tool={tool}; risk={risk}; tainted={1 if tainted else 0}")
        self._notify(task['id'], 'action_pending', f"Eylem onayı bekleniyor: {tool}",
                     util.truncate(preview.replace('\n', ' '), 300) + (' [web içeriğinden etkilenmiş olabilir]' if tainted else ''), aid)
        return Proposal(aid, False, preview, 'proposed', bool(tainted), risk)

    # ------------------------------------------------------------------ onay / ret / iptal
    def approve(self, aid, args_hash, confirm_tainted=False, via=None):
        """Onayla ve calistir. Hash uyusmazligi, sure dolumu, kirlenme onaysizligi, kapali sistem: acik hata."""
        if not self.enabled():
            raise Conflict('Eylem sistemi kapalı (TASKS_ACTIONS_ENABLED=0); eylem onaylanamaz ve çalıştırılamaz.')
        if not isinstance(args_hash, str) or not args_hash.strip():
            raise ValidationFailed('Onay isteği eylemin args_hash değerini taşımalı.', 'args_hash')
        if not isinstance(confirm_tainted, bool):
            raise ValidationFailed('confirm_tainted true ya da false olmalı.', 'confirm_tainted')
        row = self._row(aid)
        now = self.clock()
        if row['status'] == 'proposed' and row['expires_at'] <= now:
            self._expire(row)
            raise Conflict('Bu eylemin onay süresi doldu; yeniden önerilmesi gerekir.')
        if row['status'] != 'proposed':
            raise InvalidTransition(f"Bu eylem onaylanamaz (durum: {STATUS_LABEL.get(row['status'], row['status'])}).")
        # bütünlük: saklanan argumanlar hash ile hala eslesmeli (DB'ye dis mudahale)
        if specs.args_hash_of(jload(row['args'], {})) != row['args_hash']:
            with self.db.tx() as c:
                self._audit(c, aid, row['task_id'], 'integrity_failed', 'proposed', 'proposed', row['args_hash'],
                            'saklanan argumanlar hash ile uyusmuyor')
            raise Conflict('Eylem kaydının bütünlüğü doğrulanamadı; onaylanmadı.')
        if args_hash.strip() != row['args_hash']:
            with self.db.tx() as c:
                self._audit(c, aid, row['task_id'], 'approval_hash_mismatch', 'proposed', 'proposed', row['args_hash'],
                            'onay hash eslesmedi')
            raise Conflict('Onayladığınız içerik eylemin güncel içeriğiyle eşleşmiyor (args_hash uyuşmuyor); onaylanmadı.')
        if row['tainted'] and not confirm_tainted:
            raise ValidationFailed('Bu eylem web içeriğinden etkilenmiş olabilir; onaylamak için confirm_tainted=true gönderin. '
                                   'Önce önizlemeyi dikkatle okuyun.', 'confirm_tainted')
        if not self.db.one('SELECT id FROM tasks WHERE id=?', (row['task_id'],)):
            raise Conflict('Eylemin ait olduğu görev artık yok; eylem onaylanamaz.')
        ok = self._transition(aid, 'proposed', 'approved', 'approved',
                              f"confirm_tainted={1 if confirm_tainted else 0}" + (f"; via={via}" if via in ('local', 'token') else ''),
                              sets={'decided_at': now},
                              extra_where='AND args_hash=? AND expires_at>?', extra_params=(row['args_hash'], now))
        if not ok:
            cur = self._row(aid)
            if cur['status'] == 'proposed':
                raise Conflict('Eylem durumu aynı anda değişti ya da süresi doldu; tekrar deneyin.')
            raise Conflict(f"Eylem aynı anda başka bir istekle işlendi (durum: {STATUS_LABEL.get(cur['status'], cur['status'])}).")
        return self.execute_approved(aid)

    def reject(self, aid):
        row = self._row(aid)
        if row['status'] != 'proposed':
            raise InvalidTransition(f"Bu eylem reddedilemez (durum: {STATUS_LABEL.get(row['status'], row['status'])}).")
        if not self._transition(aid, 'proposed', 'rejected', 'rejected', sets={'decided_at': self.clock()}):
            raise Conflict('Eylem durumu aynı anda değişti; tekrar deneyin.')
        return self.get(aid)

    def cancel(self, aid):
        row = self._row(aid)
        if row['status'] not in ('proposed', 'approved'):
            raise InvalidTransition(f"Bu eylem iptal edilemez (durum: {STATUS_LABEL.get(row['status'], row['status'])}).")
        if not self._transition(aid, row['status'], 'cancelled', 'cancelled', sets={'decided_at': self.clock()}):
            raise Conflict('Eylem durumu aynı anda değişti (çalışmaya başlamış olabilir); tekrar deneyin.')
        return self.get(aid)

    def _expire(self, row):
        self._transition(row['id'], row['status'], 'expired', 'expired', 'onay suresi doldu',
                         sets={'finished_at': self.clock()})

    # ------------------------------------------------------------------ calistirma (YALNIZCA sistem)
    def execute_approved(self, aid):
        """approved -> executing CAS'ini kazanan calistirir. Kazanamayan hicbir sey yapmaz. Donus: eylemin son hali."""
        row = self._row(aid)
        if row['status'] != 'approved':
            return self.get(aid)
        now = self.clock()
        if row['expires_at'] <= now:
            self._expire(row)
            return self.get(aid)
        if not self.enabled():
            return self.get(aid)               # kill switch: onayli ama baslamamis eylem beklemede kalir
        spec = specs.get(row['tool'])
        args = jload(row['args'], None)
        if spec is None or not isinstance(args, dict):
            self._finish(aid, row, 'failed', error='Eylem aracı artık tanımlı değil ya da argümanlar okunamadı.', started=now,
                         event='failed', frm='approved')
            return self.get(aid)
        if not self._transition(aid, 'approved', 'executing', 'execution_started', sets={'executed_at': now},
                                extra_where='AND expires_at>?', extra_params=(now,)):
            return self.get(aid)               # baska is parcacigi/surec kazandi (ya da sure doldu)
        t0 = time.monotonic()
        try:
            # TOCTOU: calistirma aninda girdi YENIDEN dogrulanir; sonuc onaylanan hash ile birebir ayni olmali
            renorm = spec.validate(args, self.env)
            if specs.args_hash_of(renorm) != row['args_hash'] or specs.args_hash_of(args) != row['args_hash']:
                raise ActionError('Argümanlar onaylanan içerikle eşleşmiyor; çalıştırılmadı.')
            result = str(spec.execute(renorm, self.env) or '')
            self._finish(aid, row, 'succeeded', result=result, t0=t0)
        except ActionError as e:
            self._finish(aid, row, 'failed', error=str(e), t0=t0)
        except Exception as e:  # noqa: BLE001 - eylem hatasi gorevi/uygulamayi cokertmez
            log.error('eylem %s calistirma hatasi: %s: %s', aid, type(e).__name__, util.redact(e))
            self._finish(aid, row, 'failed', error=f'Beklenmeyen hata ({type(e).__name__}); ayrıntılar günlüğe yazıldı.', t0=t0)
        return self.get(aid)

    def _finish(self, aid, row, to, result=None, error=None, t0=None, started=None, event=None, frm='executing'):
        dur = f'{(time.monotonic() - t0) * 1000:.0f}ms' if t0 is not None else ''
        sets = {'finished_at': self.clock(), 'result': (util.redact(result)[:2000] if result is not None else None),
                'error': (util.redact(error)[:500] if error else None)}
        try:
            if frm == 'approved':          # arac yok: executing'e hic gecmeden basarisiz (izin verilmeyen gecis degil; ozel yol)
                with self.db.tx() as c:
                    cur = c.execute("UPDATE task_actions SET status='failed', finished_at=?, error=?, executed_at=? "
                                    "WHERE id=? AND status='approved'", (sets['finished_at'], sets['error'], started, aid))
                    if cur.rowcount == 1:
                        self._audit(c, aid, row['task_id'], 'failed', 'approved', 'failed', row['args_hash'], error or '')
                return
            ok = self._transition(aid, 'executing', to, 'succeeded' if to == 'succeeded' else 'failed',
                                  f"{dur}; {(error or result or '')[:300]}", sets=sets)
            if not ok:
                log.warning('eylem %s sonucu yazilamadi: durum baska bir surec tarafindan degistirilmis', aid)
                return
        except Exception as e:  # noqa: BLE001 - DB kilitli vb.: eylem 'executing' kalir, kurtarma outcome_unknown yapar
            log.error('eylem %s sonucu yazilamadi: %s', aid, type(e).__name__)
            return
        if to == 'succeeded':
            self._remember(row, result)

    def _remember(self, row, result):
        """Tamamlanan eylem sonucunu, YALNIZCA projeye bagli gorevlerde, otomatik 'note' olarak yazar. Icerik tamamen sistemin
        urettigi metadir (arac, yol, bayt); model/web metni icermez. Hata eylemi bozmaz."""
        if self.memory is None or not config.ACTIONS_MEMORY_NOTE:
            return
        try:
            pid = self.memory.project_of_task(row['task_id'])
            if pid is None:
                return
            args = jload(row['args'], {})
            where = args.get('path') or args.get('filename') or ''
            title = f"Eylem tamamlandı: {row['tool']} #{row['id']}"
            body = f"Araç: {row['tool']}. Sonuç: {util.truncate(result or '', 300)}. Konum: {where}. (Otomatik kayıt, kullanıcı onaylı eylem)"
            self.memory.add_item('note', title, body, project_id=pid, task_id=row['task_id'], source='auto')
        except Exception as e:  # noqa: BLE001
            log.warning('eylem notu hafizaya yazilamadi (%s)', type(e).__name__)

    # ------------------------------------------------------------------ zamanlayici/aciliş bakimi
    def tick(self):
        """Her zamanlayici turunda: suresi dolanlari kapat, cokme sonrasi 'executing' kayitlarini coz, onayli ama baslamamislari calistir.
        Donus: islenen eylem sayisi. Asla istisna firlatmaz."""
        n = 0
        try:
            now = self.clock()
            for r in self.db.all("SELECT * FROM task_actions WHERE status IN ('proposed','approved') AND expires_at<=? LIMIT 50", (now,)):
                self._expire(r)
                n += 1
            stale = now - config.ACTIONS_EXEC_STALE_SECONDS
            for r in self.db.all("SELECT * FROM task_actions WHERE status='executing' AND executed_at<=? LIMIT 50", (stale,)):
                n += 1 if self._resolve_crashed(r) else 0
            if self.enabled():
                for r in self.db.all("SELECT id FROM task_actions WHERE status='approved' AND expires_at>? LIMIT 20", (now,)):
                    self.execute_approved(r['id'])
                    n += 1
        except Exception as e:  # noqa: BLE001
            log.error('eylem bakim hatasi: %s: %s', type(e).__name__, util.redact(e))
        return n

    def _resolve_crashed(self, row):
        """'executing' iken surec olmus: ASLA yeniden calistirma. Idempotent+dogrulanabilir ise dogrula, degilse outcome_unknown."""
        aid = row['id']
        spec = specs.get(row['tool'])
        verified = None
        if spec is not None and spec.idempotent and spec.verify is not None:
            try:
                renorm = spec.validate(jload(row['args'], {}), self.env)
                if specs.args_hash_of(renorm) == row['args_hash']:
                    verified = spec.verify(renorm, self.env)
            except Exception:  # noqa: BLE001
                verified = None
        if verified is True:
            ok = self._transition(aid, 'executing', 'succeeded', 'recovered_verified',
                                  'surec cokmesi sonrasi dosya dogrulandi; yeniden calistirilmadi',
                                  sets={'finished_at': self.clock(), 'result': 'Çökme sonrası doğrulandı: eylemin etkisi mevcut (yeniden çalıştırılmadı).'})
            if ok:
                self._remember(row, 'Çökme sonrası doğrulandı')
            return ok
        ok = self._transition(aid, 'executing', 'outcome_unknown', 'outcome_unknown',
                              'surec cokmesi: sonuc dogrulanamadi; OTOMATIK YENIDEN CALISTIRILMADI',
                              sets={'finished_at': self.clock(),
                                    'error': 'Çalışma sırasında süreç kesildi; eylemin yapılıp yapılmadığı doğrulanamadı. '
                                             'Otomatik tekrar çalıştırılmadı; sonucu elle kontrol edin.'})
        if ok:
            self._notify(row['task_id'], 'action_unknown', f"Eylem sonucu belirsiz: {row['tool']}",
                         'Çalışma sırasında kesinti oldu; eylem otomatik tekrarlanmadı. Çalışma klasörünü kontrol edin.', aid)
        return ok

    # ------------------------------------------------------------------ okuma
    def public(self, r, detail=False):
        args = jload(r['args'], {})
        spec = specs.get(r['tool'])
        out = {'id': r['id'], 'task_id': r['task_id'], 'run_id': r['run_id'], 'tool': r['tool'], 'status': r['status'],
               'status_label': STATUS_LABEL.get(r['status'], r['status']), 'risk': r['risk'], 'tainted': bool(r['tainted']),
               'requires_confirm_tainted': bool(r['tainted']) and r['status'] == 'proposed',
               'taint_warning': TAINT_WARNING if r['tainted'] else None, 'args_hash': r['args_hash'], 'preview': r['preview'],
               'result': r['result'], 'error': r['error'], 'idempotent': bool(spec.idempotent) if spec else None,
               'created_at': _iso(r['created_at']), 'decided_at': _iso(r['decided_at']), 'executed_at': _iso(r['executed_at']),
               'finished_at': _iso(r['finished_at']), 'expires_at': _iso(r['expires_at'])}
        if detail:
            out['args'] = args
            out['audit'] = self.audit(r['id'])
        return out

    def get(self, aid):
        return self.public(self._row(aid), detail=True)

    def list(self, status=None, task_id=None, limit=50):
        where, params = [], []
        if status:
            if status not in STATUSES:
                raise ValidationFailed('Geçersiz durum.', 'status')
            where.append('status=?')
            params.append(status)
        if task_id is not None:
            where.append('task_id=?')
            params.append(int(task_id))
        sql = 'SELECT * FROM task_actions' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY id DESC LIMIT ?'
        return [self.public(r) for r in self.db.all(sql, tuple(params) + (max(1, min(200, int(limit))),))]

    def audit(self, aid=None, limit=200):
        if aid is None:
            rows = self.db.all('SELECT * FROM action_audit ORDER BY id DESC LIMIT ?', (max(1, min(500, int(limit))),))
        else:
            rows = self.db.all('SELECT * FROM action_audit WHERE action_id=? ORDER BY id LIMIT ?', (int(aid), max(1, min(500, int(limit)))))
        return [{'id': r['id'], 'action_id': r['action_id'], 'event': r['event'], 'status_from': r['status_from'],
                 'status_to': r['status_to'], 'detail': r['detail'], 'created_at': _iso(r['created_at'])} for r in rows]

    def for_run(self, run_id):
        """Bir calistirmanin eylem kayitlari (rapor/denetci icin). Sade sozluk listesi."""
        return self.db.all('SELECT id, tool, status, preview, tainted FROM task_actions WHERE run_id=? ORDER BY id', (run_id,))

    def stats(self):
        r = self.db.all('SELECT status, COUNT(*) AS n FROM task_actions GROUP BY status')
        return {x['status']: x['n'] for x in r}


def _iso(epoch):
    if epoch is None:
        return None
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(float(epoch)))
