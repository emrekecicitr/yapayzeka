"""Task Storage: SQLite tabanli kalici depo + guvenli sema guncelleme (migration).

Notlar
- Her thread kendi baglantisini kullanir (thread-local). WAL modu, okuyucular yazari bloklamaz.
- Tum SQL parametrelidir (SQL injection korumasi). Dinamik SQL yalnizca sabit kolon adlariyla kurulur.
- Zamanlar UTC epoch saniyesi (REAL) olarak saklanir: yaz/kis saati ve saat dilimi
  karmasasindan bagimsiz karsilastirma ve indeksleme saglar.
- Migration: PRAGMA user_version ile surumlenir; her surum tek islemde uygulanir, basarisizsa geri alinir.
  Mevcut (surum > 0) bir veritabani guncellenmeden once yanina otomatik yedek alinir.
- Bu veritabani mevcut kullanici verilerinden (sohbet, hafiza, ayarlar, API anahtarlari) tamamen bagimsizdir.
"""
import contextlib
import json
import os
import sqlite3
import threading

SCHEMA_V1 = [
    """CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        task_type TEXT NOT NULL,
        status TEXT NOT NULL,
        schedule_config TEXT NOT NULL DEFAULT '{}',
        options TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT '{}',
        timezone TEXT NOT NULL DEFAULT 'Europe/Istanbul',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        next_run_at REAL,
        last_run_at REAL,
        last_success_at REAL,
        execution_count INTEGER NOT NULL DEFAULT 0,
        retry_count INTEGER NOT NULL DEFAULT 0,
        selected_model TEXT,
        internet_search_enabled INTEGER NOT NULL DEFAULT 0,
        task_priority INTEGER NOT NULL DEFAULT 2,
        approval_required INTEGER NOT NULL DEFAULT 0,
        approved INTEGER NOT NULL DEFAULT 0,
        current_step TEXT,
        progress INTEGER NOT NULL DEFAULT 0,
        result TEXT,
        last_error TEXT,
        last_run_ok INTEGER,
        run_trigger TEXT,
        resume_run_id INTEGER,
        lease_owner TEXT,
        lease_until REAL,
        pause_requested INTEGER NOT NULL DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, next_run_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(status, lease_until)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at)",
    """CREATE TABLE IF NOT EXISTS task_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_no INTEGER NOT NULL,
        trigger TEXT NOT NULL DEFAULT 'schedule',
        status TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 1,
        started_at REAL NOT NULL,
        finished_at REAL,
        result TEXT,
        error TEXT,
        sources TEXT NOT NULL DEFAULT '[]',
        model_used TEXT,
        late_seconds REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id, id DESC)",
    """CREATE TABLE IF NOT EXISTS task_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_id INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
        step_no INTEGER NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        spec TEXT NOT NULL DEFAULT '{}',
        output TEXT,
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        started_at REAL,
        finished_at REAL,
        UNIQUE(run_id, step_no)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_steps_task ON task_steps(task_id, run_id, step_no)",
    """CREATE TABLE IF NOT EXISTS task_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_id INTEGER,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        dedupe_key TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL,
        read_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_notif_unread ON task_notifications(read_at, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS task_cache (
        cache_key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        expires_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cache_exp ON task_cache(expires_at)",
    """CREATE TABLE IF NOT EXISTS task_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
]

# (surum, [sql, ...]) - yeni surum eklerken listeye EKLE, eskileri DEGISTIRME.
MIGRATIONS = [(1, SCHEMA_V1)]
LATEST_VERSION = MIGRATIONS[-1][0]


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._all = []
        self._all_lock = threading.Lock()
        self.migrated = False

    # ---------------- baglanti ----------------
    def conn(self):
        c = getattr(self._local, 'c', None)
        if c is None:
            c = self._connect()
            self._local.c = c
            self._local.depth = 0
            with self._all_lock:
                self._all.append(c)
        return c

    def _connect(self):
        directory = os.path.dirname(self.path) or '.'
        os.makedirs(directory, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA busy_timeout=8000')
        c.execute('PRAGMA foreign_keys=ON')
        try:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA synchronous=NORMAL')
        except sqlite3.DatabaseError:
            pass
        return c

    def close_all(self):
        with self._all_lock:
            conns, self._all = self._all, []
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        self._local = threading.local()

    # ---------------- islem ----------------
    @contextlib.contextmanager
    def tx(self):
        """Yazma islemi (BEGIN IMMEDIATE). Ic ice cagrilarda yalnizca en dis islem commit eder."""
        c = self.conn()
        depth = getattr(self._local, 'depth', 0)
        if depth == 0:
            c.execute('BEGIN IMMEDIATE')
        self._local.depth = depth + 1
        try:
            yield c
        except BaseException:
            self._local.depth -= 1
            if self._local.depth == 0:
                try:
                    c.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
            raise
        else:
            self._local.depth -= 1
            if self._local.depth == 0:
                c.execute('COMMIT')

    # ---------------- kisayollar ----------------
    def execute(self, sql, params=()):
        return self.conn().execute(sql, params)

    def one(self, sql, params=()):
        r = self.conn().execute(sql, params).fetchone()
        return dict(r) if r is not None else None

    def all(self, sql, params=()):
        return [dict(r) for r in self.conn().execute(sql, params).fetchall()]

    # ---------------- migration ----------------
    def migrate(self):
        with self._init_lock:
            if self.migrated:
                return
            c = self.conn()
            cur = c.execute('PRAGMA user_version').fetchone()[0]
            if cur > LATEST_VERSION:
                raise RuntimeError(
                    f'Gorev veritabani ({cur}) koddan daha yeni ({LATEST_VERSION}); uygulama guncel degil.')
            if cur == LATEST_VERSION:
                self.migrated = True
                return
            if cur > 0 and os.path.exists(self.path):
                self._backup(cur)
            for ver, stmts in MIGRATIONS:
                if ver <= cur:
                    continue
                c.execute('BEGIN IMMEDIATE')
                try:
                    # Baska bir surec ayni anda gecis yapmis olabilir: kilit altinda tekrar oku.
                    now_ver = c.execute('PRAGMA user_version').fetchone()[0]
                    if now_ver < ver:
                        for s in stmts:
                            c.execute(s)
                        c.execute(f'PRAGMA user_version={int(ver)}')
                    c.execute('COMMIT')
                except BaseException:
                    try:
                        c.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise
            self.migrated = True

    def _backup(self, version):
        bak = f'{self.path}.bak-v{version}'
        if os.path.exists(bak):
            return
        try:
            dst = sqlite3.connect(bak)
            try:
                self.conn().backup(dst)
            finally:
                dst.close()
        except Exception as e:
            # Yedek alinamazsa gecisi durdurmak yerine uyar: sema degisiklikleri toplamsal ve IF NOT EXISTS'lidir.
            print(f'[gorevler] migration yedegi alinamadi: {type(e).__name__}: {e}')


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def jload(text, default=None):
    if text is None or text == '':
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
