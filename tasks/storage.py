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
import weakref

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

# v2: ajan modu icin tur kayitlari (her tur: karar + arac cagrisi + sonuc). Mevcut tablolara dokunmaz.
SCHEMA_V2 = [
    """CREATE TABLE IF NOT EXISTS task_agent_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_id INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
        turn_no INTEGER NOT NULL,
        thought TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL,
        args TEXT NOT NULL DEFAULT '{}',
        observation TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'ok',
        flagged INTEGER NOT NULL DEFAULT 0,
        started_at REAL NOT NULL,
        finished_at REAL,
        UNIQUE(run_id, turn_no)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_turns_run ON task_agent_turns(run_id, turn_no)",
]


# v3: kalici hafiza (projeler, hafiza ogeleri, gorev-proje bagi). Mevcut tablolara DOKUNMAZ. Tam metin arama indeksi
# (memory_fts) goc icinde DEGIL, memory.py icinde calisma aninda kurulur: SQLite'ta FTS5 yoksa goc patlamaz.
SCHEMA_V3 = [
    """CREATE TABLE IF NOT EXISTS memory_projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS memory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('decision','document','note','result')),
        project_id INTEGER REFERENCES memory_projects(id) ON DELETE SET NULL,
        task_id INTEGER,
        run_id INTEGER,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '[]',
        source TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user','auto')),
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded')),
        supersedes_id INTEGER REFERENCES memory_items(id) ON DELETE SET NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mem_project ON memory_items(project_id, kind, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mem_task ON memory_items(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_mem_source ON memory_items(source, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_auto_run ON memory_items(run_id) WHERE source='auto' AND run_id IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS memory_task_projects (
        task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        project_id INTEGER NOT NULL REFERENCES memory_projects(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mtp_project ON memory_task_projects(project_id)",
]

# v4: eylem yetenegi. YALNIZCA yeni duz tablolar (+ trigger'lar); mevcut tablolara dokunmaz. task_id/run_id bilerek FK DEGIL:
# gorev silinse ya da saklama politikasi calistirmalari temizlese de eylem kaydi ve denetim izi korunur.
SCHEMA_V4 = [
    """CREATE TABLE IF NOT EXISTS task_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        run_id INTEGER,
        tool TEXT NOT NULL,
        args TEXT NOT NULL,
        args_hash TEXT NOT NULL,
        preview TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('proposed','approved','rejected','executing','succeeded','failed',
                                               'outcome_unknown','expired','cancelled')),
        risk TEXT NOT NULL DEFAULT 'low' CHECK (risk IN ('low','medium','high')),
        tainted INTEGER NOT NULL DEFAULT 0 CHECK (tainted IN (0,1)),
        idempotency_key TEXT NOT NULL UNIQUE,
        result TEXT,
        error TEXT,
        created_at REAL NOT NULL,
        decided_at REAL,
        executed_at REAL,
        finished_at REAL,
        expires_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_actions_status ON task_actions(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_actions_task ON task_actions(task_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_actions_run ON task_actions(run_id)",
    """CREATE TABLE IF NOT EXISTS action_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action_id INTEGER NOT NULL REFERENCES task_actions(id),
        task_id INTEGER NOT NULL,
        event TEXT NOT NULL,
        status_from TEXT,
        status_to TEXT,
        args_hash TEXT,
        detail TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_audit_action ON action_audit(action_id, id)",
    # denetim kaydi yalnizca eklenir: DB seviyesinde guncelleme ve silme engellenir
    """CREATE TRIGGER IF NOT EXISTS trg_action_audit_no_update BEFORE UPDATE ON action_audit
       BEGIN SELECT RAISE(ABORT, 'action_audit degistirilemez'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_action_audit_no_delete BEFORE DELETE ON action_audit
       BEGIN SELECT RAISE(ABORT, 'action_audit silinemez'); END""",
    # onaylanan argumanlar ve kimlik alanlari onerinin ardindan degistirilemez
    """CREATE TRIGGER IF NOT EXISTS trg_task_actions_immutable
       BEFORE UPDATE OF task_id, run_id, tool, args, args_hash, idempotency_key, created_at, tainted, preview, risk
       ON task_actions
       BEGIN SELECT RAISE(ABORT, 'task_actions degistirilemez alanlar'); END""",
]

# (surum, [sql, ...]) - yeni surum eklerken listeye EKLE, eskileri DEGISTIRME.
# v5: otonom orkestrasyon (hedefler, alt gorev dugumleri, denetim olaylari). Mevcut tablolara DOKUNMAZ.
# task_id/goal_id baglari bilerek yabanci anahtar DEGILDIR (saklama politikasi gorevleri silse de hedef sonuclari kalir);
# goal_events yalnizca eklenir (DB seviyesinde guncelleme ve silme engelli).
SCHEMA_V5 = [
    """CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('planning','awaiting_approval','running','completed','failed','cancelled')),
        options TEXT NOT NULL DEFAULT '{}',
        plan TEXT,
        plan_hash TEXT,
        result TEXT,
        error TEXT,
        retries INTEGER NOT NULL DEFAULT 0,
        plan_attempts INTEGER NOT NULL DEFAULT 0,
        claimed_until REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        approved_at REAL,
        finished_at REAL,
        deadline_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status, id)",
    """CREATE TABLE IF NOT EXISTS goal_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        depends_on TEXT NOT NULL DEFAULT '[]',
        internet INTEGER NOT NULL DEFAULT 0 CHECK (internet IN (0,1)),
        agent INTEGER NOT NULL DEFAULT 0 CHECK (agent IN (0,1)),
        is_final INTEGER NOT NULL DEFAULT 0 CHECK (is_final IN (0,1)),
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','launching','running','succeeded','failed','skipped','cancelled')),
        task_id INTEGER,
        launch_at REAL,
        result TEXT,
        error TEXT,
        created_at REAL NOT NULL,
        finished_at REAL,
        UNIQUE (goal_id, key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_gnodes_goal ON goal_nodes(goal_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_gnodes_task ON goal_nodes(task_id)",
    """CREATE TABLE IF NOT EXISTS goal_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id INTEGER NOT NULL,
        node_id INTEGER,
        event TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_gevents_goal ON goal_events(goal_id, id)",
    """CREATE TRIGGER IF NOT EXISTS trg_goal_events_no_update BEFORE UPDATE ON goal_events
       BEGIN SELECT RAISE(ABORT, 'goal_events degistirilemez'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_goal_events_no_delete BEFORE DELETE ON goal_events
       BEGIN SELECT RAISE(ABORT, 'goal_events silinemez'); END""",
]

MIGRATIONS = [(1, SCHEMA_V1), (2, SCHEMA_V2), (3, SCHEMA_V3), (4, SCHEMA_V4), (5, SCHEMA_V5)]
LATEST_VERSION = MIGRATIONS[-1][0]


class _Holder:
    """Bir iş parçacığının bağlantısını tutar. İş parçacığı ölünce thread-local boşalır, Holder yok edilir ve bağlantı
    KAPANIR (__del__). Böylece Flask'ın her istek için açtığı kısa ömürlü thread'ler dosya tanıtıcısı sızdırmaz."""
    __slots__ = ('conn', '__weakref__')

    def __init__(self, conn):
        self.conn = conn

    def close(self):
        c, self.conn = self.conn, None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def __del__(self):
        self.close()


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._all = weakref.WeakSet()      # yalnızca izleme: güçlü referans TUTMAZ (sızıntıyı önler)
        self.migrated = False

    # ---------------- baglanti ----------------
    def conn(self):
        h = getattr(self._local, 'h', None)
        if h is None or h.conn is None:
            h = _Holder(self._connect())
            self._local.h = h
            self._local.depth = 0
            self._all.add(h)
        return h.conn

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

    def close_thread(self):
        """Bu iş parçacığının bağlantısını hemen kapatır (kısa ömürlü thread'ler bitmeden önce çağırır)."""
        h = getattr(self._local, 'h', None)
        if h is not None:
            h.close()
            self._local.h = None
            self._local.depth = 0

    def close_all(self):
        for h in list(self._all):
            h.close()
        self._all = weakref.WeakSet()
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
