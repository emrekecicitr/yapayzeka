"""Notification Manager.

Ilk surum: uygulama ici bildirim (veritabaninda saklanir, arayuz okunmamis sayisini ve listeyi gosterir).

Tekrar engelleme: her bildirimin benzersiz bir dedupe_key'i vardir (varsayilan: tur + gorev + calistirma).
Ayni olay icin ikinci kez eklenmeye calisilirsa veritabani UNIQUE kisitlamasi reddeder, bildirim cogalmaz.

Gelecek (Web Push / Android PWA): register_channel(fn) ile bir kanal eklenir; fn(bildirim_sozlugu) her yeni
bildirimde cagrilir. Kanal hatasi gorev akisini asla bozmaz. Mevcut sw.js'e push olayi eklemek yeterli olacaktir;
bu surumde harici push servisi YOKTUR ve calisiyormus gibi sunulmaz.
"""
import sqlite3

from .storage import jload


class Notifications:
    def __init__(self, db, clock):
        self.db = db
        self.clock = clock
        self._channels = []

    # ------------------------------------------------------------ tercihler
    def prefs(self):
        r = self.db.one("SELECT value FROM task_settings WHERE key='notifications_enabled'")
        enabled = True if r is None else r['value'] == '1'
        return {'enabled': enabled}

    def set_prefs(self, enabled):
        self.db.execute(
            "INSERT INTO task_settings(key, value) VALUES('notifications_enabled', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if enabled else '0',))
        return self.prefs()

    def register_channel(self, fn):
        self._channels.append(fn)

    # ------------------------------------------------------------ olusturma
    def _wanted(self, task_row, kind):
        if kind == 'awaiting_approval':
            return True  # kullanici onayi gerektigi icin her zaman uretilir
        if not self.prefs()['enabled']:
            return False
        pref = (jload(task_row.get('options'), {}) or {}).get('notify_pref', 'hepsi')
        if pref == 'kapali':
            return False
        if kind == 'failed':
            return True
        return pref == 'hepsi'

    def notify(self, task_row, kind, title, body='', run_id=None, dedupe_key=None):
        """Yeni bildirim id'si; tercih nedeniyle uretilmediyse ya da tekrar ise None."""
        try:
            if not self._wanted(task_row, kind):
                return None
            key = dedupe_key or f"{kind}:{task_row['id']}:{run_id or 0}"
            now = self.clock()
            try:
                cur = self.db.execute(
                    'INSERT INTO task_notifications(task_id, run_id, kind, title, body, dedupe_key, created_at) '
                    'VALUES(?,?,?,?,?,?,?)',
                    (task_row['id'], run_id, kind, str(title)[:160], str(body)[:400], key, now))
            except sqlite3.IntegrityError:
                return None  # ayni olay icin zaten var
            nid = cur.lastrowid
            item = {'id': nid, 'task_id': task_row['id'], 'run_id': run_id, 'kind': kind,
                    'title': title, 'body': body}
            for ch in list(self._channels):
                try:
                    ch(item)
                except Exception as e:  # kanal hatasi gorevi etkilemez
                    print(f'[gorevler] bildirim kanali hatasi: {type(e).__name__}')
            return nid
        except Exception as e:
            print(f'[gorevler] bildirim olusturulamadi: {type(e).__name__}: {e}')
            return None

    # ------------------------------------------------------------ okuma
    def list(self, limit=50, unread_only=False):
        where = 'WHERE n.read_at IS NULL' if unread_only else ''
        rows = self.db.all(
            'SELECT n.id, n.task_id, n.run_id, n.kind, n.title, n.body, n.created_at, n.read_at, '
            't.title AS task_title FROM task_notifications n JOIN tasks t ON t.id=n.task_id '
            f'{where} ORDER BY n.id DESC LIMIT ?', (int(limit),))
        import time
        out = []
        for r in rows:
            out.append({
                'id': r['id'], 'task_id': r['task_id'], 'run_id': r['run_id'], 'kind': r['kind'],
                'title': r['title'], 'body': r['body'], 'task_title': r['task_title'],
                'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(r['created_at'])),
                'read': r['read_at'] is not None,
            })
        return out

    def unread_count(self):
        return self.db.one('SELECT COUNT(*) AS n FROM task_notifications WHERE read_at IS NULL')['n']

    def mark_read(self, nid):
        cur = self.db.execute(
            'UPDATE task_notifications SET read_at=COALESCE(read_at, ?) WHERE id=?', (self.clock(), nid))
        return cur.rowcount == 1

    def mark_all_read(self):
        cur = self.db.execute('UPDATE task_notifications SET read_at=? WHERE read_at IS NULL', (self.clock(),))
        return cur.rowcount
