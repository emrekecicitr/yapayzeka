"""Task Recovery: yarim kalan (surec oldu, kilit suresi doldu) gorevlerin guvenli kurtarilmasi.

Bir gorev 'running' durumunda kalip lease_until suresi dolmussa sahibi artik yasamiyor demektir
(uygulama kapandi/cokmus ya da baska bir surec oldu). Kurtarma:
- Calistirma kaydi 'interrupted' olur, gorev 'scheduled' + resume_run_id ile hemen yeniden siraya girer.
- Yarim kalan adimlar 'pending'e alinir; TAMAMLANMIS adimlar ve ciktilari korunur, is kaldigi adimdan surer.
- Ayni calistirma MAX_RUN_ATTEMPTS kez kesintiye ugrarsa (surekli cokertiyor olabilir) kalici basarisiz sayilir.
- Kosul kontrolu sirasinda kalan gorevler (calistirma kaydi yok) yeniden kontrole alinir.
Kilit hala gecerliyse (baska surecte canli calisiyor) dokunulmaz.
"""
from . import config, schedule as sch
from .storage import jload


def recover_orphans(mgr, runs, notifier, now):
    """Donus: kurtarilan/islenen gorev id listesi."""
    db = mgr.db
    rows = db.all("SELECT id, lease_until FROM tasks WHERE status='running' AND (lease_until IS NULL OR lease_until<?)",
                  (now,))
    handled = []
    for r in rows:
        tid = r['id']
        failed_final = None
        with db.tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=? AND status='running' AND "
                            "(lease_until IS NULL OR lease_until<?)", (tid, now)).fetchone()
            if not row:
                continue  # bu arada baska biri halletti ya da kilit yenilendi
            row = dict(row)
            run = c.execute("SELECT * FROM task_runs WHERE task_id=? AND status='running' ORDER BY id DESC LIMIT 1",
                            (tid,)).fetchone()
            if run is None:
                c.execute("UPDATE tasks SET status='scheduled', next_run_at=?, lease_owner=NULL, lease_until=NULL, "
                          "current_step='Kesinti sonrası yeniden kontrol', updated_at=? WHERE id=?", (now, now, tid))
                handled.append(tid)
                continue
            run = dict(run)
            if run['attempt'] >= config.MAX_RUN_ATTEMPTS:
                msg = 'Görev defalarca kesintiye uğradığı için durduruldu.'
                c.execute("UPDATE task_runs SET status='failed', error=?, finished_at=? WHERE id=?", (msg, now, run['id']))
                cfg = jload(row['schedule_config'], {}) or {}
                nxt = None
                if sch.is_repeating(row['task_type'], cfg):
                    try:
                        nxt = sch.following_run_at(row['task_type'], cfg, row['timezone'], now)
                    except sch.ScheduleError:
                        nxt = None
                if nxt is not None:
                    c.execute("UPDATE tasks SET status='scheduled', next_run_at=?, last_error=?, last_run_ok=0, "
                              "resume_run_id=NULL, run_trigger=NULL, lease_owner=NULL, lease_until=NULL, "
                              "retry_count=0, updated_at=? WHERE id=?", (nxt, msg, now, tid))
                else:
                    c.execute("UPDATE tasks SET status='failed', next_run_at=NULL, last_error=?, last_run_ok=0, "
                              "resume_run_id=?, run_trigger=NULL, lease_owner=NULL, lease_until=NULL, "
                              "current_step='Başarısız', updated_at=? WHERE id=?", (msg, run['id'], now, tid))
                failed_final = (row, run['id'], msg)
            else:
                c.execute("UPDATE task_runs SET status='interrupted' WHERE id=?", (run['id'],))
                c.execute("UPDATE task_steps SET status='pending' WHERE run_id=? AND status IN ('running','failed')",
                          (run['id'],))
                c.execute("UPDATE tasks SET status='scheduled', next_run_at=?, run_trigger='recovery', "
                          "resume_run_id=?, lease_owner=NULL, lease_until=NULL, "
                          "current_step='Kesinti sonrası kaldığı yerden devam edecek', updated_at=? WHERE id=?",
                          (now, run['id'], now, tid))
            handled.append(tid)
        if failed_final:
            row, rid, msg = failed_final
            notifier.notify(mgr.get_row(tid), 'failed', f"Görev başarısız: {row['title']}", msg, run_id=rid)
    return handled
