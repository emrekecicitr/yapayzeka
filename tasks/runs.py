"""Task History + adim kalicigi: task_runs ve task_steps tablolari icin islemler.

Adim ciktilari her adim biter bitmez kalici yazilir; bir calistirma yarida kalirsa
(hata, kapanma, duraklatma) sonraki deneme TAMAMLANMIS adimlari atlayip kaldigi yerden devam eder.
"""
from . import config
from .storage import jdump, jload

RUN_STATUSES = ('running', 'succeeded', 'failed', 'cancelled', 'paused', 'interrupted', 'retry_wait')
STEP_STATUSES = ('pending', 'running', 'completed', 'failed', 'skipped')


class Runs:
    def __init__(self, db, clock):
        self.db = db
        self.clock = clock

    # ---------------- calistirmalar ----------------
    def get_run(self, run_id):
        return self.db.one('SELECT * FROM task_runs WHERE id=?', (run_id,))

    def list_runs(self, task_id, limit=20):
        rows = self.db.all(
            'SELECT id, task_id, run_no, trigger, status, attempt, started_at, finished_at, error, model_used, '
            'late_seconds, substr(result, 1, 240) AS result_preview, '
            '(result IS NOT NULL AND result != \'\') AS has_result '
            'FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT ?', (task_id, int(limit)))
        return rows

    def run_with_steps(self, task_id, run_id):
        run = self.db.one('SELECT * FROM task_runs WHERE id=? AND task_id=?', (run_id, task_id))
        if not run:
            return None
        run['sources'] = jload(run.get('sources'), [])
        run['steps'] = self.steps(run_id)
        return run

    def latest_run_id(self, task_id):
        r = self.db.one('SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,))
        return r['id'] if r else None

    def set_model_used(self, run_id, model):
        if model:
            self.db.execute('UPDATE task_runs SET model_used=? WHERE id=?', (str(model)[:80], run_id))

    def save_sources(self, run_id, sources):
        self.db.execute('UPDATE task_runs SET sources=? WHERE id=?', (jdump(sources or []), run_id))

    def prune(self, task_id):
        """Bir gorevin en eski calistirmalarini sinira kadar siler (adimlar CASCADE ile gider)."""
        self.db.execute(
            'DELETE FROM task_runs WHERE task_id=? AND id NOT IN '
            '(SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT ?)',
            (task_id, task_id, config.MAX_RUNS_PER_TASK))

    # ---------------- adimlar ----------------
    def steps(self, run_id):
        rows = self.db.all('SELECT * FROM task_steps WHERE run_id=? ORDER BY step_no', (run_id,))
        for r in rows:
            r['spec'] = jload(r.get('spec'), {})
        return rows

    def create_steps(self, task_id, run_id, specs):
        """specs: [{'kind','name', ...}] -> task_steps (zaten varsa dokunmaz)."""
        now = self.clock()
        with self.db.tx() as c:
            have = c.execute('SELECT COUNT(*) FROM task_steps WHERE run_id=?', (run_id,)).fetchone()[0]
            if have:
                return
            for i, sp in enumerate(specs, start=1):
                spec = {k: v for k, v in sp.items() if k not in ('kind', 'name')}
                c.execute(
                    'INSERT INTO task_steps(task_id, run_id, step_no, kind, name, status, spec, started_at) '
                    'VALUES(?,?,?,?,?,?,?,NULL)',
                    (task_id, run_id, i, sp['kind'], str(sp['name'])[:120], 'pending', jdump(spec)))
        return now

    def reset_interrupted_steps(self, run_id):
        """Calisirken yarim kalmis adimlari (cikti yazilmamis) yeniden calistirilmak uzere bekleyene alir."""
        self.db.execute(
            "UPDATE task_steps SET status='pending' WHERE run_id=? AND status IN ('running','failed')", (run_id,))

    def step_start(self, step_id):
        self.db.execute(
            "UPDATE task_steps SET status='running', started_at=?, attempts=attempts+1, error=NULL WHERE id=?",
            (self.clock(), step_id))

    def step_done(self, step_id, output):
        self.db.execute(
            "UPDATE task_steps SET status='completed', output=?, error=NULL, finished_at=? WHERE id=?",
            (output if output is None else str(output)[:config.MAX_RESULT_CHARS], self.clock(), step_id))

    def step_skip(self, step_id, note=''):
        self.db.execute(
            "UPDATE task_steps SET status='skipped', output=?, finished_at=? WHERE id=?",
            (note, self.clock(), step_id))

    def step_fail(self, step_id, error):
        self.db.execute(
            "UPDATE task_steps SET status='failed', error=?, finished_at=? WHERE id=?",
            (str(error)[:500], self.clock(), step_id))

    def step_update_spec(self, step_id, spec):
        self.db.execute('UPDATE task_steps SET spec=? WHERE id=?', (jdump(spec), step_id))

    def append_steps(self, task_id, run_id, specs):
        """Plan adimlari uretildikten sonra mevcut listeye (planlama adimindan sonra) ekler."""
        with self.db.tx() as c:
            last = c.execute('SELECT COALESCE(MAX(step_no),0) FROM task_steps WHERE run_id=?', (run_id,)).fetchone()[0]
            for i, sp in enumerate(specs, start=last + 1):
                spec = {k: v for k, v in sp.items() if k not in ('kind', 'name')}
                c.execute(
                    'INSERT INTO task_steps(task_id, run_id, step_no, kind, name, status, spec) VALUES(?,?,?,?,?,?,?)',
                    (task_id, run_id, i, sp['kind'], str(sp['name'])[:120], 'pending', jdump(spec)))

    def progress(self, run_id):
        """Gercek adim durumuna dayali ilerleme yuzdesi (0-100)."""
        r = self.db.one(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN status IN ('completed','skipped') THEN 1 ELSE 0 END) AS d "
            'FROM task_steps WHERE run_id=?', (run_id,))
        n = (r or {}).get('n') or 0
        d = (r or {}).get('d') or 0
        if n == 0:
            return 0
        return int(d * 100 / n)
