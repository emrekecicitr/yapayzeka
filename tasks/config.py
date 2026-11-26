"""Otonom Gorev Sistemi: sabitler ve ortam degiskeniyle degistirilebilir ayarlar.

Tum degerler guvenli varsayilanlarla gelir. Degistirmek icin ilgili ortam
degiskenini tanimlamak yeterlidir (kod degisikligi gerekmez).
"""
import os


def _int(name, default, lo=None, hi=None):
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# Veritabani: mevcut kullanici verileri gibi ev dizininde, ama ayri dosya.
DB_PATH = os.environ.get('TASKS_DB_PATH') or os.path.expanduser('~/ai_tasks.db')

DEFAULT_TIMEZONE = 'Europe/Istanbul'

# Es zamanli en fazla kac gorev calissin (kisisel kullanim: kucuk tutulur).
MAX_CONCURRENT = _int('TASKS_MAX_CONCURRENT', 2, 1, 8)
# Zamanlayici dongusu araligi (sn).
TICK_SECONDS = _int('TASKS_TICK_SECONDS', 5, 1, 60)
# Kilit (lease) suresi ve yenileme araligi (sn). Surec olurse lease dolar, gorev kurtarilir.
LEASE_SECONDS = _int('TASKS_LEASE_SECONDS', 90, 20, 600)
HEARTBEAT_SECONDS = _int('TASKS_HEARTBEAT_SECONDS', 20, 5, 120)
# Bir gorev calistirmasinin toplam zaman siniri (sn).
RUN_TIMEOUT_SECONDS = _int('TASKS_RUN_TIMEOUT', 900, 60, 7200)
# Tek model cagrisi zaman asimi (sn).
LLM_CALL_TIMEOUT = _int('TASKS_LLM_TIMEOUT', 60, 10, 300)

# Tekrar deneme: ustel geri cekilme.
MAX_RETRIES = _int('TASKS_MAX_RETRIES', 3, 0, 10)
BACKOFF_BASE_SECONDS = _int('TASKS_BACKOFF_BASE', 30, 1, 3600)
BACKOFF_CAP_SECONDS = _int('TASKS_BACKOFF_CAP', 1800, 30, 86400)
# Bir calistirma en fazla kac kez (kurtarma dahil) devam ettirilebilir.
MAX_RUN_ATTEMPTS = _int('TASKS_MAX_RUN_ATTEMPTS', 6, 1, 20)

# Kacirilan tekrarlayan calistirma: bu kadar saniyeden fazla gecikmisse atlanip sonraki zamana gidilir.
MISSED_GRACE_SECONDS = _int('TASKS_MISSED_GRACE', 6 * 3600, 60, 7 * 86400)

# Sinirlar.
MAX_TASKS = _int('TASKS_MAX_TASKS', 200, 1, 5000)
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 4000
MAX_RESULT_CHARS = 60000
MAX_STEPS = 8
MIN_INTERVAL_MINUTES = _int('TASKS_MIN_INTERVAL_MIN', 15, 1, 1440)
MIN_CHECK_INTERVAL_MINUTES = _int('TASKS_MIN_CHECK_INTERVAL_MIN', 30, 5, 1440)
MAX_BODY_BYTES = 64 * 1024

# Saklama politikasi: tamamlanmis/iptal edilmis eski gorevler ve eski calistirmalar.
RETENTION_DAYS = _int('TASKS_RETENTION_DAYS', 90, 1, 3650)
MAX_RUNS_PER_TASK = _int('TASKS_MAX_RUNS_PER_TASK', 50, 5, 1000)
NOTIFICATION_KEEP_DAYS = _int('TASKS_NOTIFICATION_KEEP_DAYS', 60, 1, 3650)

# Arastirma onbellegi (kalici).
CACHE_TTL_SECONDS = _int('TASKS_CACHE_TTL', 6 * 3600, 0, 7 * 86400)

# Log: dosya buyumesin diye dondurmeli log (tasks.log).
LOG_PATH = os.environ.get('TASKS_LOG_PATH') or os.path.expanduser('~/ai_tasks.log')
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUPS = 2

# Ajan modu (araç kullanan döngü). Tüm sınırlar SERT tavandır: döngü bu sayılardan fazla dönemez.
AGENT_BUDGETS = ('dar', 'normal', 'genis')
AGENT_TURNS = {'dar': 5, 'normal': 8, 'genis': 12}       # en çok model karar turu
AGENT_SEARCHES = {'dar': 2, 'normal': 4, 'genis': 6}     # en çok internet araması
AGENT_READS = {'dar': 1, 'normal': 3, 'genis': 5}        # en çok sayfa okuma
AGENT_CALCS = 8                                          # en çok hesaplama
AGENT_RECALLS = 2                                        # en çok önceki sonuç okuma
AGENT_MEMORY_SEARCHES = 3                                # en çok kalıcı hafıza araması (hafıza yerel ve ücretsizdir)
AGENT_MAX_TURNS_HARD = 15
AGENT_TURN_MAX_TOKENS = _int('TASKS_AGENT_TURN_TOKENS', 700, 200, 2000)
AGENT_OBS_CHARS = 2400          # tek araç sonucunun modele verilen en çok karakteri
AGENT_READ_CHARS = 3200         # sayfa okuma sonucu için
AGENT_MAX_INVALID = 3           # toplam geçersiz çıktı hakkı (aşılırsa ajan nazikçe durur)
AGENT_REPAIR_PER_TURN = 2       # aynı tur içinde geçersiz çıktıyı düzelttirme hakkı
AGENT_MAX_SOURCES = 12
AGENT_CALC_EXPR_CHARS = 200

# Kendini degerlendirme (denetci adimi). Tum sinirlar sabittir: en fazla 2 denetim + 1 duzeltme + 2 dogrulama aramasi.
REVIEW_MAX_ISSUES = 8
REVIEW_MAX_VERIFY = 2           # denetcinin isteyebilecegi en cok ek dogrulama aramasi
REVIEW_MAX_CALC = 6             # sistemin yeniden hesaplayacagi en cok iddia
REVIEW_MAX_REPAIR = 1           # gecersiz denetci ciktisi icin duzeltme hakki (cagri basina)
REVIEW_DRAFT_CHARS = 5500
REVIEW_EVIDENCE_CHARS = 4500
REVIEW_CRITIC_TOKENS = 1200
REVIEW_NOTES_MAX = 5

TASK_TYPES = ('once', 'recurring', 'conditional', 'multistep')
STATUSES = ('pending', 'scheduled', 'running', 'paused', 'completed',
            'failed', 'cancelled', 'awaiting_approval')
RESEARCH_DEPTHS = ('hizli', 'normal', 'derin')
OUTPUT_FORMATS = ('rapor', 'ozet', 'madde', 'tablo')
OUTPUT_LENGTHS = ('kisa', 'orta', 'uzun')
NOTIFY_PREFS = ('hepsi', 'sadece_hatalar', 'kapali')

# Kalici hafiza (projeler, kararlar, belgeler, notlar, otomatik sonuc kayitlari). Hicbir sinir sessizce asilmaz.
MEMORY_MAX_PROJECTS = _int('TASKS_MEMORY_MAX_PROJECTS', 100, 1, 2000)
MEMORY_MAX_ITEMS = _int('TASKS_MEMORY_MAX_ITEMS', 5000, 10, 100000)
MEMORY_MAX_AUTO_ITEMS = _int('TASKS_MEMORY_MAX_AUTO', 500, 0, 50000)   # otomatik kayitlarin tavani (en eskiler silinir)
MEMORY_AUTO_INGEST = _int('TASKS_MEMORY_AUTO_INGEST', 1, 0, 1)         # 1: projeye bagli gorevlerin sonuclari hafizaya yazilir
MEMORY_NAME_CHARS = 80
MEMORY_TITLE_CHARS = 160
MEMORY_BODY_CHARS = 20000
MEMORY_TAGS_MAX = 8
MEMORY_TAG_CHARS = 30
MEMORY_SEARCH_LIMIT = 8          # arac ve API varsayilan/azami sonuc sayisi
MEMORY_SNIPPET_CHARS = 420
MEMORY_TOOL_CHARS = 2400         # hafiza arac sonucunun modele verilen en cok karakteri

# Eylem yetenegi (onayli eylemler). VARSAYILAN KAPALI; acmak icin TASKS_ACTIONS_ENABLED=1. KILL SWITCH: TASKS_ACTIONS_ENABLED=0 iken eylem araci modele sunulmaz,
# onaylama/calistirma reddedilir ve tum mevcut davranis (istemler dahil) eylemsiz surumle ayni kalir.
# Anahtar HER cagrida ortamdan okunur (yeniden baslatma gerekmez).
def actions_enabled():
    return _int('TASKS_ACTIONS_ENABLED', 0, 0, 1) == 1          # varsayilan KAPALI: bilerek acilir


def actions_approval_token():
    """Eylem API'sine uzaktan erisim belirteci (en az 24 karakter gecerli). Bos: yalnizca yerel istek. Her cagrida okunur."""
    return (os.environ.get('TASKS_ACTIONS_APPROVAL_TOKEN') or '').strip()


ACTIONS_WORKSPACE = os.environ.get('TASKS_WORKSPACE') or '~/ai_workspace'     # dosya eylemlerinin tek izinli klasoru
ACTIONS_PER_RUN = _int('TASKS_ACTIONS_PER_RUN', 3, 1, 20)                    # bir calistirmada en cok oneri
ACTIONS_PER_DAY = _int('TASKS_ACTIONS_PER_DAY', 20, 1, 1000)                 # son 24 saatte toplam oneri
ACTIONS_PENDING_MAX = _int('TASKS_ACTIONS_PENDING_MAX', 10, 1, 200)          # bekleyen (onaylanmamis) oneri tavani
ACTIONS_ARGS_BYTES = _int('TASKS_ACTIONS_ARGS_BYTES', 32768, 1024, 262144)   # kanonik argumanlarin en cok bayti
ACTIONS_TTL_SECONDS = _int('TASKS_ACTIONS_TTL', 86400, 60, 30 * 86400)       # onay suresi (varsayilan 24 saat)
ACTIONS_EXEC_STALE_SECONDS = _int('TASKS_ACTIONS_EXEC_STALE', 60, 10, 3600)  # 'executing' bu sureden eskiyse surec olmus sayilir
ACTIONS_FILE_MAX_BYTES = _int('TASKS_ACTIONS_FILE_MAX', 20000, 100, 262144)  # file_write icerik siniri (bayt)
ACTIONS_FILE_EXTS = ('.txt', '.md', '.csv', '.json')                         # file_write uzanti izin listesi
ACTIONS_MEMORY_NOTE = _int('TASKS_ACTIONS_MEMORY', 1, 0, 1)                  # 1: tamamlanan eylem, projeli gorevde otomatik nota yazilir


# Otonom orkestrasyon (hedef -> bagimliliklarla alt gorevler -> sentez). VARSAYILAN KAPALI; acmak icin TASKS_GOALS_ENABLED=1.
# Kapaliyken (kill switch) planlama, baslatma ve izleme DURUR (hicbir sey kaybolmaz; acilinca kaldigi yerden surer).
def goals_enabled():
    return _int('TASKS_GOALS_ENABLED', 0, 0, 1) == 1


GOALS_MAX_NODES = _int('TASKS_GOALS_MAX_NODES', 8, 1, 20)               # plandaki alt gorev tavani (sentez dahil degil)
GOALS_MAX_ACTIVE = _int('TASKS_GOALS_MAX_ACTIVE', 2, 1, 10)             # ayni anda etkin (planlanan/onay bekleyen/yurutulen) hedef
GOALS_PER_DAY = _int('TASKS_GOALS_PER_DAY', 10, 1, 200)                 # son 24 saatte olusturulabilecek hedef
GOALS_PARALLEL = _int('TASKS_GOALS_PARALLEL', 2, 1, 6)                  # bir hedefte ayni anda calisan alt gorev
GOALS_TIMEOUT_SECONDS = _int('TASKS_GOALS_TIMEOUT', 6 * 3600, 600, 7 * 86400)   # onaydan itibaren toplam sure
GOALS_MAX_RETRIES = _int('TASKS_GOALS_MAX_RETRIES', 2, 0, 10)           # hedef basina elle yeniden deneme hakki
GOALS_PLAN_ATTEMPTS = 2                                                 # gecersiz plan icin model cagrisi hakki
GOALS_PLAN_LEASE_SECONDS = 180                                          # planlama kilidi (surec olurse sure dolunca yeniden alinir)
GOALS_LAUNCH_STALE_SECONDS = 60                                         # 'launching' bu sureden eskiyse surec olmus sayilir
GOALS_DESCRIPTION_CHARS = 2000
GOALS_NODE_TITLE_CHARS = 80
GOALS_NODE_DESC_CHARS = 1500
GOALS_NODE_RESULT_CHARS = 20000                                         # alt gorev sonucunun hedefte saklanan en cok karakteri
GOALS_CONTEXT_CHARS = 6000                                              # bir alt goreve verilen onceki sonuclarin toplam siniri
GOALS_MAX_DEPS = 6
