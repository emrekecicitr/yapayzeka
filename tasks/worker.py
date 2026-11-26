"""Scheduler + Worker: zamani gelen gorevleri arka planda calistirir.

- Scheduler dongusu (daemon thread): her TICK_SECONDS'ta kurtarma, kacirilanlari atlama, onaya ayirma ve
  atomik gorev alma (claim) yapar. Flask'in HTTP isteklerini ASLA bloke etmez.
- Worker havuzu: en fazla MAX_CONCURRENT gorev ayni anda calisir (bu sinir surec basinadir).
- Cok surec guvenligi: gorev alma veritabaninda tek bir kosullu UPDATE'tir; birden fazla scheduler ayni gorevi
  alamaz. Calisan gorev lease_until ile isaretlenir ve heartbeat ile yenilenir; surec olursa lease dolar ve
  baska bir scheduler gorevi kurtarir.
- start() idempotenttir; fork sonrasi (gunicorn --preload) thread'ler yeniden baslatilir.
"""
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import config, recovery, util

log = logging.getLogger('tasks')


class TaskRuntime:
    def __init__(self, mgr, runs, notifier, executor, clock=time.time,
                 max_concurrent=None, tick_seconds=None, actions=None, goals=None):
        self.goals = goals         # orkestrasyon servisi (hedef planlama/yurutme bakimi) veya None
        self.actions = actions     # eylem servisi (bakim: sure dolumu, cokme sonrasi cozum) veya None
        self.mgr = mgr
        self.runs = runs
        self.notifier = notifier
        self.executor = executor
        self.clock = clock
        self.max_concurrent = max_concurrent or config.MAX_CONCURRENT
        self.tick_seconds = tick_seconds or config.TICK_SECONDS
        self.owner = f'{os.getpid()}-{uuid.uuid4().hex[:8]}'
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._pool = None
        self._pid = None
        self._active = set()
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    # ------------------------------------------------------------ yasam dongusu
    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._pid == os.getpid():
                return False
            self._pid = os.getpid()
            self.owner = f'{os.getpid()}-{uuid.uuid4().hex[:8]}'
            self._stop = threading.Event()
            self._wake = threading.Event()
            self._active = set()
            self._pool = ThreadPoolExecutor(max_workers=self.max_concurrent, thread_name_prefix='gorev')
            self._thread = threading.Thread(target=self._loop, name='gorev-zamanlayici', daemon=True)
            self._thread.start()
        return True

    def stop(self, wait=True, timeout=10.0):
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and wait and t.is_alive():
            t.join(timeout)
        pool = self._pool
        if pool is not None:
            pool.shutdown(wait=wait, cancel_futures=True)
        self._thread = None
        self._pool = None

    def wake(self):
        """Elle calistirma/onay gibi eylemlerden sonra zamanlayiciyi beklemeden uyandirir."""
        self._wake.set()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _loop(self):
        stop = self._stop
        # Acilista hemen bir tur: kalan gorevler gecikmeden kurtarilir.
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - dongu asla olmemeli
                log.error('zamanlayici turu hatasi: %s: %s', type(e).__name__, util.redact(e))
            self._wake.wait(self.tick_seconds)
            self._wake.clear()

    # ------------------------------------------------------------ tek tur (testlerde dogrudan cagrilir)
    def tick(self, sync=False):
        now = self.clock()
        recovered = recovery.recover_orphans(self.mgr, self.runs, self.notifier, now)
        if recovered:
            log.info('kurtarilan gorevler: %s', recovered)
        skipped = self.mgr.skip_missed(now)
        if skipped:
            log.info('kacirilan tekrarlayan calistirmalar atlandi: %s', skipped)
        for tid in self.mgr.promote_approvals(now):
            row = self.mgr.get_row(tid)
            self.notifier.notify(
                row, 'awaiting_approval', f"Onay bekleniyor: {row['title']}",
                'Görevin çalışma zamanı geldi. Çalıştırmak için onaylayın.',
                dedupe_key=f"approval:{tid}:{int(row['next_run_at'] or now)}")
        if self.actions is not None:
            self.actions.tick()          # asla istisna firlatmaz
        if self.goals is not None:
            self.goals.tick(sync=sync)   # asla istisna firlatmaz; kill switch kapaliyken hicbir sey yapmaz
        with self._lock:
            free = self.max_concurrent - len(self._active)
        claims = self.mgr.claim_due(self.owner, free, now) if free > 0 else []
        for claim in claims:
            tid = claim['task']['id']
            with self._lock:
                self._active.add(tid)
            if sync:
                self._run_job(claim)
            else:
                try:
                    self._pool.submit(self._run_job, claim)
                except Exception as e:  # havuz kapaniyorsa kilidi birakmak icin kurtarma devralir
                    log.error('is havuza verilemedi: %s', type(e).__name__)
                    with self._lock:
                        self._active.discard(tid)
        if now - self._last_cleanup > 3600:
            self._last_cleanup = now
            try:
                self.mgr.cleanup()
            except Exception as e:  # noqa: BLE001
                log.error('temizlik hatasi: %s', type(e).__name__)
        return claims

    # ------------------------------------------------------------ is
    def _run_job(self, claim):
        tid = claim['task']['id']
        hb_stop = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(tid, hb_stop), daemon=True, name=f'gorev-hb-{tid}')
        hb.start()
        try:
            self.executor.execute(claim, self.owner)
        except Exception as e:  # noqa: BLE001
            log.error('gorev %s calistirma hatasi: %s: %s', tid, type(e).__name__, util.redact(e))
        finally:
            hb_stop.set()
            with self._lock:
                self._active.discard(tid)

    def _heartbeat(self, tid, stop):
        try:
            while not stop.wait(config.HEARTBEAT_SECONDS):
                try:
                    if not self.mgr.heartbeat(tid, self.owner):
                        return  # sahiplik kayboldu; yurutucu bir sonraki kontrol noktasinda durur
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self.mgr.db.close_thread()   # is basina bir thread acilir: baglantisi sizmasin

    def active_ids(self):
        with self._lock:
            return sorted(self._active)
