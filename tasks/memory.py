"""Kalici hafiza (Persistent Intelligence): projeler, kararlar, belgeler, notlar ve gorev sonuclari.

Amac: gorevler arasi, uzun vadeli baglam. Ajan onceki is ve kararlari `memory_search` araciyla, kullanici API ile okur/yazar.

Tasarim ilkeleri
- Mevcut sisteme DOKUNMAZ: yalnizca yeni tablolar (storage.SCHEMA_V3) ve bu modul. tasks tablosu degismez; gorev-proje bagi
  ayri bir tabloda (memory_task_projects) tutulur.
- Hafiza gorevlerden BAGIMSIZ yasar: gorev silinse veya saklama politikasi onu temizlese de hafiza ogeleri kalir
  (memory_items.task_id/run_id bilerek yabanci anahtar DEGILDIR).
- Arama: SQLite FTS5 (bm25, baslik/etiket/govde agirliklari). FTS5 yoksa ayni arayuzle LIKE tabanli yedek calisir.
  Metin ve sorgu Turkce icin katlanir (ı/İ/ş/ğ/ü/ö/ç -> ASCII), boylece "calisma" ve "çalışma" ayni seydir; 3+ harfli
  sorgu kelimeleri onek eslesir ("karar" -> "kararlar", "kararı"). Kok bulma (stemming) YOKTUR.
- Arama indeksi (memory_fts) goc icinde degil, calisma aninda kurulur; bozulursa/eksikse kendiliginden yeniden insa edilir.
- Hafiza icerigi (ozellikle otomatik kayitlar web kaynakli olabilir) modele HER ZAMAN guvenilmeyen veri olarak verilir (tools.py).
- Tum SQL parametrelidir; tum yazmalar tek islemde (db.tx) yapilir ve indeksle birlikte commit/rollback olur.
- Her sinir (proje/oge sayisi, metin uzunluklari, etiket sayisi) acik hata ile uygulanir; sessiz kesme yalnizca otomatik kayitta
  (sonuc metni) ve ucuncu tarafa gosterilen ozetlerde vardir.
"""
import logging
import re
import sqlite3
import time
import unicodedata

from . import config, util
from .manager import Conflict, NotFound, ValidationFailed, _iso
from .storage import jdump, jload

log = logging.getLogger('tasks')

KINDS = ('decision', 'document', 'note', 'result')
KIND_LABEL = {'decision': 'Karar', 'document': 'Belge', 'note': 'Not', 'result': 'Görev sonucu'}
# Kullanicinin/modelin yazabilecegi Turkce/Ingilizce takma adlar.
KIND_ALIASES = {'karar': 'decision', 'kararlar': 'decision', 'belge': 'document', 'dokuman': 'document',
                'doküman': 'document', 'not': 'note', 'notlar': 'note', 'sonuc': 'result', 'sonuç': 'result',
                'gorev_sonucu': 'result', 'görev_sonucu': 'result', 'decisions': 'decision',
                'documents': 'document', 'notes': 'note', 'results': 'result'}
PROJECT_STATUSES = ('active', 'archived')
USER_KINDS = ('decision', 'document', 'note')          # API ile elle yazilabilen turler ('result' otomatik/remember ile)

_CTRL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_WORD = re.compile(r'[^\W_]+', re.UNICODE)
_STOP = frozenset(('ve', 'ile', 'bir', 'bu', 'su', 'şu', 'o', 'de', 'da', 'ki', 'mi', 'mu', 'mü', 'mı', 'icin', 'için',
                   'gibi', 'ama', 'veya', 'ya', 'the', 'a', 'an', 'of', 'and', 'or', 'to', 'in', 'is'))
_NO_RESULT = 'Hafızada eşleşen kayıt yok.'
_FTS_DDL = ("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(title, tags, body, "
            "tokenize='unicode61 remove_diacritics 2')")
_PLAIN_DDL = 'CREATE TABLE IF NOT EXISTS memory_fts(title TEXT, tags TEXT, body TEXT)'

_TR = {'İ': 'i', 'I': 'i', 'ı': 'i', 'Ş': 's', 'ş': 's', 'Ğ': 'g', 'ğ': 'g', 'Ü': 'u', 'ü': 'u', 'Ö': 'o',
       'ö': 'o', 'Ç': 'c', 'ç': 'c'}


def _fold_char(c):
    m = _TR.get(c)
    if m is not None:
        return m
    d = ''.join(x for x in unicodedata.normalize('NFKD', c) if not unicodedata.combining(x)).lower()
    return d if len(d) == 1 else c.lower() if len(c.lower()) == 1 else c


def fold(text):
    """Turkce'ye duyarli katlama. UZUNLUK KORUNUR (len(fold(s)) == len(s)), boylece konumlar orijinal metne tasinabilir."""
    return ''.join(_fold_char(c) for c in str(text or ''))


def query_tokens(query, limit=12):
    """Sorgudan aranabilir (katlanmis) kelimeler. Kullanisiz/gurultulu kelimeler atilir; bos kalirsa hepsi korunur."""
    words = []
    for w in _WORD.findall(fold(query)):
        if w not in words:
            words.append(w)
    keep = [w for w in words if w not in _STOP and len(w) >= 2]
    return (keep or [w for w in words if len(w) >= 1])[:limit]


def _fts_query(tokens, mode):
    parts = []
    for t in tokens:
        parts.append('"%s"*' % t if len(t) >= 3 else '"%s"' % t)
    return (' ' if mode == 'and' else ' OR ').join(parts)


# ---------------------------------------------------------------- girdi temizligi
def _line(v, max_len, label, required=True):
    if v is None:
        v = ''
    if not isinstance(v, str):
        raise ValidationFailed(f'{label} metin olmalı.', label)
    s = re.sub(r'\s+', ' ', _CTRL.sub('', v)).strip()
    if required and not s:
        raise ValidationFailed(f'{label} boş olamaz.', label)
    if len(s) > max_len:
        raise ValidationFailed(f'{label} en fazla {max_len} karakter olabilir.', label)
    return s


def _text(v, max_len, label):
    if v is None:
        v = ''
    if not isinstance(v, str):
        raise ValidationFailed(f'{label} metin olmalı.', label)
    s = _CTRL.sub('', v).replace('\r\n', '\n').replace('\r', '\n').strip()
    if len(s) > max_len:
        raise ValidationFailed(f'{label} en fazla {max_len} karakter olabilir.', label)
    return s


def _tags(v):
    if v is None:
        return []
    if isinstance(v, str):
        v = v.split(',')
    if not isinstance(v, (list, tuple)):
        raise ValidationFailed('Etiketler liste veya virgülle ayrılmış metin olmalı.', 'tags')
    out, seen = [], set()
    for t in v:
        if not isinstance(t, str):
            raise ValidationFailed('Her etiket metin olmalı.', 'tags')
        s = re.sub(r'\s+', ' ', _CTRL.sub('', t)).strip()
        if not s:
            continue
        if len(s) > config.MEMORY_TAG_CHARS:
            raise ValidationFailed(f'Etiket en fazla {config.MEMORY_TAG_CHARS} karakter olabilir.', 'tags')
        k = fold(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    if len(out) > config.MEMORY_TAGS_MAX:
        raise ValidationFailed(f'En fazla {config.MEMORY_TAGS_MAX} etiket kullanılabilir.', 'tags')
    return out


def _kind(v, allowed=KINDS, field='kind'):
    if not isinstance(v, str):
        raise ValidationFailed('Tür seçilmeli.', field)
    k = v.strip().lower()
    k = KIND_ALIASES.get(k, k)
    if k not in allowed:
        raise ValidationFailed('Geçersiz tür. Geçerli türler: ' + ', '.join(allowed) + '.', field)
    return k


def normalize_kind(v):
    """Arac/arama icin yumusak donusum: gecersizse None (arama yok saymaz, ama hata da firlatmaz)."""
    if not isinstance(v, str):
        return None
    k = v.strip().lower()
    k = KIND_ALIASES.get(k, k)
    return k if k in KINDS else None


def _int_id(v, field):
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValidationFailed('Geçersiz kimlik.', field)
    return v


class Memory:
    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock
        self.fts = False
        self._ensure_index()

    # ================================================================== indeks
    def _ensure_index(self):
        db = self.db
        existing = db.one("SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'")
        created = False
        if existing is None:
            try:
                with db.tx() as c:
                    c.execute(_FTS_DDL)
            except sqlite3.OperationalError:          # FTS5 derlenmemis: ayni arayuzlu duz tablo
                with db.tx() as c:
                    c.execute(_PLAIN_DDL)
            created = True
        row = db.one("SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'")
        self.fts = bool(row and 'VIRTUAL' in (row['sql'] or '').upper())
        n_items = db.one('SELECT COUNT(*) AS n FROM memory_items')['n']
        n_idx = db.one('SELECT COUNT(*) AS n FROM memory_fts')['n']
        if created or n_items != n_idx:
            self.rebuild_index()

    def rebuild_index(self):
        """Indeksi ogelerden bastan kurar (onarim). Donus: indekslenen oge sayisi."""
        with self.db.tx() as c:
            c.execute('DELETE FROM memory_fts')
            rows = c.execute('SELECT id, title, tags, body FROM memory_items').fetchall()
            for r in rows:
                self._index_put(c, r['id'], r['title'], jload(r['tags'], []) or [], r['body'])
        return len(rows)

    @staticmethod
    def _index_put(c, item_id, title, tags, body):
        c.execute('DELETE FROM memory_fts WHERE rowid=?', (item_id,))
        c.execute('INSERT INTO memory_fts(rowid, title, tags, body) VALUES(?,?,?,?)',
                  (item_id, fold(title), fold(' '.join(tags)), fold(body)))

    # ================================================================== projeler
    @staticmethod
    def _project_public(r):
        return {'id': r['id'], 'name': r['name'], 'description': r['description'], 'status': r['status'],
                'items': r.get('items', 0), 'tasks': r.get('tasks', 0),
                'created_at': _iso(r['created_at']), 'updated_at': _iso(r['updated_at'])}

    _PROJECT_SQL = ('SELECT p.*, (SELECT COUNT(*) FROM memory_items i WHERE i.project_id=p.id) AS items, '
                    '(SELECT COUNT(*) FROM memory_task_projects t WHERE t.project_id=p.id) AS tasks FROM memory_projects p')

    def create_project(self, name, description='', status='active'):
        name = _line(name, config.MEMORY_NAME_CHARS, 'name')
        description = _text(description, 2000, 'description')
        if status not in PROJECT_STATUSES:
            raise ValidationFailed('Geçersiz proje durumu.', 'status')
        now = self.clock()
        with self.db.tx() as c:
            if c.execute('SELECT COUNT(*) FROM memory_projects').fetchone()[0] >= config.MEMORY_MAX_PROJECTS:
                raise Conflict(f'En fazla {config.MEMORY_MAX_PROJECTS} proje oluşturulabilir.')
            self._check_name_free(c, name)
            try:
                cur = c.execute('INSERT INTO memory_projects(name, description, status, created_at, updated_at) '
                                'VALUES(?,?,?,?,?)', (name, description, status, now, now))
            except sqlite3.IntegrityError:
                raise Conflict('Bu adla bir proje zaten var.', 'name')
            pid = cur.lastrowid
        return self.get_project(pid)

    def get_project(self, pid):
        r = self.db.one(self._PROJECT_SQL + ' WHERE p.id=?', (pid,))
        if not r:
            raise NotFound('Proje bulunamadı.')
        return self._project_public(r)

    def find_project(self, name):
        """Ada gore proje; buyuk/kucuk harf ve Turkce karakter farki (Çalışma/calisma) onemsizdir. Yoksa None."""
        if not isinstance(name, str) or not name.strip():
            return None
        key = fold(re.sub(r'\s+', ' ', name).strip())
        for r in self.db.all(self._PROJECT_SQL):
            if fold(r['name']) == key:
                return self._project_public(r)
        return None

    @staticmethod
    def _check_name_free(c, name, exclude_id=None):
        key = fold(name)
        for r in c.execute('SELECT id, name FROM memory_projects').fetchall():
            if r['id'] != exclude_id and fold(r['name']) == key:
                raise Conflict('Bu adla bir proje zaten var.', 'name')

    def list_projects(self, include_archived=False):
        where = '' if include_archived else " WHERE p.status='active'"
        rows = self.db.all(self._PROJECT_SQL + where + ' ORDER BY p.updated_at DESC, p.id DESC')
        return [self._project_public(r) for r in rows]

    def update_project(self, pid, patch):
        if not isinstance(patch, dict):
            raise ValidationFailed('Geçersiz istek.')
        sets = {}
        if 'name' in patch:
            sets['name'] = _line(patch['name'], config.MEMORY_NAME_CHARS, 'name')
        if 'description' in patch:
            sets['description'] = _text(patch['description'], 2000, 'description')
        if 'status' in patch:
            if patch['status'] not in PROJECT_STATUSES:
                raise ValidationFailed('Geçersiz proje durumu.', 'status')
            sets['status'] = patch['status']
        if not sets:
            raise ValidationFailed('Güncellenecek alan yok.')
        sets['updated_at'] = self.clock()
        cols = ', '.join(f'{k}=?' for k in sets)          # kolon adlari yukaridaki SABIT anahtarlardir
        with self.db.tx() as c:
            if 'name' in sets:
                self._check_name_free(c, sets['name'], exclude_id=pid)
            try:
                cur = c.execute(f'UPDATE memory_projects SET {cols} WHERE id=?', (*sets.values(), pid))
            except sqlite3.IntegrityError:
                raise Conflict('Bu adla bir proje zaten var.', 'name')
            if cur.rowcount == 0:
                raise NotFound('Proje bulunamadı.')
        return self.get_project(pid)

    def delete_project(self, pid):
        """Projeyi siler. Ogeleri SILMEZ (projesiz kalirlar); gorev baglari kalkar."""
        with self.db.tx() as c:
            cur = c.execute('DELETE FROM memory_projects WHERE id=?', (pid,))
            if cur.rowcount == 0:
                raise NotFound('Proje bulunamadı.')
        return {'deleted': pid}

    # ---- gorev <-> proje
    def attach_task(self, task_id, project_id):
        """project_id None ise bag kaldirilir."""
        with self.db.tx() as c:
            if not c.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
                raise NotFound('Görev bulunamadı.')
            if project_id is None:
                c.execute('DELETE FROM memory_task_projects WHERE task_id=?', (task_id,))
                return None
            _int_id(project_id, 'project_id')
            if not c.execute('SELECT 1 FROM memory_projects WHERE id=?', (project_id,)).fetchone():
                raise NotFound('Proje bulunamadı.')
            c.execute('INSERT INTO memory_task_projects(task_id, project_id) VALUES(?,?) '
                      'ON CONFLICT(task_id) DO UPDATE SET project_id=excluded.project_id', (task_id, project_id))
        return self.get_project(project_id)

    def project_of_task(self, task_id):
        r = self.db.one('SELECT project_id FROM memory_task_projects WHERE task_id=?', (task_id,))
        return r['project_id'] if r else None

    # ================================================================== ogeler
    def _item_public(self, r, full=True):
        body = r['body'] or ''
        d = {'id': r['id'], 'kind': r['kind'], 'title': r['title'],
             'body': body if full else util.truncate(body, 300),
             'tags': jload(r['tags'], []) or [], 'project_id': r['project_id'], 'project_name': r.get('project_name'),
             'task_id': r['task_id'], 'run_id': r['run_id'], 'source': r['source'], 'status': r['status'],
             'supersedes_id': r['supersedes_id'], 'created_at': _iso(r['created_at']),
             'updated_at': _iso(r['updated_at'])}
        return d

    _ITEM_SQL = ('SELECT i.*, p.name AS project_name FROM memory_items i '
                 'LEFT JOIN memory_projects p ON p.id=i.project_id')

    def get_item(self, item_id):
        r = self.db.one(self._ITEM_SQL + ' WHERE i.id=?', (item_id,))
        if not r:
            raise NotFound('Hafıza kaydı bulunamadı.')
        return self._item_public(r)

    def list_items(self, project_id=None, kind=None, status=None, limit=50, offset=0):
        where, args = [], []
        if project_id is not None:
            where.append('i.project_id=?')
            args.append(_int_id(project_id, 'project_id'))
        if kind:
            where.append('i.kind=?')
            args.append(_kind(kind))
        if status:
            if status not in ('active', 'superseded'):
                raise ValidationFailed('Geçersiz durum.', 'status')
            where.append('i.status=?')
            args.append(status)
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        sql = self._ITEM_SQL + (' WHERE ' + ' AND '.join(where) if where else '') + \
            ' ORDER BY i.updated_at DESC, i.id DESC LIMIT ? OFFSET ?'
        return [self._item_public(r, full=False) for r in self.db.all(sql, (*args, limit, offset))]

    def add_item(self, kind, title, body='', project_id=None, task_id=None, tags=None, supersedes_id=None,
                 source='user', run_id=None, _allowed=USER_KINDS):
        kind = _kind(kind, _allowed)
        title = _line(title, config.MEMORY_TITLE_CHARS, 'title')
        body = _text(body, config.MEMORY_BODY_CHARS, 'body')
        tags = _tags(tags)
        if project_id is not None:
            _int_id(project_id, 'project_id')
        if task_id is not None:
            _int_id(task_id, 'task_id')
        if supersedes_id is not None:
            _int_id(supersedes_id, 'supersedes_id')
        now = self.clock()
        with self.db.tx() as c:
            if project_id is not None and not c.execute('SELECT 1 FROM memory_projects WHERE id=?',
                                                         (project_id,)).fetchone():
                raise NotFound('Proje bulunamadı.')
            if task_id is not None and not c.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
                raise NotFound('Görev bulunamadı.')
            if supersedes_id is not None:
                old = c.execute('SELECT kind, status, project_id FROM memory_items WHERE id=?',
                                (supersedes_id,)).fetchone()
                if not old:
                    raise NotFound('Yerine geçilen kayıt bulunamadı.')
                if kind != 'decision' or old['kind'] != 'decision':
                    raise ValidationFailed('Yalnızca bir karar başka bir kararın yerine geçebilir.', 'supersedes_id')
                if old['status'] != 'active':
                    raise Conflict('Bu karar zaten yürürlükten kalkmış.', 'supersedes_id')
                if project_id is None:
                    project_id = old['project_id']
            if c.execute('SELECT COUNT(*) FROM memory_items').fetchone()[0] >= config.MEMORY_MAX_ITEMS:
                raise Conflict(f'Hafıza dolu (en fazla {config.MEMORY_MAX_ITEMS} kayıt). Eski kayıtları silin.')
            cur = c.execute(
                'INSERT INTO memory_items(kind, project_id, task_id, run_id, title, body, tags, source, status, '
                'supersedes_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (kind, project_id, task_id, run_id, title, body, jdump(tags), source, 'active', supersedes_id, now, now))
            iid = cur.lastrowid
            if supersedes_id is not None:
                c.execute("UPDATE memory_items SET status='superseded', updated_at=? WHERE id=?", (now, supersedes_id))
                self._touch_index(c, supersedes_id)
            self._index_put(c, iid, title, tags, body)
            if project_id is not None:
                c.execute('UPDATE memory_projects SET updated_at=? WHERE id=?', (now, project_id))
        return self.get_item(iid)

    @staticmethod
    def _touch_index(c, item_id):
        r = c.execute('SELECT title, tags, body FROM memory_items WHERE id=?', (item_id,)).fetchone()
        if r:
            Memory._index_put(c, item_id, r['title'], jload(r['tags'], []) or [], r['body'])

    def update_item(self, item_id, patch):
        if not isinstance(patch, dict):
            raise ValidationFailed('Geçersiz istek.')
        sets = {}
        if 'title' in patch:
            sets['title'] = _line(patch['title'], config.MEMORY_TITLE_CHARS, 'title')
        if 'body' in patch:
            sets['body'] = _text(patch['body'], config.MEMORY_BODY_CHARS, 'body')
        if 'tags' in patch:
            sets['tags'] = jdump(_tags(patch['tags']))
        if 'project_id' in patch:
            pid = patch['project_id']
            if pid is not None:
                _int_id(pid, 'project_id')
            sets['project_id'] = pid
        if 'status' in patch:
            if patch['status'] not in ('active', 'superseded'):
                raise ValidationFailed('Geçersiz durum.', 'status')
            sets['status'] = patch['status']
        if not sets:
            raise ValidationFailed('Güncellenecek alan yok.')
        now = self.clock()
        sets['updated_at'] = now
        cols = ', '.join(f'{k}=?' for k in sets)          # kolon adlari yukaridaki SABIT anahtarlardir
        with self.db.tx() as c:
            if not c.execute('SELECT 1 FROM memory_items WHERE id=?', (item_id,)).fetchone():
                raise NotFound('Hafıza kaydı bulunamadı.')
            if sets.get('project_id') is not None and not c.execute(
                    'SELECT 1 FROM memory_projects WHERE id=?', (sets['project_id'],)).fetchone():
                raise NotFound('Proje bulunamadı.')
            c.execute(f'UPDATE memory_items SET {cols} WHERE id=?', (*sets.values(), item_id))
            self._touch_index(c, item_id)
        return self.get_item(item_id)

    def delete_item(self, item_id):
        with self.db.tx() as c:
            cur = c.execute('DELETE FROM memory_items WHERE id=?', (item_id,))
            if cur.rowcount == 0:
                raise NotFound('Hafıza kaydı bulunamadı.')
            c.execute('DELETE FROM memory_fts WHERE rowid=?', (item_id,))
        return {'deleted': item_id}

    # ================================================================== arama
    def search(self, query, project_id=None, kind=None, include_superseded=False, limit=None):
        """Donus: [{id, kind, title, snippet, project_id, project_name, task_id, status, tags, score, created_at,
        updated_at}] en alakalidan baslayarak. Gecersiz/bos sorgu: ValidationFailed."""
        if not isinstance(query, str) or not query.strip():
            raise ValidationFailed('Arama sorgusu boş olamaz.', 'q')
        if len(query) > 300:
            raise ValidationFailed('Arama sorgusu en fazla 300 karakter olabilir.', 'q')
        toks = query_tokens(query)
        if not toks:
            return []
        limit = config.MEMORY_SEARCH_LIMIT if limit is None else max(1, min(50, int(limit)))
        flt, fargs = [], []
        if not include_superseded:
            flt.append("i.status='active'")
        if project_id is not None:
            flt.append('i.project_id=?')
            fargs.append(_int_id(project_id, 'project_id'))
        if kind:
            flt.append('i.kind=?')
            fargs.append(_kind(kind))
        rows = self._search_fts(toks, flt, fargs, limit) if self.fts else self._search_plain(toks, flt, fargs, limit)
        return [self._hit(r, toks) for r in rows]

    _HIT_COLS = ('i.id, i.kind, i.title, i.body, i.tags, i.project_id, p.name AS project_name, i.task_id, i.run_id, '
                 'i.status, i.source, i.created_at, i.updated_at')
    _HIT_FROM = ('FROM memory_fts JOIN memory_items i ON i.id = memory_fts.rowid '
                 'LEFT JOIN memory_projects p ON p.id=i.project_id')

    def _search_fts(self, toks, flt, fargs, limit):
        where = ' AND '.join(['memory_fts MATCH ?'] + flt)
        sql = (f'SELECT {self._HIT_COLS}, bm25(memory_fts, 3.0, 2.0, 1.0) AS score {self._HIT_FROM} '
               f'WHERE {where} ORDER BY score, i.updated_at DESC LIMIT ?')
        for mode in ('and', 'or'):                           # once hepsi, yoksa herhangi biri (bm25 yine siralar)
            if mode == 'or' and len(toks) == 1:
                break
            try:
                rows = self.db.all(sql, (_fts_query(toks, mode), *fargs, limit))
            except sqlite3.OperationalError as e:            # beklenmedik sorgu sozdizimi: ara yuz hata vermesin
                log.warning('hafiza FTS sorgusu basarisiz: %s', util.redact(e))
                return self._search_plain(toks, flt, fargs, limit)
            if rows:
                return rows
        return []

    def _search_plain(self, toks, flt, fargs, limit):
        likes = ' OR '.join('(f.title LIKE ? OR f.tags LIKE ? OR f.body LIKE ?)' for _ in toks)
        params = []
        for t in toks:
            p = f'%{t}%'
            params += [p, p, p]
        where = ' AND '.join([f'({likes})'] + flt)
        sql = (f'SELECT {self._HIT_COLS}, f.title AS ft, f.tags AS fg, f.body AS fb '
               f'FROM memory_fts f JOIN memory_items i ON i.id = f.rowid '
               f'LEFT JOIN memory_projects p ON p.id=i.project_id WHERE {where} '
               f'ORDER BY i.updated_at DESC LIMIT 500')
        rows = self.db.all(sql, (*params, *fargs))
        scored = []
        for r in rows:
            s, hit = 0.0, 0
            for t in toks:
                h = 0
                if t in r['ft']:
                    h += 3
                if t in r['fg']:
                    h += 2
                h += min(5, r['fb'].count(t))
                if h:
                    hit += 1
                    s += h
            if hit:
                scored.append((hit == len(toks), s, r))
        if any(full for full, _s, _r in scored):             # FTS ile ayni: once hepsini iceren kayitlar, yoksa herhangi biri
            scored = [x for x in scored if x[0]]
        scored.sort(key=lambda x: (x[1], x[2]['updated_at']), reverse=True)
        out = []
        for _all, s, r in scored[:limit]:
            r = dict(r)
            r['score'] = -s                                  # FTS ile ayni yon: kucuk = daha iyi
            out.append(r)
        return out

    def _hit(self, r, toks):
        body = r['body'] or ''
        fb = fold(body)
        pos = -1
        for t in toks:
            p = fb.find(t)
            if p != -1 and (pos == -1 or p < pos):
                pos = p
        n = config.MEMORY_SNIPPET_CHARS
        if pos == -1 or len(body) <= n:
            snip = body[:n]
            start = 0
        else:
            start = max(0, pos - n // 4)
            snip = body[start:start + n]
        snip = re.sub(r'\s+', ' ', snip).strip()
        if start > 0:
            snip = '…' + snip
        if len(body) > start + n:
            snip += '…'
        return {'id': r['id'], 'kind': r['kind'], 'title': r['title'], 'snippet': snip,
                'tags': jload(r['tags'], []) or [], 'project_id': r['project_id'], 'project_name': r['project_name'],
                'task_id': r['task_id'], 'status': r['status'], 'source': r['source'], 'score': round(float(r['score']), 4),
                'created_at': _iso(r['created_at']), 'updated_at': _iso(r['updated_at'])}

    # ================================================================== gorev sonuclari
    def _latest_success(self, task_id):
        return self.db.one("SELECT id, run_no, result FROM task_runs WHERE task_id=? AND status='succeeded' "
                           "AND result IS NOT NULL AND result!='' ORDER BY id DESC LIMIT 1", (task_id,))

    def remember_task(self, task_id):
        """Gorevin SON basarili sonucunu hafizaya elle kaydeder (proje bagli ise o projeye)."""
        t = self.db.one('SELECT id, title FROM tasks WHERE id=?', (task_id,))
        if not t:
            raise NotFound('Görev bulunamadı.')
        run = self._latest_success(task_id)
        if not run:
            raise Conflict('Bu görevin kaydedilecek başarılı bir sonucu yok.')
        if self.db.one('SELECT 1 AS x FROM memory_items WHERE run_id=?', (run['id'],)):
            raise Conflict('Bu çalıştırmanın sonucu zaten hafızada.')
        return self._store_result(t['id'], t['title'], run['id'], run['run_no'], run['result'], source='user')

    def ingest_result(self, task_id, task_title, run_id, run_no, result):
        """Otomatik kayit: YALNIZCA bir projeye bagli gorevlerin sonuclari ve ayar aciksa. Tekrar cagri guvenlidir
        (ayni run_id ikinci kez yazilmaz). Donus: kayit (dict) veya None (kayit yapilmadi)."""
        if not config.MEMORY_AUTO_INGEST or config.MEMORY_MAX_AUTO_ITEMS <= 0:
            return None
        if not (result or '').strip() or run_id is None:
            return None
        if self.project_of_task(task_id) is None:
            return None
        try:
            item = self._store_result(task_id, task_title, run_id, run_no, result, source='auto')
        except sqlite3.IntegrityError:
            return None                                      # ayni calistirma zaten kayitli
        self._prune_auto()
        return item

    def _store_result(self, task_id, task_title, run_id, run_no, result, source):
        title = util.truncate(re.sub(r'\s+', ' ', _CTRL.sub('', str(task_title or 'Görev'))).strip() or 'Görev', 120)
        if run_no:
            title += f' (çalıştırma #{int(run_no)})'
        body = _CTRL.sub('', str(result)).strip()
        body = body[:config.MEMORY_BODY_CHARS]
        pid = self.project_of_task(task_id)
        now = self.clock()
        with self.db.tx() as c:
            if source == 'user' and c.execute('SELECT COUNT(*) FROM memory_items').fetchone()[0] >= config.MEMORY_MAX_ITEMS:
                raise Conflict(f'Hafıza dolu (en fazla {config.MEMORY_MAX_ITEMS} kayıt). Eski kayıtları silin.')
            cur = c.execute(
                'INSERT INTO memory_items(kind, project_id, task_id, run_id, title, body, tags, source, status, '
                'supersedes_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('result', pid, task_id, run_id, title, body, '[]', source, 'active', None, now, now))
            iid = cur.lastrowid
            self._index_put(c, iid, title, [], body)
            if pid is not None:
                c.execute('UPDATE memory_projects SET updated_at=? WHERE id=?', (now, pid))
        return self.get_item(iid)

    def _prune_auto(self):
        """Otomatik kayitlar tavani asarsa EN ESKI otomatik kayitlar silinir. Elle yazilanlara dokunulmaz."""
        keep = config.MEMORY_MAX_AUTO_ITEMS
        with self.db.tx() as c:
            ids = [r['id'] for r in c.execute(
                "SELECT id FROM memory_items WHERE source='auto' ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?",
                (keep,)).fetchall()]
            for i in ids:
                c.execute('DELETE FROM memory_items WHERE id=?', (i,))
                c.execute('DELETE FROM memory_fts WHERE rowid=?', (i,))
        return len(ids)

    # ================================================================== istatistik ve ajan cikti bicimi
    def stats(self):
        d = self.db
        by_kind = {r['kind']: r['n'] for r in d.all('SELECT kind, COUNT(*) AS n FROM memory_items GROUP BY kind')}
        return {'projects': d.one('SELECT COUNT(*) AS n FROM memory_projects')['n'],
                'items': d.one('SELECT COUNT(*) AS n FROM memory_items')['n'],
                'by_kind': {k: by_kind.get(k, 0) for k in KINDS},
                'search_engine': 'fts5' if self.fts else 'basic',
                'limits': {'projects': config.MEMORY_MAX_PROJECTS, 'items': config.MEMORY_MAX_ITEMS,
                           'auto_items': config.MEMORY_MAX_AUTO_ITEMS, 'body_chars': config.MEMORY_BODY_CHARS}}

    @staticmethod
    def render_hits(hits, query):
        """Arama sonucunu modele verilecek DUZ METNE cevirir (guvenilmeyen veri olarak sarmalamayi cagiran yapar)."""
        if not hits:
            return _NO_RESULT
        per = max(160, min(config.MEMORY_SNIPPET_CHARS, 1800 // len(hits)))
        lines = [f'HAFIZA SONUÇLARI ({len(hits)} kayıt, sorgu: {util.truncate(query, 80)}):']
        for h in hits:
            when = (h['updated_at'] or '')[:10]
            meta = [KIND_LABEL.get(h['kind'], h['kind'])]
            if h.get('project_name'):
                meta.append('proje: ' + h['project_name'])
            if when:
                meta.append(when)
            if h['kind'] == 'result' and h.get('source') == 'auto':
                meta.append('otomatik kayıt')
            lines.append(f"[H{h['id']}] {h['title']} ({' | '.join(meta)})")
            if h.get('snippet'):
                lines.append(util.truncate(h['snippet'], per))
        return '\n'.join(lines)


NO_RESULT_TEXT = _NO_RESULT
