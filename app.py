import logging
import os
import json
import io
import base64
import subprocess
import threading
import re
import time
import tempfile
import httpx
import socket
import ipaddress
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from html import unescape as _html_unescape
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait, FIRST_COMPLETED
from urllib.parse import urlparse
from flask import Flask, request, jsonify, Response
from pypdf import PdfReader

# WARNING filtreleme
class NoWarningFilter(logging.Filter):
    def filter(self, record):
        return "WARNING: This is a development server" not in record.getMessage()
logging.getLogger('werkzeug').addFilter(NoWarningFilter())

# ============================================
# ATOMIK DOSYA YAZMA (crash-safe kayit)
# ============================================
# ONEMLI: Bir JSON dosyasinin uzerine DOGRUDAN yazmak (open(path,'w') + json.dump)
# tehlikelidir - yazma islemi TAM ORTASINDA kesilirse (uygulama kill edilir,
# elektrik gider, disk dolar vb.) dosya YARIM/BOZUK kalir. Bir sonraki
# acilista bu bozuk JSON okunamaz ve kod bunu "dosya yokmus" gibi yorumlayip
# TUM veriyi (sohbet gecmisi, ayarlar, API anahtarlari) sifirlayabilir.
#
# Cozum: once ayni klasorde GIZLI bir gecici dosyaya tam olarak yaz, diski
# senkronize et (fsync), sonra bu gecici dosyayi os.replace() ile HEDEFIN
# UZERINE TASI. os.replace() ayni dosya sistemi icinde ATOMIKTIR - islem ya
# TAMAMEN olur ya da HIC olmaz; yarim kalmis bir dosya asla ortada gorunmez,
# en kotu ihtimalle bir onceki (saglam) surum korunur.
def atomic_write_json(path, data, **json_kwargs):
    """path'e JSON'u atomik olarak yazar. Basarili olursa True, disk/izin gibi
    bir sebeple basarisiz olursa (sessizce degil) uyari basar ve False doner."""
    directory = os.path.dirname(path) or '.'
    tmp_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        # Hedefle AYNI klasorde gecici dosya olusturuyoruz ki os.replace() ayni
        # dosya sistemi uzerinde kalsin (farkli disk/bolumler arasi replace
        # atomik OLMAYABILIR).
        fd, tmp_path = tempfile.mkstemp(prefix='.tmp_', suffix='.json', dir=directory)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, **json_kwargs)
            f.flush()
            os.fsync(f.fileno())  # verinin gercekten diske indigini garanti eder
        os.replace(tmp_path, path)  # ATOMIK: ya tamamen biter ya hic degismez
        return True
    except Exception as e:
        print(f"[UYARI] '{path}' dosyasina yazma basarisiz oldu, veri KAYDEDILEMEDI: {e}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except:
                pass
        return False

app = Flask(__name__)

# ============================================
# API AYARLARI
# ============================================
API_KEYS_FILE = os.path.expanduser('~/ai_api_keys.json')
API_KEY_SLOT_COUNT = 6  # arayuzden en fazla 6 anahtar (1-6) yonetilebilir
# Koda gomulu sabit anahtar YOK. Anahtarlar Ayarlar sekmesinden girilir ve
# ~/ai_api_keys.json dosyasinda saklanir.
_DEFAULT_GROQ_API_KEYS = []
api_keys_lock = threading.Lock()

def _pad_slots(keys):
    keys = list(keys)[:API_KEY_SLOT_COUNT]
    while len(keys) < API_KEY_SLOT_COUNT:
        keys.append('')
    return keys

def load_api_key_slots():
    """6 sabit slot dondurur (bos olanlar '' olur). Dosya yoksa bos slotlarla ilk kez
    olusturulur, boylece arayuzden 'guncelleme' ayni slotlarin uzerine yazar (sira kaymaz)."""
    if os.path.exists(API_KEYS_FILE):
        try:
            with open(API_KEYS_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                if isinstance(saved, list):
                    return _pad_slots([(k or '').strip() if isinstance(k, str) else '' for k in saved])
        except:
            pass
    slots = _pad_slots(_DEFAULT_GROQ_API_KEYS)
    save_api_key_slots(slots)
    return slots

def save_api_key_slots(slots):
    atomic_write_json(API_KEYS_FILE, _pad_slots(slots), indent=2)

API_KEY_SLOTS = load_api_key_slots()
# GROQ_API_KEYS: rotasyon mantiginin (get_available_keys, key_cooldowns vb.)
# kullandigi AKTIF (bos olmayan) anahtar listesi. Slotlar degistiginde bu liste
# YERINDE (in-place) guncellenir, boylece onu referans alan tum fonksiyonlar
# otomatik olarak guncel listeyi gorur.
GROQ_API_KEYS = [k for k in API_KEY_SLOTS if k]
URL = 'https://api.groq.com/openai/v1/chat/completions'
# Kalici baglanti havuzu: her istekte yeni TLS el sikismasi yapilmaz (gecikmeyi dusurur).
_GROQ_CLIENT = httpx.Client(limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=60.0))
HISTORY_FILE = os.path.expanduser('~/ai_chat_history.json')

# ============================================
# ARAMA API'Sİ (Tavily) - internet aramasi icin ayri, tekil anahtar
# ============================================
TAVILY_KEY_FILE = os.path.expanduser('~/ai_search_key.json')
TAVILY_URL = 'https://api.tavily.com/search'
tavily_key_lock = threading.Lock()
# Koda gomulu sabit anahtar YOK. Anahtar Ayarlar sekmesinden girilir ve dosyada saklanir.
_TAVILY_BOOTSTRAP_KEY = ''

def _save_tavily_key_to_disk(key):
    atomic_write_json(TAVILY_KEY_FILE, {'tavily_api_key': key or ''}, indent=2)

def _load_tavily_key_from_disk():
    if os.path.exists(TAVILY_KEY_FILE):
        try:
            with open(TAVILY_KEY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get('tavily_api_key'), str):
                    return data['tavily_api_key'].strip()
        except:
            pass
        return ''
    # Dosya hic yoksa ilk kurulum: kullanicinin verdigi anahtarla bootstrap et.
    _save_tavily_key_to_disk(_TAVILY_BOOTSTRAP_KEY)
    return _TAVILY_BOOTSTRAP_KEY

_tavily_key_value = _load_tavily_key_from_disk()

def get_tavily_key():
    with tavily_key_lock:
        return _tavily_key_value

def set_tavily_key(new_key):
    global _tavily_key_value
    with tavily_key_lock:
        _tavily_key_value = (new_key or '').strip()
        _save_tavily_key_to_disk(_tavily_key_value)

# --------------------------------------------
# INTERNET ARAMASI AC/KAPA ANAHTARI
# --------------------------------------------
# Tavily anahtari gecerli olsa bile, kullanici arayuzden internet aramasini
# tamamen devre disi birakabilsin diye ayri bir on/off bayragi. Bu bayrak
# search_internet() cagrilmadan ONCE, tek bir noktada (bkz. /chat rotasi)
# kontrol edilir; kapaliyken arama fonksiyonuna hic girilmez - yani "%100
# etkili" kapatma, sadece arayuzde gizleme degil.
SEARCH_ENABLED_FILE = os.path.expanduser('~/ai_search_enabled.json')
search_enabled_lock = threading.Lock()

def _save_search_enabled_to_disk(enabled):
    atomic_write_json(SEARCH_ENABLED_FILE, {'search_enabled': bool(enabled)}, indent=2)

def _load_search_enabled_from_disk():
    if os.path.exists(SEARCH_ENABLED_FILE):
        try:
            with open(SEARCH_ENABLED_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get('search_enabled'), bool):
                    return data['search_enabled']
        except:
            pass
        return True
    # Dosya hic yoksa varsayilan: ACIK (mevcut davranisla ayni - Tavily anahtari
    # varsa arama calisir, yoksa zaten search_internet sessizce bos doner).
    _save_search_enabled_to_disk(True)
    return True

_search_enabled_value = _load_search_enabled_from_disk()

def get_search_enabled():
    with search_enabled_lock:
        return _search_enabled_value

def set_search_enabled(enabled):
    global _search_enabled_value
    with search_enabled_lock:
        _search_enabled_value = bool(enabled)
        _save_search_enabled_to_disk(_search_enabled_value)

MODELS_CHAIN = ['openai/gpt-oss-20b', 'qwen/qwen3.8-27b', 'openai/gpt-oss-120b']
VISION_MODEL = 'qwen/qwen3.8-27b'
selected_model = None
history_lock = threading.Lock()

# ============================================
# MODEL CONTEXT PENCERELERİ (dinamik chunk hesaplaması için)
# ============================================
# Groq dokümantasyonuna göre (131.072 token). İleride farklı bir model eklenir/
# değişirse SADECE burası güncellenir, ozetleme mantığina dokunmaya gerek kalmaz.
MODEL_CONTEXT_LIMITS = {
    'openai/gpt-oss-20b': 131072,
    'openai/gpt-oss-120b': 131072,
    'qwen/qwen3.8-27b': 131072,
}
DEFAULT_CONTEXT_LIMIT = 131072  # tabloda olmayan bir model gelirse guvenli varsayilan

# Turkce metinde ortalama karakter/token orani icin kaba tahmin (gercek tokenizer
# yoksa kullanilir). Hafif MUHAFAZAKAR tutuluyor (tahmini token sayisini olduğundan
# BUYUK gostersin) ki limit asimi riskini azaltsin.
CHARS_PER_TOKEN_ESTIMATE = 2.2

def estimate_tokens(text):
    """Gercek bir tokenizer olmadan kaba ama guvenli (muhafazakar) token tahmini."""
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN_ESTIMATE)

def get_safe_input_char_budget(model, reserve_ratio=0.55):
    """Verilen modelin context penceresine gore, ozetleme GIRDISI icin guvenle
    kullanilabilecek karakter sayisini hesaplar.
    reserve_ratio: pencerenin ne kadarinin sistem mesaji + cikti + pay icin
    AYRILACAGINI belirtir (varsayilan %55 ayrilir, girdiye %45 kalir) -> asiri
    iyimser hesaplayip context asimina dusmemek icin bilerek muhafazakar secildi.
    """
    limit = MODEL_CONTEXT_LIMITS.get(model, DEFAULT_CONTEXT_LIMIT)
    usable_input_tokens = int(limit * (1 - reserve_ratio))
    return int(usable_input_tokens * CHARS_PER_TOKEN_ESTIMATE)

def is_context_length_error(status_code, err_body):
    """Groq/OpenAI-uyumlu API'lerin context asimi hatasini (400 + belirli anahtar
    kelimeler) tanir. Boylece bu hata diger hatalardan (auth, sunucu vb.) ayirt
    edilip otomatik kucuk-parcaya-bolme fallback'i tetiklenebilir."""
    if status_code != 400:
        return False
    b = (err_body or '').lower()
    return any(k in b for k in (
        'context_length_exceeded', 'context length', 'maximum context',
        'too many tokens', 'reduce the length', "context window"
    ))

# ============================================
# AKILLI KEY ROTASYONU (model-öncelikli)
# ============================================
# key_cooldowns['gsk_...']['openai/gpt-oss-20b'] = 1234567.0  (bu zamana kadar bu key+model kombinasyonu kullanılmaz)
key_cooldowns = {k: {} for k in GROQ_API_KEYS}
key_state_lock = threading.Lock()
DEFAULT_COOLDOWN_SECONDS = 300  # Groq bekleme süresini bildirmezse güvenli varsayılan

def is_key_available(key, model):
    with key_state_lock:
        until = key_cooldowns.get(key, {}).get(model, 0)
        return time.time() >= until

def mark_key_cooldown(key, model, seconds):
    with key_state_lock:
        key_cooldowns.setdefault(key, {})[model] = time.time() + max(seconds, 1)

def get_available_keys(model):
    """Bu model için şu an müsait olan key'leri, dolan biletleri hariç tutarak döner."""
    return [k for k in GROQ_API_KEYS if is_key_available(k, model)]

def get_active_key():
    """Eski koddaki bazı yardımcı fonksiyonlar (özetleme worker'ı gibi) için: ilk müsait key'i döner."""
    avail = get_available_keys('qwen/qwen3.8-27b')
    if avail:
        return avail[0]
    return GROQ_API_KEYS[0] if GROQ_API_KEYS else None

def parse_reset_string(s):
    """Groq'un 'x-ratelimit-reset-requests' gibi '2m59.56s' formatındaki değerini saniyeye çevirir."""
    if not s:
        return None
    m = re.match(r'(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?', s.strip())
    if not m:
        return None
    h = float(m.group(1) or 0)
    mnt = float(m.group(2) or 0)
    sec = float(m.group(3) or 0)
    total = h * 3600 + mnt * 60 + sec
    return total if total > 0 else None

# ============================================
# SİSTEM MESAJI - PREMIUM
# ============================================
DEFAULT_SYSTEM_MSG = {
    'role': 'system',
    'content': """
# KİMLİK
Sen Emre'nin kişisel danışmanı ve çalışma ortağısın. İş, para, hukuk, sağlık, teknoloji, öğrenme, yazışma, planlama ve günlük kararlar dahil hayatın her alanında o alanın en iyi uzmanı gibi düşünürsün. Amacın soruyu yanıtlamak değil, Emre'yi gerçekten ilerletmek: asıl ihtiyacı görmek, işe yarar sonucu vermek, bir sonraki adımı netleştirmek. Tonun sıcak, sakin ve güven verici olsun.

# NASIL ÇALIŞIRSIN
1. Önce sonuç: ilk cümlede cevabı veya kararı ver, gerekçeyi sonra anlat.
2. Asıl ihtiyacı bul: sorunun altındaki amaca hizmet et. Fark ettiğin kritik bir şey varsa bir iki cümleyle söyle.
3. Yazmadan önce kontrol et: rakamları, çıkarımı, tarihleri ve kodun çalışıp çalışmadığını kendi içinde doğrula. Hata bulursan düzelt, sonra yaz.
4. Önceliklendir: her şeyi sıralama, en önemli bir ila üç şeyi seç. Gerisini yalnızca gerçekten değer katıyorsa ekle.
5. Karar sorularında seçenekleri tart, net bir öneri yap, fikrini neyin değiştireceğini söyle ve uygulanabilir ilk adımı ver.
6. Eksik bilgi varsa makul bir varsayımla ilerle ve varsayımı belirt. Yanlış cevap riski yüksekse yalnızca tek ve kısa bir soru sor.
7. Tutarlı ol: önceki mesajlardaki bilgi ve talimatları unutma, sohbet boyunca aynı çizgide kal.
8. Ölçülü proaktif ol: Emre'nin sormadığı ama bilmesi gereken bir risk veya daha iyi bir yol görürsen kısaca belirt.

# DÜRÜSTLÜK
Uydurma bilgi, rakam, alıntı, kaynak veya bağlantı üretme. Bildiğin, tahmin ettiğin ve bilmediğin şeyi birbirinden ayır, bilmiyorsan bilmiyorum de. Emre yanılıyorsa ya da planında zayıf nokta varsa nazikçe ve gerekçesiyle söyle. Sadece onaylamak için onaylama.

# ALANLARA GÖRE
- Para, hukuk, sağlık: somut bilgi ve hesap ver. Riskleri ve ne zaman uzmana gidilmesi gerektiğini bir kez kısaca belirt, her cümleye uyarı ekleme. Acil tehlikede 112'ye yönlendir.
- Kod: önce kök neden, sonra çalışan ve eksiksiz çözüm. Neyi değiştirdiğini belirt.
- Yazışma: doğrudan kullanılabilir, uygun tonda taslak ver.
- Öğrenme: önce temel mantığı, sonra ayrıntıyı anlat, uygun yerde örnek ver.
- Planlama: somut adımlar, süreler ve öncelik sırası ver.
- Görsel veya PDF: yalnızca gerçekten gördüğün ya da okuduğun şeye dayan, göremediğin kısmı uydurma.
- Aksi belirtilmedikçe Türkiye bağlamını (mevzuat, TL, yerel uygulamalar) varsay.

# İNTERNET
Mesajda arama sonuçları varsa yanıtı yalnızca bunlara ve konuşmaya dayandır; sonuçlarda olmayan bilgiyi ekleme, çelişkide en yeniyi tercih edip çelişkiyi belirt. Zamanla değişen bilgilerde (kur, fiyat, skor, oran, yasa gibi) verinin hangi tarih için geçerli olduğunu kısaca belirt. Tarih kaynakta yoksa tarih uydurma. Kaynak adı veya bağlantı yazma, sistem ekler. Mesaj soru işareti (?) ile bitmiyorsa internete erişimin yoktur: fiyat, kur, haber, skor, hava gibi güncel bilgilerde bilginin eski olabileceğini söyle ve sorunun sonuna soru işareti (?) koyarak aratabileceğini hatırlat. Güncel bilgi gerektirmeyen sorularda bunu yapma.

# BİÇİM
Emre çoğunlukla telefondan okuyor. Basit soruya bir ila üç cümle, orta konuya iki üç kısa paragraf, karmaşık konuya bölümlü yanıt ver. Giriş cümlesi, soruyu tekrar etme, özet tekrarı ve kapanış nezaketi yazma. Başlığı yalnızca çok bölümlü yanıtta "## Kısa başlık" biçiminde kullan, emoji koyma. Tablo kullanma. Kalın yazıyı yalnızca kritik rakam ve uyarılar için kullan. Sıralı adımlarda numaralı, paralel öğelerde kısa maddeli liste kullan. Yanıt sesli okunduğunda da akıcı olsun. Gerçekten değer katıyorsa yanıtı tek bir net sonraki adımla bitir.
"""
}

# Eski varsayilan talimat: Ayarlar'dan "Varsayilana Don + Kaydet" ile diske yazilmis olabilir.
# Bu metin diskte kayitliysa "ozel talimat" sayilmaz, boylece yeni varsayilan otomatik devreye girer.
_LEGACY_DEFAULT_PROMPTS = ("""
# KİMLİK VE AMAÇ
Sen, profesyonel, bilgili ve premium kalitede yanıtlar veren bir yapay zeka asistanısın.

# DİL KURALI (EN ÖNEMLİ KURAL)
SADECE ve SADECE TÜRKÇE yanıt ver. Kesinlikle İngilizce cümle kurma. İstisna: marka/ürün isimleri (örn. iPhone, Google), yaygın teknik kısaltmalar (örn. API, CPU, PDF) ve programlama kodu olduğu gibi bırakılır; bunların dışındaki tüm açıklama ve cümleler Türkçe olmalı.

# DAVRANIŞ KURALLARI
1. Soruyu tam olarak anla, bağlamı doğru yorumla
2. Bilmediğin konuda "yeterli bilgim yok" de
3. Uydurma bilgi, istatistik veya kaynak üretme

# YANIT FORMATI (PREMIUM KALİTE)
1. **BAŞLIKLAR:** Her bölüm için "## Başlık" kullan. Başlık cümle başları büyük harfle başlasın.
2. **İÇERİK:** Başlıktan sonra, 1 satır boşluk bırakarak açıklamaya başla.
3. **MADDELER:** Uzun açıklamaları kısa maddeler halinde özetleme.
4. **TABLO YASAK:** Asla Markdown tablosu kullanma.
5. **BAŞLIK SADELİĞİ:** Başlık satırlarında emoji veya sembol kullanma, sadece düz metin başlık yaz.

# ÖRNEK YANIT FORMATI:
## Nemlendirme ve Cilt Bakımı
Egzama, cildin kurumasına ve tahriş olmasına yol açar. Bu yüzden düzenli nemlendirme kritik öneme sahiptir.

## Topikal Steroid ve İlaçlar
Topikal steroidler, egzama alevlenmelerinde hızlı rahatlama sağlar. Doktor kontrolünde kullanılmalıdır.
""", """
# KİMLİK VE AMAÇ
Sen Emre'nin kişisel danışmanı ve çalışma ortağısın. İş, finans, hukuk, sağlık, teknoloji, eğitim, yazışma, planlama ve günlük yaşam dahil her konuda, o alanın kıdemli bir uzmanı gibi düşün ve konuş. Tonun sıcak, sakin, güven veren ve profesyonel olsun. Amacın sadece soruyu cevaplamak değil, Emre'nin asıl ihtiyacını karşılayıp ona bir sonraki adımı net biçimde göstermektir.

# DİL
Türkçe yaz; akıcı, doğal, imla ve noktalaması doğru bir Türkçe kullan. Marka ve ürün adları, yaygın teknik kısaltmalar (API, PDF, CPU gibi) ve kod olduğu gibi kalır. Emre açıkça başka bir dil isterse veya çeviri yapıyorsan o dili kullan. Yanıtın içine gereksiz İngilizce kelime karıştırma.

# ÇALIŞMA İLKELERİ
1. Önce asıl niyeti anla. Sorunun altındaki gerçek ihtiyacı düşün, önceki mesajlardaki bilgi ve talimatları unutma, tutarlı kal.
2. Önce cevabı ver, sonra gerekçeyi anlat. İlk cümlede sonuca ulaş.
3. Net ol. Sorulan şeyde kaçamak yapma, somut bir öneri veya karar sun. Belirsizlik varsa nedenini ve neye bağlı olduğunu söyle.
4. Doğru ol. Uydurma bilgi, rakam, tarih, alıntı, kaynak veya bağlantı üretme. Emin olmadığın şeyi emin değilim diye belirt, tahmini açıkça tahmin olarak işaretle. Emre yanılıyorsa nazikçe ve gerekçesiyle düzelt; sadece onaylamak için onaylama.
5. Derinliği soruya göre ayarla. Selamlaşma ve basit sorulara kısa ve doğal cevap ver. Karmaşık konularda kapsamlı, ama tekrar ve dolgu içermeyen bir yanıt ver.
6. Eksik bilgi varsa makul bir varsayımla ilerle ve varsayımı belirt. Yanlış cevap verme riski yüksekse yalnızca tek ve kısa bir netleştirme sorusu sor.
7. Karar ve strateji sorularında seçenekleri, artı ve eksileri, riskleri değerlendir; ardından gerekçeli net bir öneri ve uygulanabilir bir sonraki adım ver.
8. Hesap ve mantık gerektiren konularda adım adım ilerle, sonucu vermeden önce rakamları ve çıkarımı kontrol et.
9. Kod yazarken çalışır ve eksiksiz kod ver, dil etiketli kod bloğu kullan, kısaca ne yaptığını açıkla. Hata ayıklarken önce kök nedeni söyle, sonra çözümü ver.
10. E-posta, dilekçe, mesaj gibi metinlerde doğrudan kullanılabilir, uygun tonda ve düzgün biçimlendirilmiş bir taslak ver; gereksiz giriş cümleleri ekleme.
11. Sağlık, hukuk ve finans gibi hassas konularda somut ve bilgilendirici yardım et. Riskleri ve bir uzmana başvurulması gereken durumları kısaca belirt, ama her cümleye uyarı ekleme. Acil bir tehlike varsa net biçimde ilgili acil hatta (112) yönlendir.
12. Görsel veya PDF eklendiyse yalnızca gerçekten içinde gördüğün veya okuduğun şeye dayan; göremediğin kısmı uydurma.
13. Mesajın başında güncel tarih ve saat bilgisi verilir; tarihle ilgili hesaplarda bunu kullan, kendi eski bilginden yıl veya sürüm varsayma. Kullanıcı açıkça sormadıkça yanıtında bugünün tarihini veya saatini yazma, yanıtı tarih/saat satırıyla başlatma.

# İNTERNET ARAMA SONUÇLARI
Mesajda internet arama sonuçları varsa yanıtı yalnızca bu sonuçlara ve konuşmaya dayandır. Rakam, tarih ve özel isimleri kaynaktan doğru aktar, çelişki varsa en yeni olanı tercih edip çelişkiyi kısaca belirt, sonuçlarda olmayan bilgiyi ekleme. Yanıtın içine kaynak adı, bağlantı veya Kaynak başlığı yazma; kaynaklar sistem tarafından otomatik eklenir.
Arama sonuçlarına dayanan ve zamanla değişebilen bilgilerde (rakam, oran, fiyat, kur, skor, enflasyon, faiz, yasa, sürüm, kişinin veya kurumun görevi gibi) verinin hangi tarih için geçerli olduğunu cevabın içinde "3 Ekim itibarıyla" veya "Eylül 2026 verisine göre" biçiminde belirt. Bu, bugünün tarihini yazmama kuralının istisnasıdır: yazılan tarih her zaman bugünün tarihi değil, verinin kaynaktaki tarihidir. Tarih kaynakta yoksa tarih uydurma ve bugünün tarihini veriye atfetme; bu durumda verinin tarihinin belirsiz olduğunu kısaca söyle. Zamandan bağımsız bilgilerde (tanım, tarihçe, nasıl yapılır) veri tarihi yazma.
Kullanıcı mesajı soru işareti (?) ile bitmiyorsa internete erişimin yoktur. Fiyat, kur, haber, skor, hava durumu veya bir kişi ya da kurumun şu anki durumu gibi güncel bilgi gerektiren bir soruda cevabı hafızandan kesin bilgi gibi sunma; bilgin eski olabileceğini kısaca belirt ve kullanıcının sorunun sonuna soru işareti (?) koyarak web'den güncel kaynakları getirtebileceğini söyle. Güncel bilgi gerektirmeyen sorularda bu hatırlatmayı yapma.

# YANIT BİÇİMİ
1. Kısa yanıtlarda başlık kullanma, doğrudan akıcı paragraflarla yaz. Birden çok bölümü olan uzun yanıtlarda her bölüm için "## Başlık" kullan; başlık kısa olsun, yalnızca ilk harf büyük yazılsın, emoji veya sembol içermesin ve başlıktan sonra bir boş satır bırakılsın.
2. Paragrafları kısa tut. Sıralı adımlar için numaralı liste, birbirine paralel öğeler için madde işareti kullan; her madde bir veya iki cümleyi geçmesin. Düz anlatım yeterliyse liste kullanma.
3. Asla Markdown tablosu kullanma. Karşılaştırmaları kısa maddeler veya paragraflarla ver.
4. Kalın yazıyı yalnızca gerçekten kritik terim, rakam veya uyarılar için kullan. Emoji ve süs kullanma.
5. Cümleler yüksek sesle okunduğunda da akıcı olsun.
6. Gereksiz kapanış cümleleri (umarım yardımcı olmuştur gibi), özür, övgü ve kendini tanıtma yapma. Gerçekten değer katıyorsa yanıtı tek bir net sonraki adım veya kısa bir soruyla bitir.

# ÖRNEK BİÇİM (uzun ve çok bölümlü yanıtlar için)
## Nemlendirme ve cilt bakımı
Egzama cildi kurutur ve tahriş eder, bu yüzden düzenli nemlendirme temel adımdır.

## Topikal steroidler
Alevlenmelerde hızlı rahatlama sağlar. Doktor kontrolünde kullanılmalıdır.
""", """
# KİMLİK
Sen Emre'nin kişisel danışmanı ve çalışma ortağısın. İş, para, hukuk, sağlık, teknoloji, öğrenme, yazışma, planlama ve günlük kararlar dahil hayatın her alanında o alanın en iyi uzmanı gibi düşünürsün. Amacın soruyu yanıtlamak değil, Emre'yi gerçekten ilerletmek: asıl ihtiyacı görmek, işe yarar sonucu vermek, bir sonraki adımı netleştirmek. Tonun sıcak, sakin ve güven verici olsun.

# EMRE HAKKINDA
(Buraya kendini yaz: yaşadığın şehir, mesleğin, hedeflerin, ilgi alanların, kullandığın araçlar, sevdiğin anlatım tarzı.) Bu bilgileri yalnızca konuyla ilgiliyse kullan, her yanıtta tekrar etme.

# NASIL ÇALIŞIRSIN
1. Önce sonuç: ilk cümlede cevabı veya kararı ver, gerekçeyi sonra anlat.
2. Asıl ihtiyacı bul: sorunun altındaki amaca hizmet et. Fark ettiğin kritik bir şey varsa bir iki cümleyle söyle.
3. Yazmadan önce kontrol et: rakamları, çıkarımı, tarihleri ve kodun çalışıp çalışmadığını kendi içinde doğrula. Hata bulursan düzelt, sonra yaz.
4. Önceliklendir: her şeyi sıralama, en önemli bir ila üç şeyi seç. Gerisini yalnızca gerçekten değer katıyorsa ekle.
5. Karar sorularında seçenekleri tart, net bir öneri yap, fikrini neyin değiştireceğini söyle ve uygulanabilir ilk adımı ver.
6. Eksik bilgi varsa makul bir varsayımla ilerle ve varsayımı belirt. Yanlış cevap riski yüksekse yalnızca tek ve kısa bir soru sor.
7. Ölçülü proaktif ol: Emre'nin sormadığı ama bilmesi gereken bir risk veya daha iyi bir yol görürsen kısaca belirt.

# DÜRÜSTLÜK
Uydurma bilgi, rakam, alıntı, kaynak veya bağlantı üretme. Bildiğin, tahmin ettiğin ve bilmediğin şeyi birbirinden ayır, bilmiyorsan bilmiyorum de. Emre yanılıyorsa ya da planında zayıf nokta varsa nazikçe ve gerekçesiyle söyle. Sadece onaylamak için onaylama.

# ALANLARA GÖRE
- Para, hukuk, sağlık: somut bilgi ve hesap ver. Riskleri ve ne zaman uzmana gidilmesi gerektiğini bir kez kısaca belirt, her cümleye uyarı ekleme. Acil tehlikede 112'ye yönlendir.
- Kod: önce kök neden, sonra çalışan ve eksiksiz çözüm. Neyi değiştirdiğini belirt.
- Yazışma: doğrudan kullanılabilir, uygun tonda taslak ver.
- Öğrenme: önce temel mantığı, sonra ayrıntıyı anlat, uygun yerde örnek ver.
- Planlama: somut adımlar, süreler ve öncelik sırası ver.
- Aksi belirtilmedikçe Türkiye bağlamını (mevzuat, TL, yerel uygulamalar) varsay.

# İNTERNET
Mesajda arama sonuçları varsa yanıtı yalnızca bunlara ve konuşmaya dayandır; sonuçlarda olmayan bilgiyi ekleme, çelişkide en yeniyi tercih edip çelişkiyi belirt. Kaynak adı veya bağlantı yazma, sistem ekler. Mesaj soru işareti (?) ile bitmiyorsa internete erişimin yoktur: fiyat, kur, haber, skor, hava gibi güncel bilgilerde bilginin eski olabileceğini söyle ve sorunun sonuna soru işareti (?) koyarak aratabileceğini hatırlat. Güncel bilgi gerektirmeyen sorularda bunu yapma.

# BİÇİM
Emre çoğunlukla telefondan okuyor. Basit soruya bir ila üç cümle, orta konuya iki üç kısa paragraf, karmaşık konuya bölümlü yanıt ver. Giriş cümlesi, soruyu tekrar etme, özet tekrarı ve kapanış nezaketi yazma. Başlığı yalnızca çok bölümlü yanıtta "## Kısa başlık" biçiminde kullan, emoji koyma. Tablo kullanma. Kalın yazıyı yalnızca kritik rakam ve uyarılar için kullan. Sıralı adımlarda numaralı, paralel öğelerde kısa maddeli liste kullan. Yanıt sesli okunduğunda da akıcı olsun. Gerçekten değer katıyorsa yanıtı tek bir net sonraki adımla bitir.
""")

# ============================================
# KULLANICI AYARLARI (sistem promptu / model bazli yaraticilik) - arayuzden degistirilebilir
# ============================================
SETTINGS_FILE = os.path.expanduser('~/ai_settings.json')
settings_lock = threading.Lock()

# Her modelin kod icindeki (get_generation_params) taban sicaklik degeri burada da
# tekrarlanir; boylece Ayarlar panelinde "varsayilan" olarak dogru deger gosterilir.
MODEL_TEMP_DEFAULTS = {
    'openai/gpt-oss-20b':  0.5,
    'qwen/qwen3.8-27b':    0.3,
    'openai/gpt-oss-120b': 0.2,
}
MODEL_DISPLAY_NAMES = {
    'openai/gpt-oss-20b':  'GPT-OSS 20B',
    'qwen/qwen3.8-27b':    'Qwen3.8 27B',
    'openai/gpt-oss-120b': 'GPT-OSS 120B',
}

def load_settings():
    data = {'system_prompt': None, 'temperatures': {}}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    if isinstance(saved.get('system_prompt'), str) and saved['system_prompt'].strip():
                        sp_saved = saved['system_prompt'].strip()
                        # Eski varsayilan metin ya da guncel varsayilanin kendisi "ozel talimat" degildir.
                        if sp_saved not in [x.strip() for x in _LEGACY_DEFAULT_PROMPTS] and sp_saved != DEFAULT_SYSTEM_MSG['content'].strip():
                            data['system_prompt'] = saved['system_prompt']
                    temps = saved.get('temperatures')
                    if isinstance(temps, dict):
                        for model_id, val in temps.items():
                            if model_id in MODEL_TEMP_DEFAULTS and isinstance(val, (int, float)):
                                data['temperatures'][model_id] = max(0.0, min(1.0, float(val)))
        except:
            pass
    return data

def save_settings(s):
    atomic_write_json(SETTINGS_FILE, s, indent=2)

app_settings = load_settings()

def get_active_system_prompt():
    """Kullanici ozel bir sistem promptu kaydettiyse onu, kaydetmediyse varsayilani dondurur."""
    with settings_lock:
        sp = app_settings.get('system_prompt')
    return sp if sp else DEFAULT_SYSTEM_MSG['content']

ZORUNLU_BICIM_KURALLARI = """

# DEĞİŞMEZ KURALLAR (başka her talimattan önceliklidir)
1. Yanıtı her zaman yalnızca Türkçe yaz. Başka bir dilde cevap verme, yanıtın içine yabancı dilde cümle, kelime veya ifade karıştırma. Arama sonuçları veya kaynaklar İngilizce ya da başka dilde olsa bile bilgiyi Türkçeye çevirerek aktar. Yalnızca kullanıcı açıkça çeviri veya başka bir dilde metin isterse o dili kullan. Marka, ürün ve site adları, yaygın teknik kısaltmalar (USD, API, PDF gibi) ve kod değişmeden kalabilir.
2. Tarihleri her zaman Türkçe yaz: gün, Türkçe ay adı, yıl (örneğin 3 Ekim 2026). Kullanıcı sormadıkça güncel tarih ve saati yanıtına ekleme. Ay ve gün adlarını (Oct, October, Mon, Saturday gibi) asla İngilizce yazma; kaynak İngilizce tarih verse bile Türkçeye çevir.
3. Yanıtta uzun tire (—) ve kısa tire (–) karakterlerini asla kullanma. Bunların yerine virgül, iki nokta, parantez veya yeni cümle kullan. Sayı aralıklarında normal kısa çizgi (10-20) ya da "ile" kullan.
"""

def get_active_system_message():
    # Degismez kurallar, kullanici ozel sistem promptu kaydetmis olsa bile HER ZAMAN eklenir.
    # Sabit metin oldugu icin prompt onbellegi (prefix cache) bozulmaz.
    return {'role': 'system', 'content': get_active_system_prompt().rstrip() + ZORUNLU_BICIM_KURALLARI}

# ============================================
# HAFIZA YÖNETİMİ (SOHBET BAZLI / PER-CHAT)
# ============================================
# ONEMLI: Hafiza artik TEK bir global liste degil, chat_id -> mesaj listesi
# seklinde bir SOZLUK (dict). Boylece frontend'deki her sohbet, sunucuda da
# kendi izole gecmisine sahip olur; bir sohbetteki konusma baska bir sohbetin
# baglamina (context) hicbir sekilde karismaz. Sistem mesaji (system prompt)
# ARTIK bu listelerin icinde SAKLANMAZ - her istekte get_active_system_message()
# ile TAZE olarak eklenir (bkz. /chat route). Bu sayede:
#   - Ayarlar'dan sistem promptu degistirildiginde tum sohbetleri tek tek
#     guncelleme derdi kalmaz (otomatik olarak hepsine yansir).
#   - Ozetleme (condense) mantigi index kaymasi riski olmadan calisir.
def load_all_histories():
    """Diskten {chat_id: [mesajlar]} sozlugunu yukler. Eski (tek-liste, global
    gecmis) formatindaki bir dosya bulunursa, veri kaybolmasin diye bunu
    'legacy' adli tek bir sohbete tasir (bir kerelik gecis/migrasyon)."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return {str(k): v for k, v in d.items() if isinstance(v, list)}
                if isinstance(d, list) and len(d) > 0:
                    # Eski format: sistem mesaji varsa at, geri kalanini 'legacy'
                    # sohbetine tasi ki gecmis konusma tamamen kaybolmasin.
                    conv = [m for m in d if m.get('role') != 'system']
                    return {'legacy': conv} if conv else {}
        except:
            pass
    return {}

def save_all_histories(d):
    atomic_write_json(HISTORY_FILE, d, indent=4)

def get_chat_history(chat_id):
    with history_lock:
        return list(CHAT_HISTORIES.get(chat_id, []))

def set_chat_history(chat_id, msgs):
    """Bir sohbetin gecmisini gunceller ve HEMEN diske yazar. Cagiran taraf
    history_lock'u ZATEN tutuyor olmali (bu fonksiyon lock almaz, ic ice
    kilitlenmeyi onlemek icin)."""
    CHAT_HISTORIES[chat_id] = msgs
    save_all_histories(CHAT_HISTORIES)

def parse_pdf_bytes(b):
    try:
        r = PdfReader(io.BytesIO(b))
        t = ''
        for p in r.pages:
            ex = p.extract_text()
            if ex:
                t += ex + '\n'
        return t.strip() if t.strip() else 'PDF metni alinamadi.'
    except Exception as e:
        return str(e)

class ContextLengthExceeded(Exception):
    """Groq'un 'context asimi' (400) hatasini normal hatalardan ayirt etmek icin.
    Bu hata yakalanirsa, tahmini karakter/token butcemiz yanilmis demektir ve
    cagiran taraf otomatik olarak daha kucuk parcaya bolme fallback'ine gecer."""
    pass

def _ozet_api_cagir(prompt, model='openai/gpt-oss-20b', max_tokens=1200, temperature=0.2):
    """Tek bir ozetleme istegini mevcut API anahtarlari arasinda sirayla dener.
    Basarili olursa ozet metnini, hicbir anahtar basarili olmazsa None doner.
    Eger API context-asimi (400) hatasi bildirirse ContextLengthExceeded firlatir
    ki cagiran taraf (summarize_long_text) bunu diger hatalardan ayirt edip
    otomatik olarak daha kucuk parcaya bolme fallback'ine gecebilsin.
    (summarize_long_text icindeki tekrarli anahtar-deneme mantiginin ortak fonksiyonu.)"""
    avail_keys = get_available_keys(model)
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
        'max_completion_tokens': max_tokens,
        'temperature': temperature,
    }
    for key in avail_keys:
        try:
            with httpx.Client() as c:
                r = c.post(URL, headers={'Authorization': f'Bearer {key}'}, json=payload, timeout=25)
                if r.status_code == 200:
                    ozet = r.json()['choices'][0]['message']['content'].strip()
                    if ozet:
                        return ozet
                elif r.status_code == 429:
                    cooldown = None
                    ra = r.headers.get('retry-after')
                    if ra:
                        try:
                            cooldown = float(ra)
                        except ValueError:
                            cooldown = None
                    if not cooldown:
                        cooldown = parse_reset_string(r.headers.get('x-ratelimit-reset-requests')) or DEFAULT_COOLDOWN_SECONDS
                    mark_key_cooldown(key, model, cooldown)
                    continue
                elif r.status_code == 400:
                    try:
                        err_body = r.text
                    except Exception:
                        err_body = ''
                    if is_context_length_error(r.status_code, err_body):
                        raise ContextLengthExceeded(err_body[:300])
                    continue
                else:
                    continue
        except ContextLengthExceeded:
            raise
        except Exception:
            continue
    return None

def _ozet_cikti_butcesi(char_len):
    """Ozetleme cikti (max_tokens) butcesini girdi buyuklugune ORANTILI hesaplar.
    Eskiden sabit 1200 idi; simdi buyuk belgelerde ozet asiri sigla kalmasin diye
    girdiyle birlikte buyuyor, ama modelin makul ciktisini asmasin diye 4000 ile sinirli."""
    est = int(1200 * (char_len / 6000))
    return max(1200, min(est, 4000))

def summarize_long_text(text, max_chars=6000):
    """Uzun PDF metinlerini kor kesme yerine, mumkunse akilli ozetleyerek kisaltir.
    Belge, KULLANILAN MODELIN GERCEK CONTEXT PENCERESINE gore hesaplanan guvenli
    sinira (CHUNK_SIZE, get_safe_input_char_budget ile dinamik belirlenir) sigiyorsa
    TEK istekte ozetlenir. Bu sayede sabit/keyfi bir karakter siniri yerine, model
    degisse veya Groq limitleri degisse bile sistem otomatik dogru sinira gore calisir.
    Belge daha uzunsa PARCALARA BOLUNEREK her parca ayri ayri ozetlenir ve birlestirilir;
    boylece belgenin TAMAMI (MAX_CHUNKS sinirina kadar) degerlendirilir, sadece asiri
    uzun belgelerde acik bir uyari ile hangi kismin islenmedigi belirtilir.
    GUVENLIK AGI: tahmini karakter/token hesabimiz yanilip API yine de context-asimi
    (400) hatasi bildirirse (ContextLengthExceeded), otomatik olarak daha kucuk sabit
    bir parca boyutuna dusup coklu-parca yoluna geçilir - kullanici hicbir hata gormez.
    Ozetleme (tum anahtarlar dolu vb. nedenle) hic basarili olmazsa guvenli sekilde
    kor kesmeye/kismi metne duser, veri asla sessizce kaybolmaz."""
    if len(text) <= max_chars:
        return text

    ozet_model = 'openai/gpt-oss-20b'
    CHUNK_SIZE = get_safe_input_char_budget(ozet_model)  # modelin gercek context penceresinden turetilir
    MAX_CHUNKS = 8  # asiri uzun belgelerde makul bir ust sinir (toplam is/maliyet icin)

    if len(text) <= CHUNK_SIZE:
        # --- Tek parcaya sigan belgeler: TEK istekte ozetle ---
        prompt = ('Asagidaki metni, en onemli bilgi ve rakamlari kaybetmeden, '
                   'Turkce olarak kisa ve net maddeler halinde ozetle:\n\n' + text)
        try:
            ozet = _ozet_api_cagir(prompt, model=ozet_model, max_tokens=_ozet_cikti_butcesi(len(text)), temperature=0.2)
        except ContextLengthExceeded:
            # Tahmini butce beklenenden yanilmis: guvenlik agi olarak daha kucuk
            # sabit bir parca boyutuna dusup asagidaki coklu-parca yoluna geciyoruz.
            CHUNK_SIZE = 15000
            ozet = None
        else:
            if ozet:
                return f'[PDF Ozeti - orijinal metin uzun oldugu icin ozetlendi]\n{ozet}'
            return text[:max_chars] + '\n\n[NOT: Metin cok uzun oldugu icin kisaltildi, ozetleme su an yapilamadi.]'

    # --- Belge tek parcaya sigmiyor (ya da yukaridaki guvenlik agi tetiklendi):
    #     PARCALARA BOLEREK ozetle (belgenin TAMAMI islenir) ---
    chunks = [text[i:i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)]
    islenen = chunks[:MAX_CHUNKS]
    atlanan = len(chunks) - len(islenen)

    parca_ozetleri = []
    for idx, parca in enumerate(islenen, start=1):
        prompt = (
            f'Asagida uzun bir belgenin {idx}/{len(islenen)}. parcasi var. '
            'Bu parcadaki en onemli bilgi, rakam ve isimleri kaybetmeden, '
            'Turkce olarak kisa ve net maddeler halinde ozetle (en fazla 200 kelime):\n\n' + parca
        )
        try:
            ozet = _ozet_api_cagir(prompt, model=ozet_model, max_tokens=500, temperature=0.2)
        except ContextLengthExceeded:
            # Bu tekil parca da (son derece nadir) context asarsa, veri kaybolmasin
            # diye asagidaki "basarisiz oldu" yoluna dusuruyoruz -> parca kirpilarak eklenir.
            ozet = None
        # Bu parca ozetlenemezse (o an tum anahtarlar dolu, context asimi vb.) parcayi
        # TAMAMEN kaybetmemek icin kor kesilmis halini not duserek ekliyoruz -> veri
        # sessizce hic kaybolmuyor.
        parca_ozetleri.append(ozet if ozet else (parca[:800] + ' [...]'))

    if len(parca_ozetleri) == 1:
        birlesik = parca_ozetleri[0]
    else:
        birlesim_prompt = (
            'Asagida ayni belgenin farkli bolumlerinden cikarilmis kisa ozetler var. '
            'Bunlari, tekrar etmeyen, tutarli, Turkce ve maddeler halinde TEK bir ozette birlestir '
            f'(en fazla {min(2000, 200 * len(parca_ozetleri))} kelime):\n\n' + '\n\n---\n\n'.join(parca_ozetleri)
        )
        try:
            final_ozet = _ozet_api_cagir(birlesim_prompt, model=ozet_model,
                                          max_tokens=_ozet_cikti_butcesi(len(birlesim_prompt)), temperature=0.2)
        except ContextLengthExceeded:
            # Birlestirme prompt'u bile context'i asarsa (cok fazla parca ozeti birikti),
            # birlestirme adimini atlayip parca ozetlerini dogrudan art arda kullaniyoruz.
            final_ozet = None
        if final_ozet:
            birlesik = final_ozet
        else:
            # Birlestirme adimi basarisiz olsa bile parca ozetleri basliklarla art arda
            # eklenir -> belgenin TAMAMI (ozetlenmis haliyle) yine de kapsanmis olur.
            birlesik = '\n\n'.join(f'[Bolum {i+1}]\n{o}' for i, o in enumerate(parca_ozetleri))

    sonuc = f'[PDF Ozeti - orijinal metin uzun oldugu icin {len(islenen)} parca halinde ozetlendi]\n{birlesik}'
    if atlanan > 0:
        sonuc += (f'\n\n[NOT: Belge cok uzun oldugu icin sadece ilk {len(islenen)} parca '
                  f'(~{len(islenen) * CHUNK_SIZE:,} karakter) islendi, sonraki {atlanan} parca degerlendirilmedi.]')
    return sonuc

# ============================================
# SORU ISARETI (?) KOMUTU (internet aramasinin TEK tetikleyicisi)
# ============================================
# Arama kelime/soru kalibina ya da bir modelin "gerekli mi" kararina BAGLI DEGIL.
# Mesaj soru isareti (?) ile biterse arama ISTISNASIZ yapilir; bitmezse hic yapilmaz.
# Ornek: "Bugun dolar kac TL?"
_SORU_ISARETLERI = ('?', '\uFF1F', '\u061F')  # ASCII, tam genislikte, Arapca soru isareti

def parse_ara_komutu(text):
    """(komut_mu, soru) doner. Mesaj '?' ile bitiyorsa ve '?' disinda icerik varsa komuttur.
    Soru metni degistirilmeden aynen doner."""
    t = (text or '').strip()
    if t.endswith(_SORU_ISARETLERI) and t.strip(''.join(_SORU_ISARETLERI) + ' \t\r\n'):
        return True, t
    return False, t

# ============================================
# TARIH / METIN YARDIMCILARI
# ============================================
_TR_AYLAR = ('Ocak', 'Şubat', 'Mart', 'Nisan', 'Mayıs', 'Haziran', 'Temmuz', 'Ağustos', 'Eylül', 'Ekim', 'Kasım', 'Aralık')
_TR_GUNLER = ('Pazartesi', 'Salı', 'Çarşamba', 'Perşembe', 'Cuma', 'Cumartesi', 'Pazar')

try:
    from zoneinfo import ZoneInfo
    _TR_TZ = ZoneInfo('Europe/Istanbul')
except Exception:
    # tzdata yoksa (Termux, minimal Docker vb.) sabit UTC+3: Turkiye 2016'dan beri yaz/kis saati uygulamiyor.
    _TR_TZ = timezone(timedelta(hours=3))

def _tr_now():
    """Sunucunun saat diliminden bagimsiz, her zaman Turkiye saati (UTC+3)."""
    return datetime.now(_TR_TZ)

def _tr_now_str():
    """'20 Eylül 2026 Pazar, 14:05' biciminde Turkce tarih/saat (locale'den bagimsiz)."""
    lt = _tr_now()
    return f"{lt.day} {_TR_AYLAR[lt.month - 1]} {lt.year} {_TR_GUNLER[lt.weekday()]}, {lt.hour:02d}:{lt.minute:02d}"

def _tr_lower(s):
    return (s or '').replace('İ', 'i').replace('I', 'ı').lower().replace('i̇', 'i')

_STOP_KELIMELER = {
    'bir', 'bu', 'şu', 'su', 'ile', 'için', 'icin', 'ama', 'veya', 'gibi', 'daha', 'çok', 'cok',
    'nedir', 'kimdir', 'nasıl', 'nasil', 'neden', 'kaç', 'kac', 'hangi', 'olan', 'olarak',
    'the', 'and', 'for', 'what', 'how', 'who', 'are', 'was', 'with', 'from', 'that', 'this',
}

def _query_terms(queries):
    terms = []
    for q in queries:
        for w in re.findall(r'\w{3,}', _tr_lower(q)):
            if w not in _STOP_KELIMELER and w not in terms:
                terms.append(w)
    return terms[:24]

# ============================================
# SERPER (GOOGLE SONUCLARI) ANAHTAR YONETIMI
# ============================================
# Anahtar oncelik sirasi: ~/ai_serper_key.json (arayuz/rota ile ayarlanan) -> SERPER_API_KEY
# ortam degiskeni -> bos. Anahtar yoksa Serper sessizce atlanir, diger arama yollari calismaya devam eder.
SERPER_KEY_FILE = os.path.expanduser('~/ai_serper_key.json')
SERPER_URL = 'https://google.serper.dev/search'
SERPER_NEWS_URL = 'https://google.serper.dev/news'
serper_key_lock = threading.Lock()
_DEFAULT_SERPER_API_KEY = ''  # koda gomulu sabit anahtar yok; Ayarlar sekmesinden girilir

def _load_serper_key_from_disk():
    file_key = ''
    if os.path.exists(SERPER_KEY_FILE):
        try:
            with open(SERPER_KEY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get('serper_api_key'), str):
                    file_key = data['serper_api_key'].strip()
        except Exception:
            file_key = ''
    return file_key or (os.environ.get('SERPER_API_KEY') or '').strip() or _DEFAULT_SERPER_API_KEY

_serper_key_value = _load_serper_key_from_disk()

def get_serper_key():
    with serper_key_lock:
        return _serper_key_value

def set_serper_key(new_key):
    global _serper_key_value
    with serper_key_lock:
        new_key = (new_key or '').strip()
        atomic_write_json(SERPER_KEY_FILE, {'serper_api_key': new_key}, indent=2)
        _serper_key_value = new_key or (os.environ.get('SERPER_API_KEY') or '').strip() or _DEFAULT_SERPER_API_KEY

def _mask_key(k):
    k = k or ''
    if len(k) <= 8:
        return '*' * len(k)
    return k[:4] + '…' + k[-4:]

# Saglayici durumlari (teshis rotasi ve log icin)
_provider_status = {}
_provider_status_lock = threading.Lock()

def _note_provider(name, err):
    with _provider_status_lock:
        _provider_status[name] = {'ts': time.time(), 'error': err}
    if err:
        print(f'[arama] {name} hatasi: {err}')

# --- Saglayici sigortasi (circuit breaker) ---
# Bir saglayici ust uste _BREAKER_FAILS aramada gecici hata (zaman asimi, baglanti, 429/5xx)
# verirse _BREAKER_COOLDOWN sn atlanir; boylece her soruda bosuna beklenmez, kredi yanmaz.
# Diger saglayici hazir degilse (anahtar yok / o da acik) HICBIR ZAMAN atlanmaz.
_BREAKER_FAILS = 3
_BREAKER_COOLDOWN = 60.0
_breaker = {}
_breaker_lock = threading.Lock()

def _is_transient_err(err):
    e = (err or '').lower()
    if not e:
        return False
    if 'zaman asimi' in e or 'timeout' in e or 'baglanti hatasi' in e:
        return True
    return bool(re.match(r'http (429|5\d\d)\b', e))

def _breaker_is_open(name):
    with _breaker_lock:
        st = _breaker.get(name)
        return bool(st and st['open_until'] > time.time())

def _breaker_skip(name, other_name, other_configured):
    if not other_configured:
        return False
    if not _breaker_is_open(name):
        return False
    if _breaker_is_open(other_name):
        return False
    return True

def _breaker_report(name, ok):
    with _breaker_lock:
        st = _breaker.setdefault(name, {'fails': 0, 'open_until': 0.0})
        if ok:
            st['fails'] = 0
            st['open_until'] = 0.0
            return
        st['fails'] += 1
        if st['fails'] >= _BREAKER_FAILS:
            st['open_until'] = time.time() + _BREAKER_COOLDOWN
            # yari-acik: sure dolunca tek bir hata sigortayi tekrar acar
            st['fails'] = _BREAKER_FAILS - 1
            print(f'[arama] {name} gecici olarak atlaniyor ({int(_BREAKER_COOLDOWN)}s)')

# ============================================
# ARAMA SONUCU NORMALIZASYONU
# ============================================
def _domain_of(url):
    try:
        return re.sub(r'^www\.', '', urlparse(url).netloc.lower())
    except Exception:
        return ''

# Sosyal medya platformlari arama kaynaklarindan HER konuda cikarilir (tek giris noktasi: _mk_item).
_SOCIAL_BRANDS = frozenset({'instagram', 'facebook', 'tiktok', 'linkedin', 'pinterest', 'snapchat'})
_SOCIAL_DOMAINS = ('twitter.com', 'x.com', 't.co', 'fb.com', 'fb.watch', 'threads.net', 'threads.com',
                   't.me', 'telegram.me', 'wa.me', 'whatsapp.com', 'bsky.app')

def _is_social_url(url):
    """URL bir sosyal medya platformuna (Instagram, X/Twitter, Facebook, TikTok...) aitse True."""
    try:
        host = (urlparse((url or '').strip()).hostname or '').lower().rstrip('.')
        if not host:
            return False
        if any(host == d or host.endswith('.' + d) for d in _SOCIAL_DOMAINS):
            return True
        return any(lab in _SOCIAL_BRANDS for lab in host.split('.'))
    except Exception:
        return False

def _mk_item(title, url, snippet, date, provider, rank, content=''):
    url = (url or '').strip()
    if not url.lower().startswith(('http://', 'https://')):
        return None
    if _is_social_url(url):
        return None
    return {
        'title': (title or '').strip() or _domain_of(url) or url,
        'url': url,
        'snippet': (snippet or '').strip(),
        'content': (content or '').strip(),
        'date': (date or '').strip() if isinstance(date, str) else '',
        'source': _domain_of(url),
        'provider': provider,
        'rank': rank,
        'tscore': 0.0,
        'page': '',
        'page_pub': '',
        'page_mod': '',
        'page_date_src': '',
    }

def _norm_url(u):
    try:
        p = urlparse((u or '').strip())
        host = re.sub(r'^(www\.|m\.|amp\.)', '', p.netloc.lower())
        path = p.path.rstrip('/')
        q = '&'.join(sorted(
            kv for kv in p.query.split('&')
            if kv and not kv.lower().startswith(('utm_', 'fbclid', 'gclid', 'ref=', 'source='))
        ))
        return host + path + (('?' + q) if q else '')
    except Exception:
        return ''

def _recency_bonus(date_str):
    s = (date_str or '').lower()
    if not s:
        return 0.0
    m = re.search(r'(\d+)\s*(dakika|minute|min|saat|hour|gün|gun|day|hafta|week|ay|month|yıl|yil|year)', s)
    if m:
        n = int(m.group(1))
        u = m.group(2)
        if u in ('dakika', 'minute', 'min', 'saat', 'hour'):
            return 0.4
        if u in ('gün', 'gun', 'day'):
            return 0.3 if n <= 7 else 0.15
        if u in ('hafta', 'week'):
            return 0.15 if n <= 4 else 0.05
        return 0.0
    try:
        if str(_tr_now().year) in s:
            return 0.1
    except Exception:
        pass
    return 0.0

# ============================================
# GUNCELLIK: YIL BILINCI (kural tabanli, ek model cagrisi YOK)
# ============================================
# Amac: "Eylül enflasyon" gibi guncel sorularda eski yilin (ornegin 2025) sonuclarinin one gecmesini
# onlemek. Eski yilli sonuclar silinmez, sadece puanla asagi cekilir (yedek olarak kalir).
_YEAR_RE = re.compile(r'(?<!\d)(20[0-3]\d)(?!\d)')
_REL_FRESH_RE = re.compile(r'\d+\s*(dakika|minute|min|saat|hour|gün|gun|day|hafta|week)')
_AY_NO = {_tr_lower(a): i + 1 for i, a in enumerate(_TR_AYLAR)}
_AY_RE = re.compile(r'(?<![a-zçğıöşü])(ocak|şubat|mart|nisan|mayıs|haziran|temmuz|ağustos|eylül|ekim|kasım|aralık)')
_GUNCEL_A_RE = re.compile(
    r'(?<![a-zçğıöşü])(en son|son durum|son dakika|son gelişme\w*|açıklandı|açıkladı|açıklanan|'
    r'bu ay|bu yıl|bu hafta|bugün|dün|şu an\w*|şimdi|güncel\w*)(?![a-zçğıöşü])')
_PIYASA_RE = re.compile(
    r'(?<![a-zçğıöşü])(dolar|euro|sterlin|altın|faiz|enflasyon|borsa|bist|endeks|bitcoin|akaryakıt|'
    r'benzin|motorin|mevduat|işsizlik|büyüme|cari açık|ihracat|ithalat)\w*')
_VERI_RE = re.compile(
    r'(?<![a-zçğıöşü])(kaç|ne kadar|oran\w*|fiyat\w*|kur\w*|değer\w*|durum\w*|son)(?![a-zçğıöşü])')
_ZAMAN_SIRA = ('gun', 'hafta', 'ay', 'yil')

# --- ANLIK VERI (kur, altin, borsa, kripto, akaryakit): eski bilgi kabul edilmez ---
LIVE_MAX_AGE_MIN = 60   # veri saati bundan eskiyse 'guncel' sayilmaz
_ANLIK_RE = re.compile(
    r'(?<![a-zçğıöşü])(dolar|euro|sterlin|parite|altın|çeyrek altın|b[iı]st|borsa|endeks|bitcoin|btc|'
    r'ethereum|kripto|brent|akaryakıt|benzin|motorin|mazot)\w*')
_LIVE_HINT_RE = re.compile(r'(?<![a-zçğıöşü])(bugün|şimdi|şu an\w*|anlık|canlı|güncel\w*|son)(?![a-zçğıöşü])')
_CLOCK_COLON_RE = re.compile(r'(?<![\d.,:])([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?!\d)')
_CLOCK_DOT_RE = re.compile(
    r'(?:saat\s*|güncelleme\s*:?\s*)([01]?\d|2[0-3])\.([0-5]\d)(?!\d)'
    r'|(?<![\d.,])([01]?\d|2[0-3])\.([0-5]\d)(?!\d)\s*(?:itibar)')

def _is_live_query(queries):
    """Kur/altin/borsa gibi ANLIK veri soruluyorsa True. Sorguda bugunden farkli bir yil varsa False."""
    try:
        cur = _tr_now().year
        for q in queries or []:
            t = _tr_lower(q or '')
            if not t or not _ANLIK_RE.search(t):
                continue
            if any(int(y) != cur for y in _YEAR_RE.findall(t)):
                continue
            if _VERI_RE.search(t) or _LIVE_HINT_RE.search(t):
                return True
        return False
    except Exception:
        return False

def _clock_age_min(text):
    """Metinde gecen EN YENI saat damgasinin (bugun, simdiden ileri olmayan) kac dakika once oldugunu doner;
    damga yoksa None. Doner: (yas_dakika, 'SS:DD') | (None, None)."""
    try:
        if not text:
            return None, None
        now = _tr_now()
        now_min = now.hour * 60 + now.minute
        best = None
        t = _tr_lower(text[:6000])
        stamps = [(m.group(1), m.group(2)) for m in _CLOCK_COLON_RE.finditer(t)]
        for m in _CLOCK_DOT_RE.finditer(t):
            stamps.append((m.group(1) or m.group(3), m.group(2) or m.group(4)))
        for h, mi in stamps:
            v = int(h) * 60 + int(mi)
            if v <= now_min and (best is None or v > best):
                best = v
        if best is None:
            return None, None
        return now_min - best, f'{best // 60:02d}:{best % 60:02d}'
    except Exception:
        return None, None

def _fmt_age(mins):
    if mins is None:
        return ''
    if mins < 1:
        return 'şimdi'
    if mins < 60:
        return f'{mins} dk önce'
    return f'{mins // 60} sa {mins % 60} dk önce' if mins % 60 else f'{mins // 60} sa önce'

def _live_filter(results):
    """Anlik veri sorgusunda sonuclara 'live_age'/'live_clock' yazar; veri saati ESKI olanlari cikarir
    (en az 2 guncel/damgasiz kaynak kaliyorsa), yoksa eski olanlari isaretleyip sona atar.
    Doner: (sonuclar, hepsi_eski_mi). ASLA exception firlatmaz."""
    try:
        fresh, unk, old = [], [], []
        for r in results:
            nr = dict(r)
            txt = nr.get('page') or ((nr.get('content') or '') + ' ' + (nr.get('snippet') or ''))
            age, clk = _clock_age_min(txt)
            nr['live_age'], nr['live_clock'] = age, clk
            if age is None:
                unk.append(nr)
            elif age <= LIVE_MAX_AGE_MIN:
                fresh.append(nr)
            else:
                nr['live_stale'] = True
                old.append(nr)
        good = fresh + unk
        if len(good) >= 2:
            if old:
                print(f'[arama] anlik veri: veri saati eski {len(old)} sonuc cikarildi')
            return good, False
        if good:
            return good + old, False
        return old, bool(old)
    except Exception as e:
        print(f'[arama] anlik veri filtresi atlandi: {type(e).__name__}: {e}')
        return results, False

def _fresh_rules(text):
    """Soru guncellik istiyorsa {'ekle': 'Eylül 2026' gibi sorgu eki, 'zaman': filtre|None} doner; yoksa None.
    Kullanici kendisi yil yazdiysa ya da bugunden SONRAKI bir ay adi verdiyse (belirsiz) dokunmaz."""
    t = _tr_lower(text or '')
    if not t.strip() or _YEAR_RE.search(t):
        return None
    now = _tr_now()
    months = [_AY_NO[m] for m in _AY_RE.findall(t) if m in _AY_NO]
    if any(m > now.month for m in months):
        return None
    a = _GUNCEL_A_RE.search(t)
    b = _PIYASA_RE.search(t) and _VERI_RE.search(t)
    if not (months or a or b):
        return None
    zaman = None
    if months:
        zaman = 'yil'  # adi gecen ay gecen aylardan biri olabilir; yil siniri 2025 sonuclarini eler
    elif a:
        w = a.group(1)
        if w.startswith('son dakika'):
            zaman = 'gun'
        elif w in ('bugün', 'dün', 'şimdi', 'bu hafta') or w.startswith('şu an'):
            zaman = 'hafta'
        elif w in ('bu ay', 'son durum', 'açıklandı', 'açıkladı', 'açıklanan') or w.startswith('son gelişme'):
            zaman = 'ay'
        else:
            zaman = 'yil'  # en son, bu yil, guncel
    ekle = []
    if not months and re.search(r'(?<![a-zçğıöşü])bu ay(?![a-zçğıöşü])', t):
        ekle.append(_TR_AYLAR[now.month - 1])
    ekle.append(str(now.year))
    return {'ekle': ' '.join(ekle), 'zaman': zaman}

def _apply_fresh_rules(plan, text):
    """Plana yil/ay eki ve (bos ise) zaman filtresi ekler, plan['guncel']=True yapar.
    Modelin yazdigi yil/zaman degerlerini ezmez. ASLA exception firlatmaz."""
    try:
        if not isinstance(plan, dict):
            return plan
        qs = [q.strip() for q in (plan.get('queries') or []) if isinstance(q, str) and q.strip()]
        rules = _fresh_rules(text)
        if not qs or not rules:
            return plan
        new = dict(plan)
        new['guncel'] = True
        if not new.get('zaman') and rules['zaman']:
            new['zaman'] = rules['zaman']
        if _is_live_query(qs + [text or '']):
            new['zaman'] = 'gun'  # kur/altin/borsa: yalnizca son 24 saat
        if not any(_YEAR_RE.search(q) for q in qs):
            orig = (text or '').strip()[:300]
            qs[0] = (qs[0][:280] + ' ' + rules['ekle']).strip()
            if orig and len(qs) < SEARCH_MAX_QUERIES and orig.lower() not in [x.lower() for x in qs]:
                qs.append(orig)  # kullanicinin kendi cumlesi yedek olarak kalir
        new['queries'] = qs[:SEARCH_MAX_QUERIES]
        return new
    except Exception as e:
        print(f'[arama] guncellik kurali atlandi: {type(e).__name__}: {e}')
        return plan

def _item_years(item):
    """Sonucun baslik/URL/ozet/tarih alanlarindan gecen yillar (2000..gelecek yil)."""
    try:
        cur = _tr_now().year
        txt = ' '.join([item.get('title') or '', item.get('url') or '',
                        (item.get('snippet') or '')[:400], item.get('date') or ''])
        ys = {int(y) for y in _YEAR_RE.findall(txt)}
        ys = {y for y in ys if 2000 <= y <= cur + 1}
        if _REL_FRESH_RE.search((item.get('date') or '').lower()):
            ys.add(cur)  # '3 saat once', '2 gun once' gibi goreli tarih = taze
        return ys
    except Exception:
        return set()

def _year_adjust(item, cur):
    """(puan_degisimi, eski_mi). Yil bilgisi yoksa notr; ocak ayinda gecen yil eski sayilmaz."""
    ys = _item_years(item)
    if not ys:
        return 0.0, False
    if cur in ys or (cur + 1) in ys:
        return 0.25, False
    gap = cur - max(ys)
    if gap == 1 and _tr_now().month == 1:
        return 0.0, False
    return (-0.7 if gap == 1 else -1.0), True

def _stale_top(results, guncel):
    """Guncel soruda ilk 3 sonuctan biri eski yilliysa ve ilk 5 icinde guncel yila ait kanit yoksa True."""
    try:
        if not guncel:
            return False
        cur = _tr_now().year
        top = results[:5]
        if not any(r.get('stale') for r in top[:3]):
            return False
        return not any((cur in _item_years(r)) for r in top)
    except Exception:
        return False

# ============================================
# SERPER: GERCEK GOOGLE SONUCLARI
# ============================================
_SERPER_TBS = {'gun': 'qdr:d', 'hafta': 'qdr:w', 'ay': 'qdr:m', 'yil': 'qdr:y'}

# Kalici baglanti havuzu (arama saglayicilari): her istekte yeni TLS el sikismasi yapilmaz.
_SEARCH_CLIENT = httpx.Client(limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=30.0))

def _search_post(url, headers, body, timeout):
    """Ortak client ile POST. Yalnizca istek sunucuya ULASMADAN kopan durumlarda (baglanti kurulamadi,
    bos kalmis eski baglanti) bir kez daha dener; okuma zaman asiminda ASLA tekrar denemez (kredi korunur)."""
    last = None
    for attempt in range(2):
        try:
            return _SEARCH_CLIENT.post(url, headers=headers, json=body, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
            last = e
            if attempt == 0:
                time.sleep(0.2)
    raise last

def _serper_post(endpoint, payload, key):
    """(json | None, hata_metni | None) doner. ASLA exception firlatmaz."""
    try:
        r = _search_post(endpoint, {'X-API-KEY': key, 'Content-Type': 'application/json'},
                         payload, httpx.Timeout(6.0, connect=5.0))
    except Exception as e:
        return None, f'baglanti hatasi: {type(e).__name__}'
    if r.status_code != 200:
        body = ''
        try:
            body = (r.text or '')[:120]
        except Exception:
            pass
        return None, f'HTTP {r.status_code} {body}'.strip()
    try:
        return r.json(), None
    except Exception:
        return None, 'gecersiz JSON'

def _serper_extras(data):
    """Google'in 'one cikan cevap' ve 'bilgi kutusu' bloklarini kisa metne cevirir."""
    out = []
    ab = data.get('answerBox') or {}
    if isinstance(ab, dict):
        txt = (ab.get('answer') or ab.get('snippet') or '').strip()
        if not txt and isinstance(ab.get('snippetHighlighted'), list):
            txt = ' '.join(str(x) for x in ab['snippetHighlighted']).strip()
        if txt and not _is_social_url(ab.get('link') or ''):
            src = _domain_of(ab.get('link') or '')
            out.append('Google öne çıkan cevap: ' + txt[:600] + (f' (site: {src})' if src else ''))
    kg = data.get('knowledgeGraph') or {}
    if isinstance(kg, dict) and (kg.get('title') or kg.get('description')):
        parts = [str(kg.get('title') or '').strip()]
        if kg.get('type'):
            parts.append(f"({kg.get('type')})")
        if kg.get('description'):
            parts.append(': ' + str(kg.get('description')).strip())
        attrs = kg.get('attributes')
        if isinstance(attrs, dict) and attrs:
            parts.append(' | ' + '; '.join(f'{k}: {v}' for k, v in list(attrs.items())[:8]))
        out.append(('Google bilgi kutusu: ' + ' '.join(parts))[:700])
    return out

def _serper_items(data, haber):
    items = []
    rows = (data.get('news') if haber else data.get('organic')) or []
    for i, it in enumerate(rows):
        if not isinstance(it, dict):
            continue
        mk = _mk_item(it.get('title'), it.get('link'), it.get('snippet'), it.get('date'), 'google', i)
        if mk:
            if haber and it.get('source'):
                mk['source'] = str(it['source'])[:60] or mk['source']
            items.append(mk)
    return items

def _serper_fetch(query, zaman=None, haber=False, num=8):
    """(items, extras, hata) doner. Zaman filtresi sonuc bulamazsa filtresiz tekrar dener;
    haber modu bos donerse normal aramaya duser."""
    key = get_serper_key()
    if not key:
        return [], [], 'anahtar yok'
    payload = {'q': (query or '')[:300], 'gl': 'tr', 'hl': 'tr', 'num': num}
    tbs = _SERPER_TBS.get(zaman)
    if tbs:
        payload['tbs'] = tbs
    use_news = bool(haber)
    endpoint = SERPER_NEWS_URL if use_news else SERPER_URL
    data, err = _serper_post(endpoint, payload, key)
    items = _serper_items(data, use_news) if data is not None else []
    if data is not None and not items and tbs:
        payload.pop('tbs', None)  # zaman filtresi fazla dar kaldi
        data, err = _serper_post(endpoint, payload, key)
        items = _serper_items(data, use_news) if data is not None else []
    if data is not None and not items and use_news:
        use_news = False  # haber bulunamadi -> genel Google aramasi
        payload.pop('tbs', None)
        data, err = _serper_post(SERPER_URL, payload, key)
        items = _serper_items(data, False) if data is not None else []
    if data is None:
        return [], [], err
    extras = _serper_extras(data) if not use_news else []
    return items, extras, None

# ============================================
# TAVILY (derin arama - kaynak basina birden fazla parca)
# ============================================
_TAVILY_TIME = {'gun': 'day', 'hafta': 'week', 'ay': 'month', 'yil': 'year'}

def _tavily_fetch(query, zaman=None, haber=False, derin=True):
    """(items, hata) doner. ASLA exception firlatmaz."""
    key = get_tavily_key()
    if not key:
        return [], 'anahtar yok'
    # Gercek zamanli tek veri (kur, puan, skor...) icin 'advanced' gereksiz: yavas ve 2 kat kredi.
    if derin and zaman == 'gun' and not haber and len((query or '').split()) <= 8:
        derin = False
    _tmo = httpx.Timeout(7.0 if derin else 5.0, connect=5.0)
    payload = {
        'api_key': key,
        'query': (query or '')[:400],
        'search_depth': 'advanced' if derin else 'basic',
        'max_results': 6,
        'include_answer': False,  # Tavily'nin kendi mini ozeti hatali/eski olabiliyor
        'topic': 'news' if haber else 'general',
    }
    if derin:
        payload['chunks_per_source'] = 3
    tr = _TAVILY_TIME.get(zaman)
    if tr:
        payload['time_range'] = tr
    headers = {'Authorization': f'Bearer {key}'}
    try:
        r = _search_post(TAVILY_URL, headers, payload, _tmo)
        if r.status_code in (400, 422):
            # Desteklenmeyen bir parametre olabilir -> sade istekle tekrar dene
            minimal = {'api_key': key, 'query': payload['query'], 'search_depth': 'basic', 'max_results': 6}
            r = _search_post(TAVILY_URL, headers, minimal, _tmo)
    except Exception as e:
        return [], f'baglanti hatasi: {type(e).__name__}'
    if r.status_code != 200:
        body = ''
        try:
            body = (r.text or '')[:120]
        except Exception:
            pass
        return [], f'HTTP {r.status_code} {body}'.strip()
    try:
        data = r.json()
    except Exception:
        return [], 'gecersiz JSON'
    items = []
    for i, it in enumerate(data.get('results') or []):
        if not isinstance(it, dict):
            continue
        mk = _mk_item(it.get('title'), it.get('url'), '', it.get('published_date'), 'tavily', i,
                      content=(it.get('content') or ''))
        if mk:
            try:
                mk['tscore'] = float(it.get('score') or 0.0)
            except Exception:
                mk['tscore'] = 0.0
            items.append(mk)
    return items, None

# ============================================
# SAYFA OKUYUCU (bagimlilik gerektirmez: httpx + html.parser)
# ============================================
_NO_FETCH_DOMAINS = (
    'youtube.com', 'youtu.be', 'twitter.com', 'x.com', 'facebook.com', 'instagram.com',
    'tiktok.com', 'linkedin.com', 'reddit.com', 'pinterest.com',
)
_NO_FETCH_EXT = ('.pdf', '.zip', '.rar', '.mp4', '.mp3', '.jpg', '.jpeg', '.png', '.gif',
                 '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx')
_BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/124.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.5',
    'Accept-Language': 'tr-TR,tr;q=0.9,en;q=0.7',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
}

def _ip_is_private(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        return False

def _is_fetchable(url):
    """Sunucu tarafinda sayfa cekmeden once guvenlik/uygunluk kontrolu (SSRF korumasi dahil)."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False
        host = (p.hostname or '').lower()
        if not host or host == 'localhost' or host.endswith(('.local', '.internal', '.localhost')):
            return False
        if _ip_is_private(host):
            return False
        if any(host == d or host.endswith('.' + d) for d in _NO_FETCH_DOMAINS):
            return False
        if p.path.lower().endswith(_NO_FETCH_EXT):
            return False
        try:
            for info in socket.getaddrinfo(host, None):
                if _ip_is_private(info[4][0]):
                    return False
        except Exception:
            return False
        return True
    except Exception:
        return False

class _ReadableParser(HTMLParser):
    _SKIP = {'script', 'style', 'noscript', 'svg', 'nav', 'footer', 'aside', 'iframe', 'template'}
    _BOUNDARY = {'p', 'li', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'dd', 'dt', 'figcaption',
                 'div', 'br', 'section', 'article', 'tr', 'ul', 'ol', 'table', 'main'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.cur = []
        self.blocks = []

    def _flush(self):
        if self.cur:
            t = re.sub(r'\s+', ' ', ' '.join(self.cur)).strip()
            # Kur/fiyat/skor gibi tablo satirlari kisadir ama rakam icerir; bunlar atilmaz.
            if len(t) >= 40 or (len(t) >= 8 and re.search(r'\d', t)):
                self.blocks.append(t)
            self.cur = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skip_depth = min(self.skip_depth + 1, 50)
        elif tag in self._BOUNDARY and not self.skip_depth:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in self._BOUNDARY and not self.skip_depth:
            self._flush()

    def handle_data(self, data):
        if self.skip_depth:
            return
        s = data.strip()
        if s:
            self.cur.append(s)

def _html_to_blocks(html_text):
    p = _ReadableParser()
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        pass
    p._flush()
    return p.blocks

def _pick_relevant(blocks, terms, max_chars):
    """Sayfa paragraflarindan sorguyla en cok ortusenleri (sirayi bozmadan) secer."""
    if not blocks:
        return ''
    scored = []
    for i, b in enumerate(blocks[:250]):
        bl = _tr_lower(b)
        hits = sum(1 for t in terms if t in bl)
        if hits and re.search(r'\d', b):
            hits += 0.5  # sayisal veri (fiyat, kur, skor) iceren ilgili satir one cikar
        scored.append((hits, i, b))
    top = sorted(scored, key=lambda x: (-x[0], x[1]))[:14]
    chosen = [x for x in top if x[0] > 0] or sorted(top, key=lambda x: x[1])[:6]
    chosen.sort(key=lambda x: x[1])
    out, n = [], 0
    for _, _, b in chosen:
        room = max_chars - n
        if room <= 0:
            break
        out.append(b[:room])
        n += min(len(b), room) + 1
    return ' '.join(out).strip()

def _fetch_page_ex(url, terms, max_chars=None, timeout=6.0):
    """Sayfayi indirip (ilgili_paragraflar, tarih_sozlugu) dondurur. Herhangi bir sorunda ('', {}) doner."""
    max_chars = max_chars or SEARCH_PAGE_CHARS
    if not _is_fetchable(url):
        return '', {}
    try:
        with httpx.Client(follow_redirects=True, headers=_BROWSER_HEADERS) as c:
            with c.stream('GET', url, timeout=timeout) as r:
                if r.status_code != 200:
                    return '', {}
                ctype = (r.headers.get('content-type') or '').lower()
                if ctype and 'html' not in ctype and 'text' not in ctype:
                    return '', {}
                try:
                    if not _is_fetchable(str(r.url)):
                        return '', {}
                except Exception:
                    pass
                buf = bytearray()
                _t_dl = time.time()
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > 400_000 or time.time() - _t_dl > timeout * 1.5:
                        break
                enc = None
                m = re.search(r'charset=([\w\-]+)', ctype)
                if m:
                    enc = m.group(1)
                if not enc:
                    m = re.search(rb'charset=["\']?([\w\-]+)', bytes(buf[:3000]), re.IGNORECASE)
                    if m:
                        enc = m.group(1).decode('ascii', errors='ignore')
        try:
            html_text = bytes(buf).decode(enc or 'utf-8', errors='replace')
        except LookupError:
            html_text = bytes(buf).decode('utf-8', errors='replace')
    except Exception:
        return '', {}
    try:
        dates = _extract_page_dates(html_text)  # head/script temizlenmeden ONCE
    except Exception:
        dates = {}
    html_text = re.sub(r'(?is)<(script|style|noscript|svg|template|iframe|head)\b.*?</\1\s*>', ' ', html_text)
    html_text = re.sub(r'(?s)<!--.*?-->', ' ', html_text)
    return _pick_relevant(_html_to_blocks(html_text), terms, max_chars), dates

def _fetch_page_text(url, terms, max_chars=None, timeout=6.0):
    """Geriye uyumluluk: yalnizca metin dondurur."""
    return _fetch_page_ex(url, terms, max_chars, timeout)[0]

# ============================================
# SAYFA OKUMA YEDEGI (JavaScript ile yuklenen / bos gelen sayfalar icin)
# ============================================
# httpx + html.parser JavaScript calistirmaz; borsa, skor ve bircok haber sitesinde metin bos kalir.
# Ilk dalgada yeterli metin (SEARCH_PAGE_MIN_CHARS) alinamayan en iyi kaynaklar icin ikinci bir dalga
# calisir: once Serper scrape, olmazsa Tavily extract. Yedek mevcut akisi ASLA bozmaz: anahtar eksigi,
# hata, zaman asimi, bos ya da engel sayfasi yanitinda sonuc eskisi gibi kalir. Ilk dalgada yeterli
# sayfa okunduysa hic calismaz; boylece normal aramalar ek sure ve kredi harcamaz.
SERPER_SCRAPE_URL = 'https://scrape.serper.dev'
TAVILY_EXTRACT_URL = 'https://api.tavily.com/extract'

_MD_IMG_RE = re.compile(r'!\[[^\]]*\]\([^)]*\)')
_MD_LINK_RE = re.compile(r'\[([^\]]*)\]\([^)]*\)')
_MD_LEAD_RE = re.compile(r'^\s*(?:#{1,6}\s+|[>*\u2022]\s+|-\s+)')
_PLAIN_URL_RE = re.compile(r'https?://\S+')
_BLOCKED_PAGE_RE = re.compile(
    r'(?i)(enable javascript|javascript (?:is )?(?:disabled|required)|just a moment|checking your browser|'
    r'access denied|verify you are (?:a )?human|are you a robot|captcha|güvenlik doğrulaması|'
    r'tarayıcınızı kontrol|robot olmadığınızı)')

def _fallback_blocks(raw):
    """Duz metin/markdown yaniti, _pick_relevant'in bekledigi paragraf listesine cevirir."""
    s = (raw or '')[:300000]
    s = _MD_IMG_RE.sub(' ', s)
    s = _MD_LINK_RE.sub(r'\1', s)
    s = _PLAIN_URL_RE.sub(' ', s)
    blocks = []
    for line in s.split('\n'):
        t = _MD_LEAD_RE.sub('', line)
        t = t.replace('**', '').replace('__', '').replace('|', ' ')
        t = re.sub(r'\s+', ' ', t).strip()
        # HTML okuyucuyla ayni kural: kisa ama rakam iceren tablo satirlari atilmaz.
        if len(t) >= 40 or (len(t) >= 8 and re.search(r'\d', t)):
            blocks.append(t)
    return blocks

def _scrape_serper(url, timeout):
    """(metin, metadata) doner. ASLA exception firlatmaz."""
    key = get_serper_key()
    if not key:
        return '', None
    try:
        with httpx.Client() as c:
            r = c.post(SERPER_SCRAPE_URL, headers={'X-API-KEY': key, 'Content-Type': 'application/json'},
                       json={'url': url}, timeout=timeout)
        if r.status_code != 200:
            print(f'[arama] sayfa yedegi (serper scrape) HTTP {r.status_code}')
            return '', None
        data = r.json()
        if not isinstance(data, dict):
            return '', None
        txt = data.get('text') or data.get('markdown') or ''
        return (txt if isinstance(txt, str) else ''), data.get('metadata')
    except Exception as e:
        print(f'[arama] sayfa yedegi (serper scrape) hatasi: {type(e).__name__}')
        return '', None

def _extract_tavily(url, timeout):
    """Metin doner. ASLA exception firlatmaz."""
    key = get_tavily_key()
    if not key:
        return ''
    headers = {'Authorization': f'Bearer {key}'}
    try:
        with httpx.Client() as c:
            r = c.post(TAVILY_EXTRACT_URL, headers=headers,
                       json={'urls': [url], 'extract_depth': 'advanced'}, timeout=timeout)
            if r.status_code in (400, 422):
                r = c.post(TAVILY_EXTRACT_URL, headers=headers, json={'urls': [url]}, timeout=timeout)
        if r.status_code != 200:
            print(f'[arama] sayfa yedegi (tavily extract) HTTP {r.status_code}')
            return ''
        data = r.json()
        rows = data.get('results') if isinstance(data, dict) else None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            txt = rows[0].get('raw_content') or ''
            return txt if isinstance(txt, str) else ''
    except Exception as e:
        print(f'[arama] sayfa yedegi (tavily extract) hatasi: {type(e).__name__}')
    return ''

def _meta_dates(meta):
    """Scrape yanitindaki metadata sozlugunden yayin/guncelleme tarihi cikarir (mevcut tarih cikaricisiyla)."""
    try:
        if not isinstance(meta, dict):
            return {}
        parts = []
        for k, v in list(meta.items())[:60]:
            if isinstance(k, str) and isinstance(v, str) and v and len(v) < 80:
                parts.append('<meta property="%s" content="%s">' % (k.replace('"', ''), v.replace('"', '')))
        if not parts:
            return {}
        d = _extract_page_dates('\n'.join(parts))
        return d if (d.get('pub') or d.get('mod')) else {}
    except Exception:
        return {}

def _fetch_page_fallback(url, terms, max_chars=None):
    """Yedek okuma: (ilgili_paragraflar, tarih_sozlugu). Herhangi bir sorunda ('', {}) doner."""
    try:
        max_chars = max_chars or SEARCH_PAGE_CHARS
        t0 = time.time()
        raw, meta = _scrape_serper(url, 3.0)
        if not raw.strip():
            meta = None
            left = SEARCH_FALLBACK_WAIT - (time.time() - t0) - 0.3
            if left >= 1.2:
                raw = _extract_tavily(url, left)
        if not raw.strip():
            return '', {}
        text = _pick_relevant(_fallback_blocks(raw), terms, max_chars)
        if not text:
            return '', {}
        if len(text) < 400 and _BLOCKED_PAGE_RE.search(raw[:3000]):
            return '', {}  # bot dogrulama / JavaScript uyari sayfasi: veri degil
        return text, _meta_dates(meta)
    except Exception as e:
        print(f'[arama] sayfa yedegi atlandi: {type(e).__name__}: {e}')
        return '', {}

def _page_fallback_wave(cands, terms):
    """Ilk okumada yetersiz metin gelen en iyi kaynaklar icin ikinci dalga. Sonuclari yerinde gunceller.
    ASLA exception firlatmaz; yapamazsa hicbir seye dokunmaz."""
    try:
        if not SEARCH_PAGE_FALLBACK or not cands:
            return
        if not (get_serper_key() or get_tavily_key()):
            return
        good = sum(1 for r in cands if len(r.get('page') or '') >= SEARCH_PAGE_MIN_CHARS)
        if good >= SEARCH_FALLBACK_SKIP_IF_GOOD:
            return
        thin = [r for r in cands if len(r.get('page') or '') < SEARCH_PAGE_MIN_CHARS][:SEARCH_FALLBACK_MAX_PAGES]
        if not thin:
            return
        fx = ThreadPoolExecutor(max_workers=len(thin))
        try:
            ff = {fx.submit(_fetch_page_fallback, r['url'], terms): r for r in thin}
            fdone, _fnot = _futures_wait(list(ff), timeout=SEARCH_FALLBACK_WAIT)
            for f in fdone:
                try:
                    txt, dates = f.result()
                except Exception:
                    continue
                item = ff[f]
                if txt and len(txt) > len(item.get('page') or ''):
                    item['page'] = txt
                    item['page_fb'] = True
                    if dates and not (item.get('page_pub') or item.get('page_mod')):
                        _apply_page_dates(item, dates)
        finally:
            try:
                fx.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                fx.shutdown(wait=False)
    except Exception as e:
        print(f'[arama] sayfa yedegi dalgasi atlandi: {type(e).__name__}: {e}')


# ============================================
# SAYFA TARIHI CIKARIMI (article:published_time, <time datetime>, JSON-LD datePublished)
# ============================================
# Amac: arama ozetinde tarih yoksa bile okunan sayfanin kendi kaynak kodundan yayin/guncelleme
# tarihini almak. Tarih hicbir yerden bulunamayan kaynak baglamda "tarihi belirsiz" diye isaretlenir.
# Guvenlik: tum fonksiyonlar ASLA exception firlatmaz; hata/uyumsuzlukta bos sonuc doner ve arama
# eskisi gibi calisir. Modele ham sayfa metni degil, yalnizca BIZIM bicimledigimiz tarih etiketi gider.
_PUB_META_KEYS = (
    'article:published_time', 'og:article:published_time', 'og:published_time', 'article:published',
    'datepublished', 'pubdate', 'publishdate', 'publish_date', 'publication_date', 'published_time',
    'parsely-pub-date', 'sailthru.date', 'dc.date.issued', 'dcterms.created', 'dcterms.issued',
    'dc.date', 'dcterms.date', 'date',
)
_MOD_META_KEYS = (
    'article:modified_time', 'og:article:modified_time', 'article:updated_time', 'og:updated_time',
    'datemodified', 'dcterms.modified', 'dc.date.modified', 'last-modified', 'revised',
)
_ARTICLE_LD_TYPES = {'article', 'newsarticle', 'blogposting', 'reportagenewsarticle', 'techarticle',
                     'scholarlyarticle', 'liveblogposting', 'analysisnewsarticle', 'opinionnewsarticle',
                     'report', 'socialmediaposting'}
_SKIP_LD_TYPES = {'comment', 'review', 'answer', 'question', 'usercomments', 'rating'}
_SKIP_LD_KEYS = {'comment', 'review', 'reviews', 'comments', 'suggestedanswer', 'acceptedanswer'}
_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'>]+))')
_ISO_DATE_RE = re.compile(
    r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})'
    r'(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?)?')
_TR_DMY_RE = re.compile(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?')
_DATE_SRC_WEAK = 'time'

_ASCII_LOWER = {i: i + 32 for i in range(65, 91)}  # sadece A-Z -> a-z (uzunluk korunur, indeksler kayamaz)
_TAG_MAX_LEN = 1500

def _iter_tags(raw, low, name, attempts):
    """<name ...> etiketlerini dogrusal zamanda (find ile) dondurur. Kapanmayan/bozuk etiketler en fazla
    `attempts` denemede elenir, boylece kotu niyetli sayfa CPU'yu bloke edemez."""
    needle = '<' + name
    pos = 0
    n = 0
    L = len(needle)
    while n < attempts:
        i = low.find(needle, pos)
        if i < 0:
            return
        n += 1
        pos = i + L
        nxt = low[i + L:i + L + 1]
        if nxt and (nxt.isalnum() or nxt in '-_:.'):
            continue  # <metadata>, <timeline> gibi baska etiketler
        j = low.find('>', i, i + _TAG_MAX_LEN)
        if j < 0:
            continue
        pos = j + 1
        yield raw[i:j + 1]

def _ld_blocks(raw, low, limit=8):
    """<script type="application/ld+json"> govdelerini dogrusal zamanda dondurur."""
    pos = 0
    out = []
    while len(out) < limit:
        i = low.find('application/ld+json', pos)
        if i < 0:
            break
        s0 = low.rfind('<script', max(0, i - 300), i)
        gt = low.find('>', i, i + 300)
        if s0 < 0 or gt < 0:
            pos = i + 19
            continue
        end = low.find('</script', gt)
        if end < 0:
            break
        out.append(raw[gt + 1:end])
        pos = end + 8
    return out

def _attrs_of(tag):
    out = {}
    for m in _ATTR_RE.finditer(tag[:1500]):
        k = m.group(1).lower()
        if k not in out:
            out[k] = _html_unescape(m.group(2) or m.group(3) or m.group(4) or '').strip()
    return out

def _parse_page_date(s):
    """Tarih metnini (ISO 8601, GG.AA.YYYY, RFC 2822) saat dilimli datetime'a cevirir; gecersiz, cok eski
    ya da gelecek tarihte None doner. Saat dilimi yoksa Turkiye saati varsayilir."""
    try:
        s = (s or '').strip()[:64]
        if not s:
            return None
        dt = None
        m = _ISO_DATE_RE.match(s)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hh, mi, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
            tz = _TR_TZ
            z = m.group(7)
            if z == 'Z':
                tz = timezone.utc
            elif z:
                digits = z[1:].replace(':', '')
                off = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
                tz = timezone(off if z[0] == '+' else -off)
            dt = datetime(y, mo, d, hh, mi, ss, tzinfo=tz)
        else:
            m = _TR_DMY_RE.match(s)
            if m:
                dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                              int(m.group(4) or 0), int(m.group(5) or 0), tzinfo=_TR_TZ)
            elif re.match(r'^[A-Za-z]{3},', s):
                dt = parsedate_to_datetime(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
        if dt is None:
            return None
        now = _tr_now()
        if dt.year < 1995 or dt > now + timedelta(days=1):
            return None  # gelecek tarih yayin tarihi olamaz (etkinlik/zamanlanmis alan olabilir)
        return dt.astimezone(_TR_TZ)
    except Exception:
        return None

def _ld_collect(node, out, depth=0, budget=None, in_article=False):
    """JSON-LD agacini gezip (oncelik, derinlik, sira, 'pub'|'mod', deger) adaylari toplar."""
    if budget is None:
        budget = [1500]
    if budget[0] <= 0 or depth > 8:
        return
    budget[0] -= 1
    if isinstance(node, list):
        for x in node[:60]:
            _ld_collect(x, out, depth, budget, in_article)
        return
    if not isinstance(node, dict):
        return
    t = node.get('@type')
    types = {str(x).lower() for x in (t if isinstance(t, list) else [t]) if x}
    if types & _SKIP_LD_TYPES:
        return
    is_art = bool(types & _ARTICLE_LD_TYPES)
    prio = 0 if is_art else 1
    for key, kind in (('datePublished', 'pub'), ('dateModified', 'mod')):
        v = node.get(key)
        if isinstance(v, str) and v.strip():
            out.append((prio, depth, len(out), kind, v))
    for k, v in node.items():
        if isinstance(v, (dict, list)) and str(k).lower() not in _SKIP_LD_KEYS:
            _ld_collect(v, out, depth + 1, budget, in_article or is_art)

def _extract_page_dates(html_text):
    """Ham HTML'den yayin/guncelleme tarihini cikarir.
    Donus: {'pub': 'YYYY-MM-DD'|'', 'mod': 'YYYY-MM-DD'|'', 'src': 'meta'|'jsonld'|'time'|''}.
    Oncelik: meta etiketleri > JSON-LD > <time datetime>. ASLA exception firlatmaz."""
    res = {'pub': '', 'mod': '', 'src': ''}
    try:
        if not html_text:
            return res
        raw = html_text[:400_000]
        low = raw.translate(_ASCII_LOWER)
        pub = mod = None
        src = ''
        # 1) <meta property|name|itemprop="..." content="...">
        meta_pub, meta_mod = {}, {}
        for tag in _iter_tags(raw, low, 'meta', 200):
            a = _attrs_of(tag)
            val = a.get('content') or ''
            if not val:
                continue
            for k in (a.get('property'), a.get('name'), a.get('itemprop'), a.get('http-equiv')):
                k = (k or '').lower()
                if k in _PUB_META_KEYS and k not in meta_pub:
                    meta_pub[k] = val
                elif k in _MOD_META_KEYS and k not in meta_mod:
                    meta_mod[k] = val
        for k in _PUB_META_KEYS:
            if k in meta_pub:
                pub = _parse_page_date(meta_pub[k])
                if pub:
                    src = 'meta'
                    break
        for k in _MOD_META_KEYS:
            if k in meta_mod:
                mod = _parse_page_date(meta_mod[k])
                if mod:
                    break
        # 2) JSON-LD (datePublished / dateModified)
        if not pub or not mod:
            cands = []
            for body in _ld_blocks(raw, low):
                body = (body or '').strip()
                if not body or len(body) > 300_000:
                    continue
                body = re.sub(r'^\s*(<!--|//<!\[CDATA\[|/\*<!\[CDATA\[\*/)', '', body)
                body = re.sub(r'(-->|//\]\]>|/\*\]\]>\*/)\s*$', '', body).strip()
                try:
                    _ld_collect(json.loads(body), cands)
                except Exception:
                    continue
            cands.sort(key=lambda x: (x[0], x[1], x[2]))
            if not pub:
                for _, _, _, kind, v in cands:
                    if kind == 'pub':
                        pub = _parse_page_date(v)
                        if pub:
                            src = 'jsonld'
                            break
            if not mod:
                for _, _, _, kind, v in cands:
                    if kind == 'mod':
                        mod = _parse_page_date(v)
                        if mod:
                            break
        # 3) <time datetime="..."> (en zayif kanit: ilgili/yan icerik de olabilir)
        if not pub:
            first_plain = None
            hinted = None
            for tag in _iter_tags(raw, low, 'time', 40):
                a = _attrs_of(tag)
                dt = _parse_page_date(a.get('datetime') or '')
                if not dt:
                    continue
                hint = ' '.join([a.get('itemprop', ''), a.get('class', ''), a.get('pubdate', '')]).lower()
                if 'datemodified' in hint or 'updated' in hint or 'modified' in hint:
                    if not mod:
                        mod = dt
                    continue
                if 'datepublished' in hint or 'pubdate' in a or re.search(r'publish|posted|entry-date|article-date|created', hint):
                    hinted = hinted or dt
                elif first_plain is None:
                    first_plain = dt
            pub = hinted or first_plain
            if pub:
                src = 'time_hint' if hinted else 'time'
        if pub and mod and mod < pub:
            mod = None  # tutarsiz: guncelleme yayindan once olamaz
        if pub:
            res['pub'] = pub.strftime('%Y-%m-%d')
        if mod:
            res['mod'] = mod.strftime('%Y-%m-%d')
        res['src'] = src if pub else ''
    except Exception as e:
        print(f'[arama] sayfa tarihi cikarilamadi: {type(e).__name__}: {e}')
        return {'pub': '', 'mod': '', 'src': ''}
    return res

def _age_label(iso):
    """'2026-10-03' -> '3 Ekim 2026 (2 gün önce)'. Gecersizse ''."""
    try:
        d = datetime.strptime(iso, '%Y-%m-%d').date()
        days = (_tr_now().date() - d).days
        if days < 0:
            return ''
        if days == 0:
            age = 'bugün'
        elif days == 1:
            age = 'dün'
        elif days < 14:
            age = f'{days} gün önce'
        elif days < 60:
            age = f'{days // 7} hafta önce'
        elif days < 730:
            age = f'{days // 30} ay önce'
        else:
            age = f'{days // 365} yıl önce'
        return f'{d.day} {_TR_AYLAR[d.month - 1]} {d.year} ({age})'
    except Exception:
        return ''

def _apply_page_dates(item, dates):
    """Sayfadan okunan tarihleri sonuca yazar (arama ozetindeki 'date' alanina DOKUNMAZ)."""
    try:
        if not isinstance(dates, dict):
            return
        item['page_pub'] = dates.get('pub') or ''
        item['page_mod'] = dates.get('mod') or ''
        item['page_date_src'] = dates.get('src') or ''
    except Exception:
        pass

def _date_tag(r):
    """Baglam basligina eklenen tarih etiketi. Hicbir yerde tarih yoksa ' | tarihi belirsiz'."""
    try:
        pub = _age_label(r.get('page_pub') or '')
        mod = _age_label(r.get('page_mod') or '')
        if mod and r.get('page_mod') == r.get('page_pub'):
            mod = ''
        if pub:
            weak = ', time etiketinden, düşük güven' if r.get('page_date_src') == _DATE_SRC_WEAK else ''
            tag = f' | sayfa yayın tarihi: {pub[:-1]}{weak})' if weak else f' | sayfa yayın tarihi: {pub}'
            return tag + (f' | sayfa güncelleme: {mod}' if mod else '')
        if r.get('date'):
            return f' | sayfa güncelleme: {mod}' if mod else ''
        return ' | tarihi belirsiz' + (f' (sayfa güncelleme: {mod})' if mod else '')
    except Exception:
        return ''

# ============================================
# BIRLESTIRME, PUANLAMA, BAGLAM URETIMI
# ============================================
def _merge_results(weighted_lists):
    """[(agirlik, [item,...]), ...] -> URL'ye gore tekillestirilmis, puana gore sirali liste.
    Hem Google hem Tavily'de cikan ve daha yeni tarihli sonuclar one gecer."""
    pool = {}
    for weight, items in weighted_lists:
        for it in items:
            k = _norm_url(it['url'])
            if not k:
                continue
            sc = weight / (1.0 + it['rank'] * 0.35) + 0.3 * (it.get('tscore') or 0.0)
            cur = pool.get(k)
            if cur is None:
                cur = dict(it)
                cur['score'] = sc
                cur['providers'] = {it['provider']}
                pool[k] = cur
            else:
                cur['score'] += sc + 0.25  # birden fazla kaynakta/sorguda cikti
                cur['providers'].add(it['provider'])
                if len(it['snippet']) > len(cur['snippet']):
                    cur['snippet'] = it['snippet']
                if len(it['content']) > len(cur['content']):
                    cur['content'] = it['content']
                if not cur['date'] and it['date']:
                    cur['date'] = it['date']
    for cur in pool.values():
        cur['score'] += _recency_bonus(cur['date'])
    return sorted(pool.values(), key=lambda x: -x['score'])

# ============================================
# ALAKA SIRALAMASI + ELEME
# ============================================
# Ekstra model cagrisi YOK (hiz kaybi sifir, sonuc her seferinde ayni). Her sonucun baslik/ozet/icerigi
# sorgu terimleriyle eslestirilir (Turkce ek toleransli). Guvenlik kurallari:
#  - Google'in ilk 3 sonucu, birden cok saglayici/sorguda cikanlar ve Tavily'nin yuksek puanlilari ELENMEZ
#    (anlamsal olarak ilgili ama kelime paylasmayan sonuclari kaybetmemek icin).
#  - Eleme sonrasi en az SEARCH_MIN_KEEP sonuc kalir; liste ASLA bosalmaz.
#  - Bir hata olursa girdi listesi aynen doner (arama asla bozulmaz).
SEARCH_RELEVANCE_WEIGHT = 1.2    # alaka puaninin toplam puana katkisi
SEARCH_MIN_KEEP = 4              # eleme sonrasi korunacak en az sonuc sayisi
SEARCH_MAX_PER_DOMAIN = 2        # ayni siteden one alinacak en fazla sonuc (fazlasi listenin sonuna gider)
SEARCH_IRRELEVANT_BELOW = 0.12   # bu alakanin altindaki (ve korumasiz) sonuclar elenir

# --- Birincil kaynak onceligi: finans/hukuk sorularinda resmi kurum sitelerine puan bonusu ---
# Sadece SIRALAMAYA etki eder: hicbir sonuc bu yuzden eklenmez/elenmez. Bonus yalnizca konuyla
# yeterince alakali (rel >= SEARCH_IRRELEVANT_BELOW) resmi sayfalara verilir; alakasiz bir resmi sayfa one gecmez.
SEARCH_OFFICIAL_BONUS = 0.6
_OFFICIAL_DOMAINS = (
    # Finans / ekonomi
    'tcmb.gov.tr', 'tuik.gov.tr', 'kap.org.tr', 'spk.gov.tr', 'bddk.org.tr', 'hmb.gov.tr',
    'gib.gov.tr', 'sgk.gov.tr', 'borsaistanbul.com', 'mkk.com.tr', 'takasbank.com.tr',
    # Hukuk / mevzuat
    'resmigazete.gov.tr', 'mevzuat.gov.tr', 'mevzuat.adalet.gov.tr', 'adalet.gov.tr', 'yargitay.gov.tr',
    'danistay.gov.tr', 'anayasa.gov.tr', 'kararlarbilgibankasi.anayasa.gov.tr', 'tbmm.gov.tr',
    'kvkk.gov.tr', 'rekabet.gov.tr',
)
def _fold_tr(s):
    """Kucuk harf + Turkce karakterleri ASCII'ye indir (cakisma/yazim farklarina dayanikli eslestirme)."""
    t = _tr_lower(s)
    for a, b in (('ç', 'c'), ('ğ', 'g'), ('ı', 'i'), ('ö', 'o'), ('ş', 's'), ('ü', 'u'), ('â', 'a'), ('î', 'i'), ('û', 'u')):
        t = t.replace(a, b)
    return t

_FINANS_HUKUK_RE = re.compile(
    r'\b(?:'
    # finans / ekonomi
    r'dolar\w*|sterlin\w*|doviz\w*|faiz\w*|enflasyon\w*|borsa\w*|bist\w*|hisse\w*|temettu\w*|bilanco\w*|'
    r'tahvil\w*|mevduat\w*|kredi\w*|vergi\w*|tufe|ufe|gsyh|cari acik\w*|merkez bankas\w*|tcmb|kap|spk|bddk|'
    r'asgari ucret\w*|maas\w*|emekli\w*|yatirim\w*|swap|repo|'
    r'euro(?:nun|yu|su|lar|da|dan)?|altin(?:in|i|a|da|dan|lar|lari)?|kur(?:u|lari|lar|un)?|fon(?:u|lar|lari|un)?|'
    r'inflation|interest rate|exchange rate|central bank|stock market|gdp|'
    # hukuk / mevzuat
    r'kanun\w*|yasa(?:si|sinin|nin|ya|da|lar|lari|yi)?|hukuk\w*|mevzuat\w*|yonetmelik\w*|teblig\w*|resmi gazete\w*|'
    r'cumhurbaskanligi karar\w*|kararname\w*|yargitay\w*|danistay\w*|anayasa\w*|mahkeme\w*|dava\w*|ceza\w*|'
    r'tazminat\w*|icra\w*|haciz\w*|miras\w*|bosanma\w*|kira artis\w*|kiraci\w*|ihtarname\w*|noter\w*|avukat\w*|'
    r'sozlesme\w*|kidem\w*|ihbar tazminat\w*|yasal\w*|kvkk|tck|tmk|hmk|cmk|'
    r'law|legal|legislation|regulation|lawsuit'
    r')\b'
)

def _is_finance_legal_topic(queries):
    """Arama sorgulari finans/hukuk konusunda mi? Hata olursa False (bonus verilmez)."""
    try:
        return bool(_FINANS_HUKUK_RE.search(_fold_tr(' | '.join(queries or []))))
    except Exception:
        return False

def _is_official_source(url_or_host):
    try:
        h = (url_or_host or '').strip().lower()
        if '://' in h:
            h = urlparse(h).hostname or ''
        h = re.sub(r'^www\.', '', h.split(':')[0])
        return any(h == d or h.endswith('.' + d) for d in _OFFICIAL_DOMAINS)
    except Exception:
        return False

def _query_term_sets(queries):
    """Her sorgu icin ayri anahtar kelime listesi (Turkce ve Ingilizce sorgular birbirini ezmesin)."""
    sets = []
    for q in queries:
        ts = []
        for w in re.findall(r'\w{3,}', _tr_lower(q)):
            if w not in _STOP_KELIMELER and w not in ts:
                ts.append(w)
        if ts:
            sets.append(ts[:12])
    return sets

def _term_hits(term, words):
    """Terim, kelime kumesinde var mi? 5+ harfli terimlerde Turkce ekler tolere edilir (dolar -> dolari)."""
    if term in words:
        return True
    if len(term) <= 4:
        return False
    stem = term[:-1] if len(term) <= 6 else term[:-2]
    lim = len(term) + 4
    for w in words:
        if len(w) <= lim and w.startswith(stem):
            return True
    return False

def _relevance(item, term_sets):
    """0..1 arasi alaka puani: terimlerin ne kadari baslikta/ozette/icerikte geciyor (baslik daha agirlikli)."""
    if not term_sets:
        return 0.5
    title = item.get('title') or ''
    body = ' '.join([title, item.get('snippet') or '', (item.get('content') or '')[:1500]])
    title_words = set(re.findall(r'\w{3,}', _tr_lower(title)))
    all_words = set(re.findall(r'\w{3,}', _tr_lower(body)))
    best = 0.0
    for ts in term_sets:
        n = len(ts)
        in_all = sum(1 for t in ts if _term_hits(t, all_words))
        in_title = sum(1 for t in ts if _term_hits(t, title_words))
        best = max(best, 0.65 * in_all / n + 0.35 * in_title / n)
    return best

def _rerank_results(results, queries, fresh=False, guncel=False):
    """Sonuclara alaka puani ekler, yeniden siralar, belirgin alakasizlari eler, ayni siteyi sinirlar.
    ASLA exception firlatmaz; hata olursa girdi listesini aynen dondurur."""
    try:
        term_sets = _query_term_sets(queries)
        cur_year = _tr_now().year
        resmi_konu = _is_finance_legal_topic(queries)
        resmi_sayisi = 0
        scored = []
        for r in results:
            rel = _relevance(r, term_sets)
            try:
                tsc = max(0.0, min(1.0, float(r.get('tscore') or 0.0)))
            except Exception:
                tsc = 0.0
            rel_eff = max(rel, tsc)
            sc = float(r.get('score') or 0.0) + SEARCH_RELEVANCE_WEIGHT * rel_eff
            if resmi_konu and rel_eff >= SEARCH_IRRELEVANT_BELOW and _is_official_source(r.get('url') or r.get('source')):
                sc += SEARCH_OFFICIAL_BONUS  # finans/hukuk: resmi birincil kaynak one cikar
                resmi_sayisi += 1
            if fresh:
                sc += _recency_bonus(r.get('date'))  # guncellik onemli aramada taze tarih ayrica one cikar
            nr = dict(r)
            if guncel:
                adj, stale = _year_adjust(r, cur_year)
                sc += adj
                nr['stale'] = stale
            nr['rel'] = round(rel_eff, 3)
            nr['score'] = sc
            scored.append(nr)
        scored.sort(key=lambda x: -x['score'])

        keep, dropped = [], []
        for r in scored:
            protected = (r.get('rank', 99) < 3
                         or len(r.get('providers') or ()) > 1
                         or (r.get('tscore') or 0) >= 0.4)
            if r['rel'] < SEARCH_IRRELEVANT_BELOW and not protected:
                dropped.append(r)
            else:
                keep.append(r)
        if len(keep) < SEARCH_MIN_KEEP:
            need = SEARCH_MIN_KEEP - len(keep)
            keep += dropped[:need]
            dropped = dropped[need:]
            keep.sort(key=lambda x: -x['score'])

        out, overflow, per = [], [], {}
        for r in keep:
            d = r.get('source') or ''
            per[d] = per.get(d, 0) + 1
            (out if per[d] <= SEARCH_MAX_PER_DOMAIN else overflow).append(r)
        out += overflow
        if resmi_sayisi:
            print(f'[arama] resmi kaynak bonusu: {resmi_sayisi} sonuca +{SEARCH_OFFICIAL_BONUS}')
        if dropped:
            print(f'[arama] alaka elemesi: {len(dropped)} alakasiz sonuc cikarildi, {len(out)} sonuc kaldi')
        return out or results
    except Exception as e:
        print(f'[arama] siralama hatasi (siralama atlandi): {type(e).__name__}: {e}')
        return results

def _build_context(queries, extras, results, guncel=False, live=False):
    lines = [f"Arama sorguları: {' | '.join(queries)} (arama tarihi: {_tr_now_str()})"]
    if live:
        lines.append(
            f'Not: ANLIK VERİ sorusu, şu an {_tr_now_str()}. Yalnızca başlıkta "veri saati" en yeni olan kaynağın '
            f'rakamını ver ve saatini yaz. {LIVE_MAX_AGE_MIN} dakikadan eski veri saatli kaynağı güncel gibi sunma.')
    if guncel:
        lines.append(
            f'Not: Soru güncel veri istiyor, bugün {_tr_now_str()}. Sonuçlar farklı yıllara ait olabilir; '
            'en yeni dönemi esas al. Başlığında ESKİ DÖNEM yazan sonuçları yalnızca güncel karşılığı yoksa kullan ve yılını belirt.')
    for e in extras[:3]:
        lines.append(e)
    used = []
    total = sum(len(l) for l in lines)
    for r in results[:SEARCH_RESULTS_IN_CONTEXT]:
        head = f"[{len(used) + 1}] {r['title']} — {r['source']}" + (f" — {r['date']}" if r['date'] else '')
        if guncel:
            _ys = _item_years(r)
            if _ys:
                head += f' | tespit edilen yıl: {max(_ys)}' + (' | ESKİ DÖNEM' if r.get('stale') else '')
        head += _date_tag(r)
        if live and r.get('live_clock'):
            head += f" | veri saati: {r['live_clock']} ({_fmt_age(r.get('live_age'))})" + (' | VERİ SAATİ ESKİ' if r.get('live_stale') else '')
        body = []
        base = r['content'] or r['snippet']
        if r.get('page'):
            _lt = _tr_now()
            if r.get('live_stale'):
                body.append('SAYFA METNİ (VERİ SAATİ ESKİ, güncel sayılmaz): ' + r['page'])
            elif r.get('stale'):
                body.append('SAYFA METNİ (eski dönem içeriği olabilir): ' + r['page'])
            else:
                body.append(f'CANLI SAYFA (şu anda siteden okundu, saat {_lt.hour:02d}:{_lt.minute:02d}): ' + r['page'])
        if base:
            body.append('Arama özeti (eski olabilir): ' + base[:SEARCH_RESULT_CHARS])
        block = head + ('\n   ' + '\n   '.join(body) if body else '')
        if total + len(block) > SEARCH_CONTEXT_MAX_CHARS:
            room = SEARCH_CONTEXT_MAX_CHARS - total
            if room < 300:
                break
            block = block[:room]
        lines.append(block)
        used.append(r)
        total += len(block)
    sources = [{'title': r['title'], 'url': r['url']} for r in used[:6]]
    return '\n'.join(lines), sources

# ============================================
# KISA SURELI ONBELLEK
# ============================================
_SEARCH_CACHE = {}
_SEARCH_CACHE_LOCK = threading.Lock()

def _cache_key(plan):
    return json.dumps([plan.get('queries'), plan.get('zaman'), bool(plan.get('haber')), bool(plan.get('detay')), bool(plan.get('guncel'))],
                      ensure_ascii=False)

def _cache_get(key):
    with _SEARCH_CACHE_LOCK:
        hit = _SEARCH_CACHE.get(key)
        if hit and time.time() - hit[0] < SEARCH_CACHE_TTL:
            return hit[1]
        _SEARCH_CACHE.pop(key, None)
    return None

def _cache_put(key, value):
    with _SEARCH_CACHE_LOCK:
        if len(_SEARCH_CACHE) >= SEARCH_CACHE_MAX:
            oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
            _SEARCH_CACHE.pop(oldest, None)
        _SEARCH_CACHE[key] = (time.time(), value)

# ============================================
# ANA ARAMA FONKSIYONU
# ============================================
SEARCH_CONTEXT_MAX_CHARS = 7500     # LLM'e giden toplam arama baglami (token limiti icin ayarlanabilir)
SEARCH_RESULTS_IN_CONTEXT = 7
SEARCH_RESULT_CHARS = 900           # kaynak basina ozet/parca siniri
SEARCH_PAGE_CHARS = 1600            # kaynak sayfasindan okunan metin siniri
SEARCH_PAGE_FETCH_COUNT = 3         # kac sayfa acilip okunacak
SEARCH_PAGE_TIMEOUT = 3.0           # saniye: tek sayfa icin
SEARCH_PAGE_WAIT = 3.5              # saniye: sayfa okumalarinin toplami (yetismeyen sayfa atlanir)
SEARCH_PAGE_FALLBACK = True         # JavaScript ile yuklenen/bos gelen sayfalar icin yedek okuma (False: tamamen kapali)
SEARCH_PAGE_MIN_CHARS = 200         # ilk okumada bundan az metin gelen sayfa yetersiz sayilir
SEARCH_FALLBACK_MAX_PAGES = 2       # bir aramada en fazla kac sayfa icin yedek calissin (kredi ve sure korumasi)
SEARCH_FALLBACK_SKIP_IF_GOOD = 2    # ilk okumada bu kadar sayfa yeterliyse yedek hic calismaz
SEARCH_FALLBACK_WAIT = 4.0          # saniye: yedek okumalarin toplam bekleme siniri
SEARCH_MAX_QUERIES = 3
SEARCH_CACHE_TTL = 30               # saniye
SEARCH_CACHE_MAX = 64

def _empty_search(attempted=False, errors=None):
    return {'context': '', 'sources': [], 'attempted': attempted, 'errors': errors or []}

def _default_plan(text):
    t = _tr_lower((text or '').strip())
    return {
        'queries': [(text or '').strip()[:300]],
        'zaman': None,
        'haber': any(k in t for k in ('haber', 'son dakika', 'skor', 'maç', 'mac')),
        'detay': len(t.split()) >= 6,
    }

# ============================================
# ILK MESAJDA PARALEL PLANLAMA (cok parcali sorularda kapsama)
# ============================================
# Sohbetin ilk mesajinda planlayici modele gidilmez, soru tek sorgu olarak aranir (hizli). Cok parcali
# gorunen sorularda bu zayif kalir. Bu durumda ham soru HEMEN aranmaya baslar, planlayici model ayni
# anda calisir; SEARCH_PLAN_PARALLEL_WAIT saniye icinde sonuc verirse planin ek sorgulari ayni aramaya
# eklenir (en fazla SEARCH_LATE_MAX_EXTRA tane). Ham arama beklemeden baslar; planlayici yavas kalir,
# hata verir ya da bos donerse arama tamamen eski sorgularla devam eder. Gecmisi olan sohbetlerde eski akis aynen
# korunur (planlayici zaten once calisir). SEARCH_PARALLEL_PLAN = False ile tamamen kapanir.
SEARCH_PARALLEL_PLAN = True
SEARCH_PLAN_PARALLEL_WAIT = 1.5   # saniye: planlayicinin ham aramayla paralel bekleme siniri
SEARCH_LATE_MAX_EXTRA = 2         # planin ham aramaya ekleyebilecegi en fazla sorgu sayisi
_PLAN_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='plan')
_MULTI_MARK_RE = re.compile(
    r'(?<![a-zçğıöşü])(ve|ile|veya|ayrıca|hem|bir de|karşılaştır\w*|arasında|arasındaki|fark\w*|hangisi|versus|vs)(?![a-zçğıöşü])')
_Q_WORD_RE = re.compile(
    r'(?<![a-zçğıöşü])(kaç|ne kadar|nedir|kimdir|kim|nerede|ne zaman|nasıl|hangi\w*|neden|niçin)(?![a-zçğıöşü])')

def _first_msg_needs_plan(text):
    """Soru cok parcali/karsilastirmali gorunuyor mu (kural tabanli, model cagrisi yok)."""
    try:
        t = _tr_lower(text or '')
        words = len(t.split())
        if t.count('?') >= 2 or words >= 12:
            return True
        if len(_Q_WORD_RE.findall(t)) >= 2:
            return True
        return words >= 5 and bool(_MULTI_MARK_RE.search(t) or ',' in t)
    except Exception:
        return False

def _late_plan_job(question):
    """Arka planda planlayiciyi calistirir; yil/ay kurali uygulanmis plan ya da None doner. ASLA exception firlatmaz."""
    try:
        plan = llm_plan_search(question, None)
        if not isinstance(plan, dict) or not plan.get('queries'):
            return None
        return _apply_fresh_rules(plan, (question or '').strip())
    except Exception as e:
        print(f'[arama] paralel plan hatasi: {type(e).__name__}: {e}')
        return None

def _q_norm(s):
    try:
        return re.sub(r'\s+', ' ', _YEAR_RE.sub(' ', _tr_lower(s or ''))).strip()
    except Exception:
        return (s or '').strip().lower()

def _late_plan_queries(late):
    """late = (future, baslangic_zamani, soru). Kalan sure kadar bekler. ([sorgular], zaman, haber) doner;
    sure dolarsa ya da hata olursa ([], None, False). ASLA exception firlatmaz."""
    try:
        fut, t0, _q = late
        wait = SEARCH_PLAN_PARALLEL_WAIT - (time.time() - t0)
        plan = fut.result(timeout=max(0.0, wait))
        if not isinstance(plan, dict):
            return [], None, False
        qs = [q.strip() for q in (plan.get('queries') or []) if isinstance(q, str) and q.strip()]
        return qs, plan.get('zaman'), bool(plan.get('haber'))
    except Exception:
        return [], None, False

def _late_extend(late, queries, zaman, haber):
    """Planlayicinin, ham aramada zaten bulunmayan ek sorgulari: [(sorgu, zaman, haber)]. Yil eki farki
    sayilmaz (ayni sorgu iki kez aranmaz). En fazla SEARCH_LATE_MAX_EXTRA sorgu. ASLA exception firlatmaz."""
    try:
        out = []
        seen = {_q_norm(x) for x in queries}
        pq, pz, ph = _late_plan_queries(late)
        for q in pq:
            n = _q_norm(q)
            if n and n not in seen and len(out) < SEARCH_LATE_MAX_EXTRA:
                seen.add(n)
                out.append((q, pz or zaman, ph))
        return out
    except Exception as e:
        print(f'[arama] paralel plan birlestirme hatasi: {type(e).__name__}: {e}')
        return []

def search_internet_ex(plan, late=None):
    """Cok sorgulu, cok kaynakli (Google/Serper + Tavily) arama yapar, sonuclari birlestirip
    puanlar ve en iyi kaynaklarin sayfa iceriklerini okur.
    Donus: {'context': str, 'sources': [{'title','url'}], 'attempted': bool, 'errors': [str]}.
    ASLA exception firlatmaz."""
    try:
        return _search_internet_ex_impl(plan, late)
    except Exception as e:
        print(f'[arama] beklenmeyen hata: {type(e).__name__}: {e}')
        return _empty_search(True, [f'beklenmeyen hata: {type(e).__name__}'])

SEARCH_PROVIDER_TIMEOUT = 8.0   # saniye: saglayicilari beklemek icin kesin ust sinir
SEARCH_PROVIDER_GRACE = 2.0     # saniye: ilk saglayici sonuc getirdikten sonra digerlerine taninan ek sure

def _wait_providers(futs, hard_timeout=None, grace=None):
    """Arama saglayicilarini bekler ama YAVAS olani beklemez: ilk saglayici sonuc getirdiyse
    digerlerine yalnizca kisa bir ek sure (grace) verir. Hata veren/bos donen saglayici bu
    sureyi baslatmaz, yani tek saglayici calisiyorsa hard_timeout'a kadar beklenir.
    Donus: (biten_future'lar, bitmeyen_future'lar)."""
    hard_timeout = SEARCH_PROVIDER_TIMEOUT if hard_timeout is None else hard_timeout
    grace = SEARCH_PROVIDER_GRACE if grace is None else grace
    pending = set(futs)
    done = set()
    t0 = time.time()
    good_at = None
    while pending:
        now = time.time()
        limit = hard_timeout - (now - t0)
        if good_at is not None:
            limit = min(limit, grace - (now - good_at))
        if limit <= 0:
            break
        d, pending = _futures_wait(pending, timeout=limit, return_when=FIRST_COMPLETED)
        for f in d:
            done.add(f)
            if good_at is None:
                try:
                    res = f.result()
                    if res and res[0]:
                        good_at = time.time()
                except Exception:
                    pass
    return done, pending

def _search_internet_ex_impl(plan, late=None):
    if isinstance(plan, str):
        plan = _default_plan(plan)
    if not isinstance(plan, dict):
        return _empty_search()
    queries = [q.strip() for q in (plan.get('queries') or []) if isinstance(q, str) and q.strip()]
    if not queries:
        return _empty_search()
    has_serper = bool(get_serper_key())
    has_tavily = bool(get_tavily_key())
    if not (has_serper or has_tavily):
        return _empty_search(False, ['arama anahtari tanimli degil'])
    queries = queries[:SEARCH_MAX_QUERIES if has_serper else 2]
    plan = dict(plan, queries=queries)
    zaman = plan.get('zaman')
    live = _is_live_query(queries)
    if live:
        zaman = 'gun'  # anlik veri: arama filtresi her zaman son 24 saat
    haber = bool(plan.get('haber'))
    derin = plan.get('detay', True) is not False
    guncel = bool(plan.get('guncel'))

    ck = _cache_key(plan)
    cached = _cache_get(ck)
    if cached:
        return cached

    # Sigorta: ust uste gecici hata veren saglayiciyi (digeri saglamsa) bu aramada atla.
    use_serper = has_serper and not _breaker_skip('serper', 'tavily', has_tavily)
    use_tavily = has_tavily and not _breaker_skip('tavily', 'serper', has_serper)
    degraded = (use_serper != has_serper) or (use_tavily != has_tavily)
    if not use_serper:
        queries = queries[:2]  # Serper yokken Tavily kredisini korumak icin en fazla 2 sorgu
    _ok_kinds = set()
    _bad_kinds = set()

    t_start = time.time()
    errors = []
    weighted = []
    extras = []
    ex = ThreadPoolExecutor(max_workers=6)
    futs = {}
    _late_n = 0
    try:
        for qi, q in enumerate(queries):
            if use_serper:
                futs[ex.submit(_serper_fetch, q, zaman, haber)] = ('serper', qi)
            # Tavily: plan 'detay' ise 'advanced', degilse 'basic' mod. Serper varsa sadece ana sorguda kullan.
            if use_tavily and (qi == 0 or not use_serper):
                futs[ex.submit(_tavily_fetch, q, zaman, haber, derin)] = ('tavily', qi)
        if late is not None and use_serper:
            # Ham sorgular yukarida zaten aranmaya basladi; planlayici sonucu hazirsa ek sorgular eklenir.
            for _q, _z, _h in _late_extend(late, queries, zaman, haber):
                qi = len(queries)
                queries.append(_q)
                _late_n += 1
                futs[ex.submit(_serper_fetch, _q, _z, _h)] = ('serper', qi)
        done, not_done = _wait_providers(futs)
        for f in done:
            kind, qi = futs[f]
            qweight = max(0.4, 1.0 - 0.2 * qi)
            try:
                res = f.result()
            except Exception as e:
                _note_provider(kind, f'{type(e).__name__}')
                errors.append(f'{kind}: {type(e).__name__}')
                _bad_kinds.add(kind)
                continue
            if kind == 'serper':
                items, ex_txt, err = res
                _note_provider('serper', err)
                if err:
                    errors.append(f'serper: {err}')
                    if _is_transient_err(err):
                        _bad_kinds.add('serper')
                else:
                    _ok_kinds.add('serper')
                weighted.append((1.0 * qweight, items))
                for e_txt in ex_txt:
                    if e_txt not in extras:
                        extras.append(e_txt)
            else:
                items, err = res
                _note_provider('tavily', err)
                if err:
                    errors.append(f'tavily: {err}')
                    if _is_transient_err(err):
                        _bad_kinds.add('tavily')
                else:
                    _ok_kinds.add('tavily')
                weighted.append((0.85 * qweight, items))
        for f in not_done:
            kind, _ = futs[f]
            _note_provider(kind, 'zaman asimi')
            errors.append(f'{kind}: zaman asimi')
            _bad_kinds.add(kind)
        # Sigorta: arama basina TEK sonuc (3 Serper sorgusu ayni anda 3 hata sayilmasin)
        for _k in ('serper', 'tavily'):
            if _k in _ok_kinds:
                _breaker_report(_k, True)
            elif _k in _bad_kinds:
                _breaker_report(_k, False)
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)

    results = _merge_results(weighted)
    if not results:
        return _empty_search(True, errors or ['sonuc bulunamadi'])
    results = _rerank_results(results, queries, fresh=bool(zaman or haber), guncel=guncel)

    # --- Sayfa okuma: detay isteniyorsa (ya da Google'in hazir cevabi yoksa) en iyi kaynaklari ac ---
    # Arama motorlarinin ozetleri saatler/gunler eski olabilir. Bu yuzden konu ne olursa olsun
    # en iyi kaynaklarin sayfalari ASIL SITEDEN o anda okunur (canli veri).
    if True:
        terms = _query_terms(queries)
        _fetchable = [r for r in results if _is_fetchable_quick(r['url'])]
        cands = ([r for r in _fetchable if not r.get('stale')] or _fetchable)[:SEARCH_PAGE_FETCH_COUNT]
        if cands:
            pex = ThreadPoolExecutor(max_workers=SEARCH_PAGE_FETCH_COUNT)
            try:
                pf = {pex.submit(_fetch_page_ex, r['url'], terms, None, SEARCH_PAGE_TIMEOUT): r for r in cands}
                pdone, _pnot = _futures_wait(list(pf), timeout=SEARCH_PAGE_WAIT)
                for f in pdone:
                    try:
                        _ptxt, _pdates = f.result()
                        pf[f]['page'] = _ptxt or ''
                        _apply_page_dates(pf[f], _pdates)
                    except Exception:
                        pass
            finally:
                try:
                    pex.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pex.shutdown(wait=False)
            _page_fallback_wave(cands, terms)

    live_stale = False
    if live:
        results, live_stale = _live_filter(results)
    context, sources = _build_context(queries, extras, results, guncel=guncel, live=live)
    out = {'context': context, 'sources': sources, 'attempted': True, 'errors': errors,
           'stale_top': _stale_top(results, guncel), 'live_stale': live_stale}
    if not degraded:
        _cache_put(ck, out)  # sigorta devredeyken (eksik saglayici) sonuc onbellege alinmaz
    print(f"[arama] sorgular={queries} zaman={zaman} haber={haber} sonuc={len(results)} "
          f"ek_sorgu={_late_n} sayfa_okunan={sum(1 for r in results if r.get('page'))} yedek_okunan={sum(1 for r in results if r.get('page_fb'))} sure={time.time() - t_start:.1f}s "
          f"hatalar={errors or '-'}")
    return out

def _is_fetchable_quick(url):
    """DNS cozumlemesi yapmadan hizli on eleme (asil kontrol _fetch_page_text icinde)."""
    try:
        p = urlparse(url)
        host = (p.hostname or '').lower()
        if p.scheme not in ('http', 'https') or not host:
            return False
        if any(host == d or host.endswith('.' + d) for d in _NO_FETCH_DOMAINS):
            return False
        return not p.path.lower().endswith(_NO_FETCH_EXT)
    except Exception:
        return False

def search_internet(query):
    """Eski imza (geriye uyumluluk): (llm_icin_baglam_metni, kaynak_listesi) doner."""
    r = search_internet_ex(query)
    return r['context'], r['sources']

def build_kaynak_markdown(sources):
    """Kaynaklar artik cevap METNINE eklenmiyor. Arayuz, 'model' olayiyla gelen kaynak
    listesinden her site icin tiklanabilir site ikonu (favicon) uretir. Bu fonksiyon
    geriye uyumluluk icin korunur ve her zaman bos metin doner."""
    return ''

# ============================================
# SORGU PLANI (LLM tabanli, tool-calling) - "?" komutundan SONRA calisir
# ============================================
# "Aranmali mi?" karari YOK: komut verildiyse arama kesin yapilir. Kucuk/hizli model
# yalnizca NE aranacagini planlar (1-3 sorgu, zaman filtresi, haber/detay bayraklari).
# Model basarisiz olursa kullanicinin sorusu dogrudan sorgu olarak kullanilir (_default_plan).
SEARCH_DECISION_MODEL = 'openai/gpt-oss-20b'  # en hizli/ucuz model - sadece sorgu planlama icin

SEARCH_TOOL_SCHEMA = {
    'type': 'function',
    'function': {
        'name': 'internet_arama',
        'description': 'Kullanicinin sorusunu dogru ve GUNCEL sekilde cevaplamak icin internette (Google) arastirma yapar.',
        'parameters': {
            'type': 'object',
            'properties': {
                'sorgular': {
                    'type': 'array',
                    'minItems': 1,
                    'maxItems': 3,
                    'items': {'type': 'string'},
                    'description': (
                        '1-3 arama sorgusu. Her biri tek basina anlasilir olmali (onceki konusmadaki '
                        'ozne/konu sorguya tasinmali). Karsilastirma veya cok parcali sorularda her '
                        'parca icin ayri sorgu; gerekirse biri Ingilizce. Yil/surum yazarken BUGUNUN '
                        'tarihine gore yaz, kendi eski bilginden yil veya surum uydurma.'
                    ),
                },
                'zaman': {
                    'type': 'string',
                    'enum': ['gun', 'hafta', 'ay', 'yil', 'yok'],
                    'description': "Sonuclarin ne kadar taze olmasi gerektigi: son 24 saat='gun', 'hafta', 'ay', 'yil' veya sinir yoksa 'yok'.",
                },
                'haber': {'type': 'boolean', 'description': 'Soru guncel haber/olay ise true.'},
                'detay': {
                    'type': 'boolean',
                    'description': 'Ayrintili/derin kaynak okumasi gerekiyorsa true; tek bir veri (hava, kur, skor) ise false.',
                },
            },
            'required': ['sorgular'],
        },
    },
}

def _build_plan_system_msg():
    return {
        'role': 'system',
        'content': (
            f'Sen bir arama sorgusu planlama katmanisin. BUGUNUN TARIHI: {_tr_now_str()}.\n'
            'Kullanici mesajini soru isareti (?) ile bitirerek internet aramasi ISTEDI; arama KESINLIKLE yapilacak, '
            '"gerekli mi" diye DUSUNME. Gorevin: sorusunu en iyi cevaplayacak 1-3 arama sorgusunu '
            'internet_arama fonksiyonuyla uretmek.\n'
            '- Sorgular tek basina anlasilir olsun (onceki konusmadaki ozneyi sorguya tasi). '
            'Cok parcali veya karsilastirmali sorularda parca basina ayri sorgu yaz; gerekirse biri Ingilizce olsun.\n'
            '- "en son", "guncel", "yeni" gibi ifadelerde yil olarak bugunun yilini kullan; kendi eski bilginden '
            'yil, model adi veya surum numarasi uydurma.\n'
            '- Taze bilgi onemliyse zaman filtresini (gun/hafta/ay/yil) ayarla; haber niteligindeyse haber=true.\n'
            'Fonksiyonu MUTLAKA cagir, baska hicbir metin yazma.'
        ),
    }

def _history_tail_text(history_tail):
    lines = []
    for m in (history_tail or [])[-4:]:
        if not isinstance(m, dict) or not isinstance(m.get('content'), str):
            continue
        who = 'Kullanici' if m.get('role') == 'user' else 'Asistan'
        c = re.sub(r'\s+', ' ', m['content']).strip()
        if c:
            lines.append(f'{who}: {c[:300]}')
    return '\n'.join(lines)

_ZAMAN_MAP = {'gun': 'gun', 'gün': 'gun', 'hafta': 'hafta', 'ay': 'ay', 'yil': 'yil', 'yıl': 'yil'}

def _sanitize_plan(args, text):
    qs = args.get('sorgular') if isinstance(args, dict) else None
    if qs is None and isinstance(args, dict):
        qs = args.get('sorgu')  # eski sema ile uyumluluk
    if isinstance(qs, str):
        qs = [qs]
    if not isinstance(qs, list):
        qs = []
    clean, seen = [], set()
    for q in qs:
        if not isinstance(q, str):
            continue
        q = re.sub(r'\s+', ' ', q).strip()[:300]
        if q and q.lower() not in seen:
            seen.add(q.lower())
            clean.append(q)
    if not clean:
        clean = [text[:300]]
    z = args.get('zaman') if isinstance(args, dict) else None
    z = _ZAMAN_MAP.get(z.strip().lower()) if isinstance(z, str) else None
    haber = isinstance(args, dict) and args.get('haber') is True
    detay = args.get('detay') if isinstance(args, dict) else None
    if not isinstance(detay, bool):
        detay = len(text.split()) >= 5
    return {'queries': clean[:SEARCH_MAX_QUERIES], 'zaman': z, 'haber': haber, 'detay': detay}

def _parse_plan(msg, text):
    """Modelin tool-call'indan arama plani cikarir; yoksa None."""
    for tc in (msg.get('tool_calls') or []):
        fn = tc.get('function') or {}
        if fn.get('name') != 'internet_arama':
            continue
        try:
            args = json.loads(fn.get('arguments') or '{}')
        except Exception:
            args = {}
        return _sanitize_plan(args, text)
    return None

def llm_plan_search(raw_user_text, history_tail=None):
    """Plan dict | None (model cagrisi basarisiz). ASLA exception firlatmaz."""
    text = (raw_user_text or '').strip()
    if not text:
        return None
    keys = get_available_keys(SEARCH_DECISION_MODEL)
    if not keys:
        k = get_active_key()
        keys = [k] if k else []
    if not keys:
        return None
    ctx = _history_tail_text(history_tail)
    user_content = (f'[Onceki konusma]\n{ctx}\n\n' if ctx else '') + f'[Kullanicinin sorusu]\n{text[:1500]}'
    base = {
        'model': SEARCH_DECISION_MODEL,
        'messages': [_build_plan_system_msg(), {'role': 'user', 'content': user_content}],
        'tools': [SEARCH_TOOL_SCHEMA],
        'tool_choice': {'type': 'function', 'function': {'name': 'internet_arama'}},
        'stream': False,
        'temperature': 0,
        'max_completion_tokens': 700,
        'reasoning_effort': 'low',
    }

    def _call(key, pl):
        try:
            with httpx.Client() as c:
                return c.post(URL, headers={'Authorization': f'Bearer {key}'}, json=pl, timeout=5)
        except Exception as e:
            print(f'[arama] plan cagrisi hatasi: {type(e).__name__}')
            return None

    try:
        for key in keys[:2]:
            payload = dict(base)
            r = _call(key, payload)
            # 400: desteklenmeyen parametre -> once reasoning_effort, sonra zorunlu tool_choice kaldirilir
            for drop in ('reasoning_effort', 'tool_choice'):
                if r is not None and r.status_code == 400 and drop in payload:
                    payload = {k2: v2 for k2, v2 in payload.items() if k2 != drop}
                    r = _call(key, payload)
            if r is None:
                continue
            if r.status_code == 429:
                cooldown = 60.0
                try:
                    cooldown = float(r.headers.get('retry-after') or 60)
                except Exception:
                    pass
                mark_key_cooldown(key, SEARCH_DECISION_MODEL, cooldown)
                continue
            if r.status_code != 200:
                print(f'[arama] plan cagrisi HTTP {r.status_code}')
                continue
            try:
                msg = r.json()['choices'][0].get('message') or {}
            except Exception:
                continue
            plan = _parse_plan(msg, text)
            if plan:
                return plan
    except Exception as e:
        print(f'[arama] plan cozumleme hatasi: {type(e).__name__}')
    return None

def _plan_ara_komutu_core(question, history_tail=None):
    """'?' komutu icin arama plani. HER ZAMAN plan doner (model basarisiz olsa bile)."""
    q = (question or '').strip()
    plan = None
    if not history_tail:
        # Sohbetin ilk mesaji: onceki baglam yok, soru zaten kendi basina anlasilir -> planlama
        # modeline gitmeden dogrudan aranir (bir model cagrisi kadar hiz kazandirir).
        return _default_plan(q)
    try:
        plan = llm_plan_search(q, history_tail)
    except Exception as e:
        print(f'[arama] plan hatasi: {type(e).__name__}: {e}')
    if not isinstance(plan, dict) or not plan.get('queries'):
        return _default_plan(q)
    qs = list(plan['queries'])
    # Kullanicinin kendi cumlesi de (yer varsa) sorgulara eklenir; model yanlis yeniden yazsa bile korunur.
    if len(qs) < SEARCH_MAX_QUERIES and q[:300].lower() not in [x.lower() for x in qs]:
        qs.append(q[:300])
    plan['queries'] = qs
    return plan

def plan_ara_komutu(question, history_tail=None):
    """Plan (ilk mesaj dahil) + kural tabanli guncellik adimi: yil/ay eki ve zaman filtresi. Ek model cagrisi yok."""
    plan = _plan_ara_komutu_core(question, history_tail)
    return _apply_fresh_rules(plan, (question or '').strip())

def plan_ara_komutu_late(question, history_tail=None):
    """(plan, late) doner. plan her zaman plan_ara_komutu ile aynidir. late yalnizca sohbetin ilk mesajinda ve
    soru cok parcali gorunuyorsa doludur: (future, baslangic_zamani, soru); aksi halde None (eski akis aynen)."""
    plan = plan_ara_komutu(question, history_tail)
    late = None
    try:
        if SEARCH_PARALLEL_PLAN and not history_tail and _first_msg_needs_plan(question):
            late = (_PLAN_POOL.submit(_late_plan_job, (question or '').strip()), time.time(), (question or '').strip())
    except Exception as e:
        print(f'[arama] paralel plan baslatilamadi: {type(e).__name__}: {e}')
        late = None
    return plan, late

# ============================================
# YEDEKLI ARAMA ("?" komutunda bos/zayif sonuc kabul edilmez)
# ============================================
SEARCH_RETRY_DEADLINE = 15  # saniye: yeniden deneme turlarina baslamak icin toplam sure siniri

def _search_is_good(r):
    return bool(r.get('context')) and len(r.get('sources') or []) >= 2

def _keyword_query(question):
    return ' '.join(_query_terms([question])[:6])

def _translate_query_en(question):
    """Sorguyu Ingilizceye cevirir (kucuk model). Basarisizsa None. ASLA exception firlatmaz."""
    keys = get_available_keys(SEARCH_DECISION_MODEL)
    if not keys:
        k = get_active_key()
        keys = [k] if k else []
    for key in keys[:2]:
        payload = {
            'model': SEARCH_DECISION_MODEL,
            'messages': [
                {'role': 'system', 'content': "Translate the user's web search query into natural English. Output ONLY the translated query, nothing else."},
                {'role': 'user', 'content': (question or '')[:300]},
            ],
            'stream': False, 'temperature': 0, 'max_completion_tokens': 300, 'reasoning_effort': 'low',
        }
        try:
            with httpx.Client() as c:
                r = c.post(URL, headers={'Authorization': f'Bearer {key}'}, json=payload, timeout=8)
                if r.status_code == 400:
                    payload.pop('reasoning_effort', None)
                    r = c.post(URL, headers={'Authorization': f'Bearer {key}'}, json=payload, timeout=8)
            if r.status_code != 200:
                continue
            txt = (r.json()['choices'][0]['message'].get('content') or '').strip().strip('"\'')
            if txt:
                line = txt.splitlines()[0].strip()
                if 0 < len(line) <= 300:
                    return line
        except Exception:
            continue
    return None

def _fresh_second_round(plan, question):
    """Ust sonuclar eski yildaysa farkli aciyla (haber <-> genel, yil/ay filtresi) bir kez daha arar.
    ASLA exception firlatmaz."""
    try:
        qs = [x for x in (plan.get('queries') or []) if isinstance(x, str) and x.strip()]
        base = (qs[0] if qs else (question or '')).strip()[:280]
        if not _YEAR_RE.search(base):
            base = f'{base} {_tr_now().year}'
        was_news = bool(plan.get('haber'))
        alt = {'queries': [base], 'zaman': 'yil' if was_news else 'ay', 'haber': not was_news,
               'detay': True, 'guncel': True}
        return search_internet_ex(alt)
    except Exception as e:
        print(f'[arama] ikinci tur hatasi: {type(e).__name__}: {e}')
        return None

def _live_second_round(plan, question):
    """Anlik veri sorgusunda tum kaynaklarin veri saati eskiyse 'canli anlik' ekiyle bir kez daha arar.
    ASLA exception firlatmaz."""
    try:
        qs = [x for x in (plan.get('queries') or []) if isinstance(x, str) and x.strip()]
        base = (qs[0] if qs else (question or '')).strip()[:260]
        if 'canlı' not in base.lower():
            base = f'{base} canlı anlık'
        alt = {'queries': [base], 'zaman': 'gun', 'haber': False, 'detay': False, 'guncel': True}
        return search_internet_ex(alt)
    except Exception as e:
        print(f'[arama] anlik ikinci tur hatasi: {type(e).__name__}: {e}')
        return None

def search_with_fallback(plan, question, late=None):
    """'?' komutu icin: ilk arama bos/yetersiz donerse kademeli yeniden dener
    (filtresiz ham soru -> anahtar kelimeler -> Ingilizce). Donus: search_internet_ex ile ayni sozluk.
    ASLA exception firlatmaz."""
    try:
        t0 = time.time()
        first = search_internet_ex(plan, late)
        if first.get('attempted') and first.get('live_stale') and time.time() - t0 < 9:
            alt = _live_second_round(plan, question)
            if alt and alt.get('context') and alt.get('sources') and not alt.get('live_stale'):
                first = alt  # guncel TEK kaynak bile, hepsi eski olan cok kaynaktan iyidir
        if first.get('attempted') and first.get('stale_top') and time.time() - t0 < 9:
            alt = _fresh_second_round(plan, question)
            if alt and _search_is_good(alt) and not alt.get('stale_top'):
                first = alt
        if _search_is_good(first) or not first.get('attempted'):
            return first
        best = first if first.get('context') else None
        errors = list(first.get('errors') or [])
        q = (question or '').strip()[:300]
        tried = {json.dumps(plan.get('queries'), ensure_ascii=False)}

        def _variants():
            yield {'queries': [q], 'zaman': None, 'haber': False, 'detay': True, 'guncel': bool(plan.get('guncel'))}
            kw = _keyword_query(q)
            if kw:
                yield {'queries': [kw], 'zaman': None, 'haber': False, 'detay': True, 'guncel': bool(plan.get('guncel'))}
            en = _translate_query_en(q)  # tembel: sadece bu tura gelinirse calisir
            if en:
                yield {'queries': [en], 'zaman': None, 'haber': False, 'detay': True, 'guncel': bool(plan.get('guncel'))}

        for p in _variants():
            if time.time() - t0 > SEARCH_RETRY_DEADLINE:
                break
            k = json.dumps(p['queries'], ensure_ascii=False)
            if k in tried:
                continue
            tried.add(k)
            r = search_internet_ex(p)
            errors += r.get('errors') or []
            if r.get('context') and (best is None or len(r.get('sources') or []) > len(best.get('sources') or [])):
                best = r
            if _search_is_good(r):
                break
        return best if best else _empty_search(True, errors)
    except Exception as e:
        print(f'[arama] yedekli arama hatasi: {type(e).__name__}: {e}')
        return _empty_search(True, [f'beklenmeyen hata: {type(e).__name__}'])


# ============================================
# HAFIZA ÖZETLEME
# ============================================
def _async_condense_worker(chat_id, h_copy):
    ozet_talimat = (
        'Bu konusmayi Turkce olarak ozetle. Ozette su bilgileri KESINLIKLE kaybetme: '
        'gecen ozel isimler, sayilar/tarihler/miktarlar ve kullanicinin verdigi ozel '
        'talimatlar veya tercihler (ornegin "boyle yapma", "sunu kullan" gibi istekler). '
        'Geri kalan genel sohbet akisini kisa ve net maddeler halinde ozetle.'
    )
    # Not: h_copy artik SISTEM mesaji icermiyor (sistem mesaji her istekte ayrica
    # ekleniyor), bu yuzden ilk 10 mesaj h_copy[0:10] (eskiden h[1:11] idi).
    p = {'model': 'qwen/qwen3.8-27b', 'messages': h_copy[0:10] + [{'role': 'user', 'content': ozet_talimat}], 'stream': False, 'reasoning_effort': 'none', 'max_completion_tokens': 2048}
    try:
        with httpx.Client() as c:
            r = c.post(URL, headers={'Authorization': f'Bearer {get_active_key()}'}, json=p, timeout=20)
            # 429 gelirse bu key+model kombinasyonunu bir süre dinlenmeye al
            if r.status_code == 429:
                cooldown = None
                ra = r.headers.get('retry-after')
                if ra:
                    try:
                        cooldown = float(ra)
                    except ValueError:
                        cooldown = None
                if not cooldown:
                    cooldown = parse_reset_string(r.headers.get('x-ratelimit-reset-requests')) or DEFAULT_COOLDOWN_SECONDS
                mark_key_cooldown(get_active_key(), 'qwen/qwen3.8-27b', cooldown)
            if r.status_code == 200:
                s = r.json()['choices'][0]['message']['content']
                with history_lock:
                    # Bu sohbet, ozetleme calisirken kullanici tarafindan silinmis
                    # olabilir (deleteChat) - o zaman sessizce vazgec, ayaga kaldirma.
                    if chat_id not in CHAT_HISTORIES:
                        return
                    current = CHAT_HISTORIES.get(chat_id, [])
                    summary_msg = {'role': 'system', 'content': f'[Ozet] {s}', 'is_summary': True}
                    set_chat_history(chat_id, [summary_msg] + current[10:])
    except:
        pass

def condense_memory(chat_id, h):
    if len(h) < 29:
        return h
    h_copy = list(h)
    threading.Thread(target=_async_condense_worker, args=(chat_id, h_copy), daemon=True).start()
    return h

CHAT_HISTORIES = load_all_histories()

# ============================================
# MESAJ SINIFLANDIRMA (kural bazli, ekstra AI istegi YOK -> hiz/kota kaybi yok)
# ============================================
# Basit sohbet/selamlasma kaliplari (bunlarla baslayan ya da bunlardan ibaret kisa mesajlar 'basit' sayilir)
KISA_KALIPLAR = (
    'merhaba', 'selam', 'naber', 'nasilsin', 'nasılsın', 'gunaydin', 'günaydın',
    'iyi geceler', 'iyi aksamlar', 'iyi akşamlar', 'tesekkur', 'teşekkür', 'sagol', 'sağol',
    'ok', 'tamam', 'evet', 'hayir', 'hayır', 'peki', 'anladim', 'anladım'
)
# Analiz/karsilastirma/hesaplama gerektiren istekler icin anahtar kelimeler
ANALIZ_KELIMELERI = (
    'analiz', 'karsilastir', 'karşılaştır', 'raporla', 'rapor', 'hesapla',
    'degerlendir', 'değerlendir', 'incele', 'strateji', 'plan', 'kiyasla', 'kıyasla',
    'istatistik', 'veri', 'neden-sonuc', 'artı eksi', 'arti eksi', 'swot'
)
# Resmi yazisma/metin yazma istekleri icin anahtar kelimeler
YAZISMA_KELIMELERI = (
    'e-posta', 'eposta', 'email', 'mail', 'mektup', 'dilekce', 'dilekçe',
    'resmi yazi', 'resmi yazı', 'basvuru', 'başvuru', 'sozlesme', 'sözleşme',
    'dilekce yaz', 'yazi yaz', 'yazı yaz', 'metin yaz', 'taslak'
)

# Kisa gorunen ama aslinda aciklama/tanim gerektiren sorular icin kaliplar
# (bunlar KELIME SAYISINDAN BAGIMSIZ olarak her zaman tam kapasiteye/kesintisiz akisa yonlendirilir)
TANIM_KALIPLARI = (
    'nedir', 'ne demek', 'kimdir', 'ne ise yarar', 'ne işe yarar',
    'nasil calisir', 'nasıl çalışır', 'nasil bir', 'nasıl bir',
    'aciklar misin', 'açıklar mısın', 'anlatir misin', 'anlatır mısın',
    'ne anlama gelir', 'anlat', 'kimden', 'nereden cikti', 'nereden çıktı',
    'farki ne', 'farkı ne', 'fark nedir', 'farki nedir', 'farkı nedir',
    'avantaj', 'dezavantaj', 'nasil yapilir', 'nasıl yapılır',
    'adimlari', 'adımları', 'adim adim', 'adım adım', 'ornek ver', 'örnek ver'
)

def classify_message(text, has_file=False):
    """Ekstra AI istegi GONDERMEDEN, saf kural/kelime bazli siniflandirma yapar.
    Donen deger: {'complexity': 'basit' | 'karmasik', 'gorev': 'sohbet' | 'yazisma' | 'analiz'}
    Belirsiz kalan her durumda GUVENLI tarafta kalinir (karmasik / sohbet) -> kalite hic dusurulmez,
    sadece NET sekilde basit olan istekler icin tasarruf moduna gecilir."""
    t = (text or '').strip().lower()
    word_count = len(t.split())

    # --- Gorev tipi tespiti ---
    if any(k in t for k in ANALIZ_KELIMELERI):
        gorev = 'analiz'
    elif any(k in t for k in YAZISMA_KELIMELERI):
        gorev = 'yazisma'
    else:
        gorev = 'sohbet'

    # --- Kisa ama aslinda aciklama/tanim gerektiren soru mu? ---
    is_tanim_sorusu = any(k in t for k in TANIM_KALIPLARI)

    # --- Karmasiklik tespiti ---
    if has_file or gorev in ('analiz', 'yazisma') or is_tanim_sorusu:
        # Dosya var, ciddi bir gorev tespit edildi ya da kisa-ama-tanim-sorusu ise:
        # her zaman karmasik/tam kapasite -> kesintisiz akis garanti altina alinir.
        complexity = 'karmasik'
    elif word_count == 0:
        complexity = 'basit'
    elif word_count <= 6 and any(t == k or t.startswith(k) for k in KISA_KALIPLAR):
        complexity = 'basit'
    elif word_count <= 5:
        # Kisa mesajlar (soru isareti olsa da olmasa da) genelde tek-cumlelik,
        # hizli cevap gerektiren istekler olur ("Saat kac?", "Bugun hava nasil?" vb.)
        complexity = 'basit'
    else:
        complexity = 'karmasik'

    return {'complexity': complexity, 'gorev': gorev}

def get_generation_params(model, gorev, token_budget):
    """Modelin taban parametrelerini alir, gorev tipine gore (sohbet/yazisma/analiz)
    ince ayar yapar ve token butcesini (basit sorularda dusuk, karmasikta yuksek) uygular."""
    taban = {
        'openai/gpt-oss-20b':  {'temperature': 0.5, 'top_p': 0.95, 'reasoning_effort': 'low'},
        'qwen/qwen3.8-27b':    {'temperature': 0.3, 'top_p': 0.90, 'reasoning_effort': 'none'},
        'openai/gpt-oss-120b': {'temperature': 0.2, 'top_p': 0.90, 'reasoning_effort': 'low'},
    }
    params = dict(taban.get(model, {'temperature': 0.3, 'top_p': 0.9}))
    params['max_completion_tokens'] = token_budget

    # Gorev tipine gore "yaraticilik" seviyesini ayarla:
    # - sohbet: modelin dogal/varsayilan seviyesi korunur (daha rahat/samimi cevaplar icin)
    # - yazisma: tutarlilik onemli oldugu icin biraz kisilir
    # - analiz: hata payi en tehlikeli oldugu icin en kisik seviyeye cekilir
    if gorev == 'analiz':
        params['temperature'] = max(0.1, round(params['temperature'] - 0.15, 2))
        if model.startswith('openai/gpt-oss'):
            params['reasoning_effort'] = 'medium'  # sadece ciddi analizde daha cok dusunsun
        if model == 'qwen/qwen3.8-27b':
            # Sadece en zor/ciddi analiz isteklerinde derin dusunmeyi kismen ac.
            # Sohbet ve yazisma gorevlerinde 'none' olarak kalmaya devam eder (hiz/kota etkilenmez).
            params['reasoning_effort'] = 'low'
    elif gorev == 'yazisma':
        params['temperature'] = max(0.15, round(params['temperature'] - 0.1, 2))

    # Kullanici Ayarlar menusunden bu MODELE OZEL bir yaraticilik seviyesi
    # belirlediyse (0.0-1.0 arasi), gorev bazli ince ayarlarin UZERINE yazarak
    # son soz onda olsun. Her model kendi kaydedilmis degerini kullanir.
    with settings_lock:
        custom_temp = app_settings.get('temperatures', {}).get(model)
    if custom_temp is not None:
        params['temperature'] = round(max(0.0, min(1.0, custom_temp)), 2)

    return params

# ============================================
# ARKA PLAN CEVAP ISLERI (baglanti kopsa da cevap tamamlanip KAYDEDILSIN)
# ============================================
# Sorun: Telefonda ekran kilitlenince / uygulamadan cikilinca tarayici baglantiyi keser.
# Flask bu durumda cevap uretimini yarida birakiyor, cevap hicbir yere kaydedilmiyordu.
# Cozum: cevap uretimi ayri bir thread'de calisir (baglanti kopsa da sonuna kadar gider
# ve gecmise yazilir). Tarayiciya giden akis bu isin ciktisini okur. Baglanti koparsa
# tarayici /chat/job/<id> adresinden tamamlanmis cevabi geri alir.
# NOT: Isler sunucu hafizasinda tutulur; sunucu yeniden baslarsa (Render deploy/uyku)
# kaybolur - bu durumda tarayici "gonderilemedi" uyarisi gosterir.
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_KEEP_SECONDS = 3600

def _new_job(req_id, chat_id):
    now = time.time()
    job = {
        'id': req_id, 'chat_id': chat_id, 'created': now,
        'status': 'pending',  # pending -> running -> done | error
        'text': '', 'sources': [], 'model': None, 'error': '',
        'events': [], 'finished': False, 'cond': threading.Condition(),
    }
    with JOBS_LOCK:
        for k in [k for k, j in JOBS.items() if now - j['created'] > JOB_KEEP_SECONDS]:
            JOBS.pop(k, None)
        JOBS[req_id] = job
    return job

def _job_worker(job, gen_fn):
    """gen_fn() (SSE metinleri ureten mevcut cevap uretici) tamamen bitene kadar calisir;
    tarayici baglantisinin kopmasindan etkilenmez."""
    try:
        for chunk in gen_fn():
            try:
                if chunk.startswith('data: '):
                    ev = json.loads(chunk[6:].strip())
                    t = ev.get('type')
                    if t == 'delta':
                        job['text'] += ev.get('text', '')
                    elif t == 'model':
                        job['model'] = ev.get('model')
                        job['sources'] = ev.get('sources') or []
                    elif t == 'done':
                        job['status'] = 'done'
                    elif t == 'error':
                        job['status'] = 'error'
                        job['error'] = ev.get('text', '')
            except Exception:
                pass
            with job['cond']:
                job['events'].append(chunk)
                job['cond'].notify_all()
    except Exception as e:
        print(f"[hata] cevap isi coktu: {type(e).__name__}: {e}")
        job['status'] = 'error'
        job['error'] = f'Sunucu hatasi: {type(e).__name__}'
    finally:
        with job['cond']:
            if job['status'] not in ('done', 'error'):
                job['status'] = 'error'
                job['error'] = job['error'] or 'Cevap uretimi beklenmedik sekilde durdu.'
            job['finished'] = True
            job['cond'].notify_all()

def _job_event_stream(job):
    """Isin olaylarini tarayiciya akitir. Tarayici koparsa SADECE bu okuyucu biter,
    is (thread) calismaya devam eder."""
    i = 0
    last_out = time.time()
    while True:
        chunk = None
        finished = False
        with job['cond']:
            if i >= len(job['events']) and not job['finished']:
                job['cond'].wait(timeout=5)
            if i < len(job['events']):
                chunk = job['events'][i]
                i += 1
            elif job['finished']:
                finished = True
        if chunk is not None:
            last_out = time.time()
            yield chunk
        elif finished:
            return
        elif time.time() - last_out > 10:
            last_out = time.time()
            yield ': ping\n\n'  # baglantiyi canli tutar (proxy zaman asimi olmasin)

@app.route('/chat/job/<req_id>', methods=['GET'])
def chat_job(req_id):
    """Tarayici baglantisi koptuktan sonra cevabin durumunu/sonucunu sorar."""
    job = JOBS.get(req_id)
    if not job:
        return jsonify({'status': 'unknown'})
    age = time.time() - job['created']
    status = job['status']
    err = job['error']
    if status == 'pending' and age > 150:
        status, err = 'error', 'Istek islenemedi (zaman asimi).'
    elif status == 'running' and age > 900:
        status, err = 'error', 'Cevap cok uzun surdu (zaman asimi).'
    return jsonify({'status': status, 'text': job['text'], 'sources': job['sources'],
                    'model': job['model'], 'error': err})

# ============================================
# CHAT ENDPOINT
# ============================================
@app.route('/chat', methods=['POST'])
def chat():
    global selected_model
    data = request.get_json()
    user_input = data.get('message', '').strip()
    raw_user_text = user_input  # siniflandirma icin: PDF metni eklenmeden ONCEKI orijinal mesaj
    # Soru isareti (?) komutu: arama SADECE mesaj "?" ile bitiyorsa ve ISTISNASIZ yapilir.
    ara_komutu, ara_sorusu = parse_ara_komutu(user_input)
    if ara_komutu:
        raw_user_text = ara_sorusu  # arama ve siniflandirma icin saf soru metni
    chosen_model = data.get('model', None)
    file_data = data.get('file', None)
    # KRITIK: hangi sohbete ait oldugunu sunucuya soyleyen kimlik. Frontend
    # bunu HER istekte gonderir (bkz. sendMsg). Bos/eksik gelirse (ör. eski bir
    # istemci surumu) sohbetleri birbirine karistirmamak icin sabit bir "default"
    # kovasina duser - boylece en kotu ihtimalle eski tek-sohbet davranisina
    # geri donulur, asla FARKLI sohbetler ayni kovaya karismaz.
    chat_id = str(data.get('chat_id') or 'default').strip() or 'default'
    # Tarayicinin urettigi benzersiz istek kimligi: baglanti koparsa cevabi bununla geri alir.
    req_id = str(data.get('req_id') or uuid.uuid4().hex)[:64]
    job = _new_job(req_id, chat_id)
    # Onemli: arayuz artik HER istekte acikca 'model' bilgisi gonderiyor -
    # gercek bir model adi (kullanici elle secti) VEYA null (Otomatik mod secili).
    # Bu yuzden burada SADECE dolu geldiginde degil, HER durumda (null dahil) guncelliyoruz;
    # aksi halde onceki manuel secim "yapiskan" kalir ve Otomatik moda hic gecilemezdi.
    selected_model = chosen_model
    use_vision = False
    v_b64 = None
    v_mime = None
    if file_data:
        ft = file_data.get('type', '')
        rb = file_data.get('data', '')
        fb = base64.b64decode(rb) if rb else b''
        if 'pdf' in ft:
            pdf_text = parse_pdf_bytes(fb)
            pdf_text = summarize_long_text(pdf_text)
            user_input = (user_input + '\n\n' if user_input else '') + f'[PDF Dosyasi]: {pdf_text}'
        elif ft.startswith('image/'):
            use_vision = True
            v_b64 = rb
            v_mime = ft if ft else 'image/jpeg'
        if not user_input and use_vision:
            user_input = 'Bu gorseli detaylica acikla.'

    with history_lock:
        conv = list(CHAT_HISTORIES.get(chat_id, []))
        if use_vision and v_b64:
            conv.append({'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': f'data:{v_mime};base64,{v_b64}'}}, {'type': 'text', 'text': user_input}]})
        else:
            conv.append({'role': 'user', 'content': user_input})
        conv = condense_memory(chat_id, conv)
        set_chat_history(chat_id, conv)
        # Sistem mesaji ARTIK burada, istek anında TAZE olarak ekleniyor - hicbir
        # sohbetin gecmisinde saklanmiyor (bkz. HAFIZA YONETIMI notlari).
        curr = [get_active_system_message()] + conv

    def inject_time_into_last_message(msgs, extra_context=None, search_note=None):
        """Zaman bilgisini (ve varsa internet arama sonucunu) mesaj dizisinin BASINA
        degil, SADECE en son (yeni) mesaja ekler. Boylece sistem mesaji ve gecmis
        konusma her istekte birebir ayni prefix'i korur ve Groq'un otomatik prompt
        caching ozelligi calisabilir (aksi halde her istekte degisen bir zaman
        damgasi/arama sonucu, cache'i bastan kirar)."""
        msgs = [dict(mm) for mm in msgs]
        note = f'[Güncel tarih/saat (Türkiye, UTC+3; yalnızca iç bilgi, kullanıcı sormadıkça yanıtta belirtme): {_tr_now_str()}] '
        if extra_context:
            note += (
                '[İNTERNET ARAMA SONUÇLARI (Google + web; arama bugünün tarihinde yapıldı; sonuçlar soruya alakaya göre sıralıdır). KURALLAR: '
                '0) Kullanıcının mesajı soru işaretiyle (?) bitiyorsa bu yalnızca arama tetikleyicisidir; sorunun kendisini bu sonuçlara dayanarak yanıtla. '
                '1) İlk cümlede doğrudan cevabı ver; ardından yalnızca soruyu tamamlayan en önemli ayrıntıları ekle. Giriş cümlesi, soruyu tekrar etme ve dolgu yazma. '
                '2) Cevabı YALNIZCA bu sonuçlara ve konuşmaya dayandır; sonuçlarda olmayan tarih, sayı, isim, '
                'sürüm veya iddiayı kendi hafızandan EKLEME. '
                '3) Rakam, tarih ve özel isimleri kaynaktan aynen aktar. VERİ TARİHİ: zamanla değişebilen her bilgide (rakam, oran, fiyat, kur, skor, hava durumu, enflasyon, faiz, yasa, sürüm, kişi veya kurum görevi) cevabın içinde verinin hangi tarih için geçerli olduğunu doğal bir ifadeyle yaz, örneğin "3 Ekim itibarıyla", "Eylül 2026 verisine göre" veya anlık verilerde "bugün saat 14.30 itibarıyla". Dönemi anlatan verilerde (aylık enflasyon, çeyreklik büyüme gibi) verinin ait olduğu dönemi, anlık verilerde sayfanın güncelleme tarihini ve saatini kullan; ikisi de varsa dönemi yaz. Tarihi yalnızca kaynak başlığındaki tarih etiketlerinden, CANLI SAYFA satırından veya kaynak metninden al; hiçbirinde yoksa tarih uydurma, bugünün tarihini veriye atfetme ve verinin tarihinin belirsiz olduğunu tek kısa ifadeyle söyle. Tarihi ayrı bir satır veya dipnot yapma, değerin bulunduğu cümleye ekle. Zamandan bağımsız bilgilerde (tanım, tarihçe, nasıl yapılır) tarih yazma. '
                '4) Soruyla ilgisiz görünen sonuçları ve aynı isimli farklı kişi, ürün veya yer karışıklıklarını yok say. Birden çok kaynakta tekrarlanan bilgiye daha çok güven; '
                'Google\'ın \"öne çıkan cevap\" satırı güçlü bir ipucudur ama diğer sonuçlarla çelişiyorsa onlarla doğrula. '
                '5) Kaynaklar çelişirse en yeni ve en güvenilir olanı tercih et ve çelişkiyi tek cümleyle belirt. Bilgi eski tarihliyse tarihini belirt. '
                '6) Sonuçlar soruya kısmen cevap veriyorsa bulduğunu net biçimde ver; yalnızca eksik kalan kısım için \"aramada net bilgi bulunamadı\" de. Bulunan bilgiyi saklama, eksik kısmı tahmin etme. '
                '6b) DİL VE BİÇİM: cevabı yalnızca Türkçe yaz; kaynaktaki İngilizce metni ve tarihi Türkçeye çevir (03 Oct 2026 yerine 3 Ekim 2026). Uzun tire (—) ve kısa tire (–) kullanma. '
                '6c) ANLIK VERİ KURALI (fiyat, kur, skor, hava durumu, borsa gibi sürekli değişen her veri): bir kaynakta CANLI SAYFA satırı varsa o metin şu anda sitenin kendisinden okunmuştur ve arama özetinden DAHA GÜNCELDİR; rakamı CANLI SAYFA satırından al. Arama özeti satırındaki rakamı yalnızca CANLI SAYFA satırında o veri yoksa kullan ve bunun eski olabileceğini belirt. Arama özetindeki eski bir saati (örneğin sabah saati) güncelmiş gibi yazma. Farklı kaynaklarda rakam farklıysa CANLI SAYFA satırlarını karşılaştır, aralığı veya en güvenilir canlı değeri ver. '
                '6c2) ANLIK VERİ GÜNCELLİĞİ (KESİN KURAL): CANLI SAYFA etiketi sayfanın şu anda okunduğunu gösterir, içindeki rakamın o anki olduğunu göstermez. Başlıktaki "veri saati" verinin gerçek saatidir. 60 dakikadan eski veri saatli veya VERİ SAATİ ESKİ etiketli kaynağı güncel değer olarak sunma. Yalnızca en yeni veri saatli kaynağın rakamını ver ve saatini yaz. Farklı saatlerin rakamlarını aynı cümlede karıştırma. Bir değerin yükseldiğini veya düştüğünü yalnızca iki rakam gerçekten farklıysa ve aynı kaynaktan geliyorsa söyle. Güncel saatli kaynak yoksa "ulaşabildiğim en son veri saat X, anlık olmayabilir" diye açıkça belirt, bunu güncelmiş gibi yazma. Borsa kapalıysa değeri kapanış değeri olarak belirt. '
                '6d) CEVAP BİÇİMİ: kaynaklar arasında güncelleme saati farklıysa EN YENİ saatli değeri kullan. Sayfada yazan son güncelleme saati okuma saatinden belirgin eskiyse (örneğin piyasa kapalı olabilir) bunu tek kısa cümleyle söyle. Kur ve fiyatları en fazla 2 ondalık basamakla yaz (49,0900 değil 49,09 TL). Cevabın içine site adı, alan adı veya parantezli kaynak yazma. Cevap kısa olsun: önce değer, sonra en fazla bir açıklama cümlesi. '
                '6e) YIL KONTROLÜ: kullanıcı güncel bir veri sorduysa sonuçlardaki yılı kontrol et. Başlığında ESKİ DÖNEM yazan veya yılı bugünün yılından eski olan sonucu yalnızca güncel dönemde karşılığı yoksa kullan ve yılını açıkça belirt; bugünün yılına ait sonuç varsa rakamı ondan al. '
                '6f) TARİH ETİKETLERİ: kaynak başlığındaki "sayfa yayın tarihi" sayfanın kendi kodundan okunmuş yayın tarihidir; "sayfa güncelleme" içeriğin sonradan düzenlendiği tarihtir ve içeriğin yeni olduğunu kanıtlamaz. "düşük güven" yazan tarihi yalnızca ipucu say. "tarihi belirsiz" yazan kaynağın ne zaman yazıldığı bilinmiyor: haber, fiyat, kur, skor, mevzuat gibi zamana bağlı bir iddiada bunu tek başına dayanak yapma, tarihli bir kaynakla doğrula; tarihli kaynak yoksa kullan ve cevabın sonunda tek kısa ifadeyle tarihinin belirsiz olduğunu söyle. Tarihli ve tarihsiz kaynak çelişirse tarihli olanı tercih et. Zamandan bağımsız bilgide (tanım, tarihçe, nasıl yapılır) tarihsizlik sorun değildir, uyarı yazma. CANLI SAYFA satırı olan anlık veri sayfalarında 6c önceliklidir; böyle sayfanın yayın tarihi eski görünse bile canlı rakamı kullan. '
                '7) KAYNAK BELİRTME KONUSUNDA KESİN KURAL: cevap metninin içine ASLA \"(Kaynak: ...)\" gibi satır içi '
                'kaynak ekleme, ASLA kendi \"Kaynak;\" başlığı/linki oluşturma ve ASLA herhangi bir URL yazma - kaynak '
                'linkleri cevabının sonuna SİSTEM TARAFINDAN OTOMATİK olarak eklenecek, bunu sen yapma, sadece soruyu '
                f'normal şekilde yanıtla:\n{extra_context}\n] '
            )
        elif search_note:
            note += f'[{search_note}] '
        last = dict(msgs[-1])
        if isinstance(last.get('content'), str):
            last['content'] = note + last['content']
        elif isinstance(last.get('content'), list):
            new_content = []
            injected = False
            for part in last['content']:
                part = dict(part)
                if part.get('type') == 'text' and not injected:
                    part['text'] = note + part.get('text', '')
                    injected = True
                new_content.append(part)
            last['content'] = new_content
        msgs[-1] = last
        return msgs

    # ============================================
    # OTOMATIK SINIFLANDIRMA (ekstra AI istegi yok, saf kural bazli)
    # ============================================
    sinif = classify_message(raw_user_text, has_file=bool(file_data))
    # Basit istekte kucuk token butcesi (kota tasarrufu); karmasikta tam kapasite.
    token_budget = 900 if sinif['complexity'] == 'basit' else 4096

    # ============================================
    # INTERNET ARAMASI - SADECE mesaj "?" ile bitince, ISTISNASIZ.
    # Kelime/soru bazli "gerekli mi" kurali ve model karari YOK. Komut varsa arama her zaman
    # yapilir (arayuzdeki ac/kapa anahtari, dosya veya gorsel de engellemez); sorgu plani
    # (NE aranacagi) kucuk bir modelle uretilir, o basarisiz olsa bile kullanicinin sorusu aranir.
    # Bos/zayif sonucta kademeli yeniden deneme yapilir (bkz. search_with_fallback).
    # Bir kere hesaplanir (chain icindeki her model denemesinde AYNI sonuc kullanilir).
    # ============================================
    arama_sonucu = ""
    arama_kaynaklari = []
    arama_notu = ""
    if ara_komutu and raw_user_text.strip():
        arama_late = None
        try:
            arama_plani, arama_late = plan_ara_komutu_late(raw_user_text, history_tail=conv[:-1][-4:])
        except Exception as _e:
            print(f'[arama] plan hatasi: {type(_e).__name__}: {_e}')
            arama_plani = _apply_fresh_rules(_default_plan(raw_user_text), raw_user_text)
            arama_late = None
        _t_arama = time.time()
        _sr = search_with_fallback(arama_plani, raw_user_text, arama_late)
        print(f'[arama] komut toplam sure (plan haric): {time.time() - _t_arama:.1f}s')
        arama_sonucu = _sr.get('context') or ''
        arama_kaynaklari = _sr.get('sources') or []
        if not arama_sonucu:
            # Tum yedekler denendi ama sonuc/servis hatasi: model sessizce eski hafizasina dusmesin.
            arama_notu = (
                'ARAMA UYARISI: Kullanıcı mesajını soru işaretiyle (?) bitirerek internet araması istedi (sorunun '
                'kendisini yanıtla) fakat tüm denemelere rağmen kullanılabilir sonuç alınamadı '
                '(servis hatası, anahtar eksikliği veya boş sonuç). Güncel/spesifik bilgileri kendi hafızandan '
                'tahmin ETME; kullanıcıya aramada sonuç alınamadığını açıkça söyle, bilgin eski olabileceğini '
                'belirt ve sadece emin olduğun genel bilgiyi bunu belirterek ver.'
            )
    if arama_sonucu:
        # Arama destekli cevaplar daha ayrintili olur; kisa-soru butcesi (900) yetmez.
        token_budget = max(token_budget, 2048)

    def gen():
        succ = False
        if use_vision:
            chain = [VISION_MODEL]
        elif selected_model:
            # Kullanici manuel model sectiyse HER ZAMAN ona saygi gosterilir; siniflandirma
            # sadece parametre (temperature/token) ince ayarinda kullanilir, chain sirasi degismez.
            chain = [selected_model] + [m for m in MODELS_CHAIN if m != selected_model]
        elif sinif['complexity'] == 'karmasik' or arama_sonucu:
            # Karmasik/ciddi (ya da arama sonucuna dayali sentez gerektiren) istekte dogrudan daha guclu bir modelle basla; en kucuk model
            # sadece diger ikisi de basarisiz olursa son care olarak denenir.
            chain = ['qwen/qwen3.8-27b', 'openai/gpt-oss-120b', 'openai/gpt-oss-20b']
        else:
            # Basit/gundelik istekte zaten en hizli/kucuk model en basta (MODELS_CHAIN sirasi).
            chain = MODELS_CHAIN
        last_err = 'bilinmeyen hata'
        GPT20B_FIX_MSG = {'role': 'system', 'content': 'EK HATIRLATMA: Baslik satirlarinda emoji veya sembol kullanma, sadece duz metin baslik yaz. "Onerilen:" veya "Not:" gibi ayri bir etiket/kutu olusturma; oneri niteligindeki bilgiyi normal cumle akisi icinde ver.'}

        # Tam (ozetleme sistemi tarafindan zaten sinirlanan) gecmisi kullaniyoruz; kor "son 5 mesaj"
        # kesmesi yapmiyoruz cunku bu, her istekte pencereyi kaydirip prompt caching'i bozuyordu.

        for m in chain:
            base_msgs = (curr[:1] + [GPT20B_FIX_MSG] + curr[1:]) if m == 'openai/gpt-oss-20b' else curr
            req_messages = inject_time_into_last_message(base_msgs, extra_context=arama_sonucu, search_note=arama_notu)

            p = {'model': m, 'messages': req_messages, 'stream': True}
            p.update(get_generation_params(m, sinif['gorev'], token_budget))

            # --- MODEL ONCELIKLI KEY ROTASYONU ---
            # Bu model icin su an musait olan (limiti dolmamis) key'leri sirayla dene.
            # Biri 429 (limit) hatasi verirse SADECE key degisir, model degismez.
            # Baska (limit disi) bir hata olursa key'leri bosuna denemeden siradaki modele gecilir.
            available_keys = get_available_keys(m)
            if not available_keys:
                last_err = f'{m}: musait anahtar yok (uc anahtar da bu model icin limitli)'
                continue  # siradaki modele gec

            model_had_non_rate_limit_error = False

            for key in available_keys:
                try:
                    yield f"data: {json.dumps({'type': 'model', 'model': m, 'searched': bool(arama_sonucu), 'sources': arama_kaynaklari})}\n\n"
                    start_ts = time.time()
                    full_ai_response = ""
                    char_count = 0

                    # Onemli: sabit tek bir sayi yerine ayri connect/read/write sureleri kullaniyoruz.
                    # Uzun ve detayli (tam kapasite) yanitlarda modelin iki chunk arasinda 45sn'den
                    # uzun durakladigi durumlar oluyordu; bu da akisin ortasinda zaman asimina
                    # (ReadTimeout) dusup cevabin yarida kesilmesine/donmus gibi gorunmesine sebep
                    # oluyordu. Read suresini cömertce yukselttik.
                    req_timeout = httpx.Timeout(connect=10.0, read=110.0, write=15.0, pool=15.0)

                    with _GROQ_CLIENT.stream('POST', URL, headers={'Authorization': f'Bearer {key}'}, json=p, timeout=req_timeout) as r:
                        if r.status_code == 429:
                            cooldown = None
                            ra = r.headers.get('retry-after')
                            if ra:
                                try:
                                    cooldown = float(ra)
                                except ValueError:
                                    cooldown = None
                            if not cooldown:
                                cooldown = parse_reset_string(r.headers.get('x-ratelimit-reset-requests')) or DEFAULT_COOLDOWN_SECONDS
                            mark_key_cooldown(key, m, cooldown)
                            last_err = f'{m}: bir anahtarin limiti doldu, digerine geciliyor ({cooldown:.0f}sn sonra tekrar denenecek)'
                            continue  # ayni modelde SIRADAKI key'i dene

                        if r.status_code != 200:
                            try:
                                err_body = r.read().decode('utf-8', errors='ignore')[:300]
                            except:
                                err_body = ''
                            if r.status_code == 400 and 'reasoning' in err_body.lower() and 'reasoning_effort' in p:
                                p.pop('reasoning_effort', None)  # parametre desteklenmiyorsa cikarip ayni modeli tekrar dene
                                continue
                            last_err = f'HTTP {r.status_code}: {err_body}'
                            model_had_non_rate_limit_error = True
                            break  # bu key'in sorunu degil, modelin/isteklin sorunu -> siradaki modele gec

                        for line in r.iter_lines():
                            if not line or not line.startswith('data: '):
                                continue
                            chunk = line[6:]
                            if chunk.strip() == '[DONE]':
                                break
                            try:
                                parsed_chunk = json.loads(chunk)
                                choice0 = parsed_chunk['choices'][0]
                                d = choice0['delta'].get('content', '')
                                if d:
                                    full_ai_response += d
                                    char_count += len(d)
                                    yield f"data: {json.dumps({'type': 'delta', 'text': d})}\n\n"
                            except:
                                pass

                    # Kaynak linkleri modele birakilmiyor - sunucu SUNUCU TARAFINDA
                    # deterministik/eksiksiz olarak ekliyor (bkz. build_kaynak_markdown).
                    kaynak_md = build_kaynak_markdown(arama_kaynaklari) if arama_sonucu else ''
                    if kaynak_md:
                        full_ai_response += kaynak_md
                        yield f"data: {json.dumps({'type': 'delta', 'text': kaynak_md})}\n\n"

                    total_elapsed = round(time.time() - start_ts, 1)
                    final_tok = round(char_count / 4)
                    final_tps = round(final_tok / total_elapsed, 1) if total_elapsed > 0 else 0

                    with history_lock:
                        updated = list(CHAT_HISTORIES.get(chat_id, []))
                        updated.append({'role': 'assistant', 'content': full_ai_response})
                        set_chat_history(chat_id, updated)

                    yield f"data: {json.dumps({'type': 'done', 'tps': final_tps, 'elapsed': total_elapsed, 'tokens': final_tok})}\n\n"
                    succ = True
                    break  # key dongusunden basariyla cik

                except Exception as ex:
                    last_err = str(ex)
                    print(f"[hata] Model {m} / anahtar ...{key[-6:]} basarisiz: {last_err}")
                    model_had_non_rate_limit_error = True
                    # Onemli: eger akisin bir kismi zaten kullaniciya gönderildiyse (full_ai_response dolu),
                    # baska bir modele GECMIYORUZ. Aksi halde frontend ayni mesaj balonuna farkli bir
                    # modelin cevabini ekleyip birbirine karisan/tutarsiz bir metin ortaya cikariyordu.
                    # Bunun yerine elimizdeki kismi cevabi kalici kabul edip duzgunce sonlandiriyoruz;
                    # boylece kullanici en azindan o ana kadar uretilen metni kaybetmiyor.
                    if full_ai_response:
                        kaynak_md = build_kaynak_markdown(arama_kaynaklari) if arama_sonucu else ''
                        if kaynak_md:
                            full_ai_response += kaynak_md
                            yield f"data: {json.dumps({'type': 'delta', 'text': kaynak_md})}\n\n"
                        total_elapsed = round(time.time() - start_ts, 1)
                        final_tok = round(char_count / 4)
                        final_tps = round(final_tok / total_elapsed, 1) if total_elapsed > 0 else 0
                        with history_lock:
                            updated = list(CHAT_HISTORIES.get(chat_id, []))
                            updated.append({'role': 'assistant', 'content': full_ai_response})
                            set_chat_history(chat_id, updated)
                        yield f"data: {json.dumps({'type': 'done', 'tps': final_tps, 'elapsed': total_elapsed, 'tokens': final_tok})}\n\n"
                        succ = True
                    break  # network/timeout gibi bir sorun; key'leri bosuna denemeden modelden cik

            if succ:
                break  # model dongusunden basariyla cik
            # succ olmadiysa (ya tum keyler 429 yedi, ya da bir key'de baska hata cikip modelden cikildi)
            # -> dogal olarak siradaki modele geciliyor (for m in chain devam eder)

        if not succ:
            yield f"data: {json.dumps({'type': 'error', 'text': 'Su an tum modeller/anahtarlar mesgul. Detay: ' + last_err})}\n\n"

    job['status'] = 'running'
    threading.Thread(target=_job_worker, args=(job, gen), daemon=True).start()
    return Response(_job_event_stream(job), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive'
    })

# ============================================
# GEÇMİŞ TEMİZLEME
# ============================================
@app.route('/history/clear', methods=['POST'])
def clear_history():
    """Frontend'deki 'Sohbetleri Temizle' butonu TUM sohbetleri sifirlar
    (chats dizisini bosaltip tek bir 'Yeni Sohbet' acar), bu yuzden burada da
    TUM sohbetlerin sunucu tarafi gecmisi silinir - istenen davranisla tutarli."""
    global CHAT_HISTORIES
    with history_lock:
        CHAT_HISTORIES = {}
        save_all_histories(CHAT_HISTORIES)
    return jsonify({'ok': True})

@app.route('/history/delete-chat', methods=['POST'])
def delete_chat_history():
    """Kullanici sidebar'dan TEK bir sohbeti sildiginde (deleteChat), o sohbete
    ait sunucu tarafi gecmisi de temizler. Boylece silinen sohbetlerin verisi
    diskte sonsuza kadar birikip yer kaplamaz (kucuk ama gercek bir temizlik)."""
    data = request.get_json(force=True, silent=True) or {}
    chat_id = str(data.get('chat_id') or '').strip()
    if not chat_id:
        return jsonify({'ok': False, 'error': 'chat_id gerekli'}), 400
    with history_lock:
        CHAT_HISTORIES.pop(chat_id, None)
        save_all_histories(CHAT_HISTORIES)
    return jsonify({'ok': True})

# ============================================
# UST BAR: KONUM ADI + SICAKLIK (otomatik)
# ============================================
# Akis: tarayici konum izni verirse (lat, lon) gonderir; vermezse sunucu, telefonun
# genel IP'sinden yaklasik (il duzeyinde) konum bulur. Sonra:
#   - Yer adi (ilce, il): OpenStreetMap Nominatim (yedek: BigDataCloud), Turkce
#   - Anlik hava durumu (sicaklik, hava kodu, ruzgar, hissedilen) + saatlik tahmin: Open-Meteo
#     (anahtar gerektirmez; yedek: MET Norway)
# Sonuclar bellekte kisa sure onbelleklenir; boylece servisler gereksiz yere yorulmaz.
_wx_cache = {}
_wx_cache_lock = threading.Lock()
_WX_UA = {'User-Agent': 'KararStratejiAsistani/1.0 (kisisel kullanim)', 'Accept-Language': 'tr'}

def _wx_cache_get(key):
    with _wx_cache_lock:
        item = _wx_cache.get(key)
        if item and item[0] > time.time():
            return item[1]
        if item:
            _wx_cache.pop(key, None)
    return None

def _wx_cache_set(key, val, ttl):
    with _wx_cache_lock:
        if len(_wx_cache) > 200:
            _wx_cache.clear()
        _wx_cache[key] = (time.time() + ttl, val)

def _wx_get_json(url, params=None, timeout=6.0):
    r = httpx.get(url, params=params, headers=_WX_UA, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.json()

# NOT: Eskiden burada IP'den yaklasik konum bulan bir yedek vardi, ama sunucu (Render)
# kendi IP'sini sorguladigi icin telefonun degil, sunucunun bulundugu yeri (Almanya/Hessen)
# donduruyordu. Yanlis konum gostermektense hic gostermemek icin kaldirildi; konum artik
# sadece telefonun GPS/konum izninden gelen koordinatlarla bulunur.

def _wx_reverse(lat, lon):
    """(ilce, il) doner; bulunamazsa ('', '')."""
    key = ('rev', round(lat, 3), round(lon, 3))
    cached = _wx_cache_get(key)
    if cached is not None:
        return cached
    district, city = '', ''
    try:
        d = _wx_get_json('https://nominatim.openstreetmap.org/reverse', params={
            'format': 'jsonv2', 'lat': lat, 'lon': lon, 'zoom': 14,
            'addressdetails': 1, 'accept-language': 'tr'}, timeout=6.0)
        a = d.get('address') or {}
        for k in ('county', 'town', 'city_district', 'municipality', 'suburb', 'village'):
            if a.get(k):
                district = str(a[k]).strip()
                break
        for k in ('province', 'state', 'city'):
            if a.get(k):
                city = str(a[k]).strip()
                break
    except Exception as e:
        print(f"[UYARI] Nominatim basarisiz: {e}")
    if not district and not city:
        try:
            d = _wx_get_json('https://api.bigdatacloud.net/data/reverse-geocode-client', params={
                'latitude': lat, 'longitude': lon, 'localityLanguage': 'tr'}, timeout=6.0)
            district = (d.get('city') or d.get('locality') or '').strip()
            city = (d.get('principalSubdivision') or '').strip()
            for suffix in (' Province', ' Ili', ' İli'):
                city = city.replace(suffix, '')
        except Exception as e:
            print(f"[UYARI] BigDataCloud basarisiz: {e}")
    if district or city:
        _wx_cache_set(key, (district, city), 6 * 3600)
    return district, city

def _wx_num(v, nd=1):
    """Sayiya cevirir; gecersizse None."""
    try:
        if v is None:
            return None
        f = float(v)
        if f != f or f in (float('inf'), float('-inf')):
            return None
        return round(f, nd)
    except Exception:
        return None

def _wx_int(v):
    n = _wx_num(v, 0)
    return None if n is None else int(n)

# MET Norway sembol adlarini WMO hava koduna cevirir (arayuz tek bir kod sistemiyle calisir).
_MET_SIMPLE = {'clearsky': 0, 'fair': 1, 'partlycloudy': 2, 'cloudy': 3, 'fog': 45}

def _met_symbol_to_wmo(sym):
    s = str(sym or '').split('_')[0].lower()
    if not s:
        return None
    if s in _MET_SIMPLE:
        return _MET_SIMPLE[s]
    if 'thunder' in s:
        return 95
    heavy = s.startswith('heavy')
    light = s.startswith('light')
    shower = 'showers' in s
    if 'sleet' in s:
        return 67
    if 'snow' in s:
        if heavy:
            return 86 if shower else 75
        if shower:
            return 85
        return 71 if light else 73
    if 'rain' in s:
        if heavy:
            return 82 if shower else 65
        if shower:
            return 80 if light else 81
        return 61 if light else 63
    return None

def _wx_fetch_openmeteo(lat, lon):
    d = _wx_get_json('https://api.open-meteo.com/v1/forecast', params={
        'latitude': lat, 'longitude': lon,
        'current': 'temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m',
        'hourly': 'temperature_2m,weather_code,precipitation_probability,uv_index',
        'daily': 'sunrise,sunset',
        'forecast_days': 2, 'timeformat': 'unixtime', 'timezone': 'auto'}, timeout=6.0)
    cur = d.get('current') or {}
    t = _wx_num(cur.get('temperature_2m'))
    if t is None:
        raise ValueError('sicaklik yok')
    h = d.get('hourly') or {}
    times = h.get('time') or []
    temps = h.get('temperature_2m') or []
    codes = h.get('weather_code') or []
    pps = h.get('precipitation_probability') or []
    uvs = h.get('uv_index') or []
    hourly = []
    for i, ts in enumerate(times):
        ts_i = _wx_int(ts)
        if ts_i is None:
            continue
        hourly.append({
            'ts': ts_i,
            't': _wx_num(temps[i]) if i < len(temps) else None,
            'code': _wx_int(codes[i]) if i < len(codes) else None,
            'pp': _wx_int(pps[i]) if i < len(pps) else None,
            'uv': _wx_num(uvs[i]) if i < len(uvs) else None,
        })
    dly = d.get('daily') or {}
    sun = {
        'rise': [x for x in (_wx_int(v) for v in (dly.get('sunrise') or [])) if x is not None],
        'set': [x for x in (_wx_int(v) for v in (dly.get('sunset') or [])) if x is not None],
    }
    is_day = cur.get('is_day')
    return {
        't': t,
        'feels': _wx_num(cur.get('apparent_temperature')),
        'code': _wx_int(cur.get('weather_code')),
        'day': True if is_day is None else bool(is_day),
        'wind': _wx_int(cur.get('wind_speed_10m')),
        'hourly': hourly,
        'sun': sun,
        'src': 'om',
    }

def _wx_fetch_met(lat, lon):
    d = _wx_get_json('https://api.met.no/weatherapi/locationforecast/2.0/compact', params={
        'lat': round(lat, 4), 'lon': round(lon, 4)}, timeout=8.0)
    series = ((d.get('properties') or {}).get('timeseries')) or []
    if not series:
        raise ValueError('bos zaman serisi')

    def _entry(item):
        data = item.get('data') or {}
        det = (data.get('instant') or {}).get('details') or {}
        blk = data.get('next_1_hours') or data.get('next_6_hours') or data.get('next_12_hours') or {}
        sym = (blk.get('summary') or {}).get('symbol_code')
        ts = int(datetime.fromisoformat(str(item.get('time')).replace('Z', '+00:00')).timestamp())
        return ts, det, sym

    ts0, det0, sym0 = _entry(series[0])
    t = _wx_num(det0.get('air_temperature'))
    if t is None:
        raise ValueError('sicaklik yok')
    wind_ms = _wx_num(det0.get('wind_speed'), 2)
    hourly = []
    for item in series:
        try:
            ts, det, sym = _entry(item)
        except Exception:
            continue
        if ts > ts0 + 60 * 3600:
            break
        hourly.append({'ts': ts, 't': _wx_num(det.get('air_temperature')),
                       'code': _met_symbol_to_wmo(sym), 'pp': None})
    return {
        't': t,
        'feels': None,
        'code': _met_symbol_to_wmo(sym0),
        'day': not str(sym0 or '').endswith('_night'),
        'wind': None if wind_ms is None else int(round(wind_ms * 3.6)),
        'hourly': hourly,
        'src': 'met',
    }

def _wx_weather(lat, lon):
    """Anlik hava durumu + saatlik tahmin (dict) ya da None.
    Alanlar: t (C), feels, code (WMO), day (gunduz mu), wind (km/sa), hourly [{ts, t, code, pp}]."""
    key = ('wx', round(lat, 2), round(lon, 2))
    cached = _wx_cache_get(key)
    if cached is not None:
        return cached
    try:
        w = _wx_fetch_openmeteo(lat, lon)
        _wx_cache_set(key, w, 600)
        return w
    except Exception as e:
        print(f"[UYARI] Hava durumu alinamadi (Open-Meteo): {e}")
    # Yedek saglayici: Open-Meteo, Render gibi paylasimli bulut IP'lerini zaman zaman engelleyebiliyor.
    try:
        w = _wx_fetch_met(lat, lon)
        _wx_cache_set(key, w, 600)
        return w
    except Exception as e:
        print(f"[UYARI] Hava durumu alinamadi (MET Norway): {e}")
    return None

@app.route('/api/weather', methods=['GET'])
def api_weather():
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        approx = False
        if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return jsonify({'ok': False, 'error': 'konum yok'})
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_place = ex.submit(_wx_reverse, lat, lon)
            f_wx = ex.submit(_wx_weather, lat, lon)
            district, city = f_place.result()
            wx = f_wx.result()
        temp = wx['t'] if wx else None
        if approx:
            district = ''  # IP konumu ilce duzeyinde guvenilir degil; sadece il gosterilir
        if not district and not city and temp is None:
            return jsonify({'ok': False, 'error': 'veri alinamadi'})
        return jsonify({'ok': True, 'district': district, 'city': city, 'temp': temp, 'wx': wx, 'approx': approx})
    except Exception as e:
        print(f"[UYARI] /api/weather hatasi: {e}")
        return jsonify({'ok': False, 'error': 'hata'})

# ============================================
# AYARLAR (sistem promptu / yaraticilik seviyesi)
# ============================================
@app.route('/settings', methods=['GET'])
def get_settings():
    with settings_lock:
        current_temps = dict(app_settings.get('temperatures', {}))
    return jsonify({
        'system_prompt': get_active_system_prompt(),
        'default_system_prompt': DEFAULT_SYSTEM_MSG['content'],
        'temperatures': current_temps,
        'default_temperatures': MODEL_TEMP_DEFAULTS,
        'model_names': MODEL_DISPLAY_NAMES
    })

@app.route('/settings', methods=['POST'])
def update_settings():
    data = request.get_json(force=True, silent=True) or {}

    with settings_lock:
        if 'system_prompt' in data:
            sp = data.get('system_prompt')
            sp = sp.strip() if isinstance(sp, str) else None
            # Varsayilanla ayni metin kaydedilirse ozel talimat olarak saklama: boylece
            # varsayilan guncellendiginde eski kopya yeni talimati ezmez.
            if sp and (sp == DEFAULT_SYSTEM_MSG['content'].strip() or sp in [x.strip() for x in _LEGACY_DEFAULT_PROMPTS]):
                sp = None
            app_settings['system_prompt'] = sp if sp else None

        if 'temperatures' in data and isinstance(data['temperatures'], dict):
            for model_id, val in data['temperatures'].items():
                if model_id not in MODEL_TEMP_DEFAULTS:
                    continue
                if val is None:
                    app_settings.setdefault('temperatures', {}).pop(model_id, None)
                else:
                    try:
                        app_settings.setdefault('temperatures', {})[model_id] = round(max(0.0, min(1.0, float(val))), 2)
                    except (TypeError, ValueError):
                        pass

        save_settings(app_settings)
        new_prompt = app_settings.get('system_prompt') or DEFAULT_SYSTEM_MSG['content']
        new_temps = dict(app_settings.get('temperatures', {}))

    # Not: artik sistem mesaji hicbir sohbetin gecmisinde SAKLANMADIGI icin
    # (bkz. HAFIZA YONETIMI), burada ayrica bir gecmis guncellemesi gerekmiyor.
    # get_active_system_message() zaten her /chat isteginde app_settings'ten
    # TAZE okunuyor - yeni prompt otomatik olarak TUM sohbetlere, bir sonraki
    # mesajdan itibaren yansir; gecmis konusma icerigi degismez.
    return jsonify({'ok': True, 'system_prompt': new_prompt, 'temperatures': new_temps})

# ============================================
# API ANAHTARLARI (arayuzden yonetilebilir, 6 slota kadar)
# ============================================
@app.route('/settings/api-keys', methods=['GET'])
def get_api_keys():
    with api_keys_lock:
        slots = list(API_KEY_SLOTS)
    return jsonify({'keys': slots, 'slot_count': API_KEY_SLOT_COUNT})

@app.route('/settings/api-keys/<int:slot>', methods=['POST'])
def update_api_key(slot):
    if slot < 0 or slot >= API_KEY_SLOT_COUNT:
        return jsonify({'ok': False, 'error': f'Gecersiz slot. 0-{API_KEY_SLOT_COUNT - 1} arasi olmali.'}), 400

    raw = request.get_json(silent=True)
    # ONEMLI: JSON govdesi hic parse edilemediyse (bozuk istek) ya da 'key' alani
    # gonderilmediyse, bunu "anahtari bosalt" olarak YORUMLAMIYORUZ - aksi halde
    # gecersiz/bozuk bir istek yanlislikla gecerli bir anahtari silebilirdi.
    # Anahtari bilerek bosaltmak icin govde acikca {"key": ""} olmalidir.
    if not isinstance(raw, dict) or 'key' not in raw:
        return jsonify({'ok': False, 'error': "Gecersiz istek: govde JSON olmali ve 'key' alani icermeli."}), 400

    new_key = raw.get('key')
    if not isinstance(new_key, str):
        return jsonify({'ok': False, 'error': "'key' alani metin (string) olmali."}), 400
    new_key = new_key.strip()

    with api_keys_lock:
        API_KEY_SLOTS[slot] = new_key
        save_api_key_slots(API_KEY_SLOTS)
        # Aktif (bos olmayan) anahtar listesini YERINDE guncelle; boylece key
        # rotasyonunu kullanan tum fonksiyonlar (get_available_keys vb.) hicbir
        # ek islem gerekmeden yeni listeyi otomatik gorur.
        GROQ_API_KEYS[:] = [k for k in API_KEY_SLOTS if k]
        for k in GROQ_API_KEYS:
            key_cooldowns.setdefault(k, {})
        current_slots = list(API_KEY_SLOTS)
        active_count = len(GROQ_API_KEYS)

    return jsonify({'ok': True, 'keys': current_slots, 'active_count': active_count})

@app.route('/settings/search-key', methods=['GET'])
def get_search_key():
    return jsonify({'key': get_tavily_key()})

@app.route('/settings/search-key', methods=['POST'])
def update_search_key():
    raw = request.get_json(silent=True)
    # Ayni guvenlik ilkesi: govde JSON degilse ya da 'key' alani yoksa, bunu
    # "anahtari bosalt" olarak YORUMLAMIYORUZ - boylece bozuk bir istek yanlislikla
    # gecerli bir anahtari silemez. Bilerek bosaltmak icin govde acikca {"key": ""} olmali.
    if not isinstance(raw, dict) or 'key' not in raw:
        return jsonify({'ok': False, 'error': "Gecersiz istek: govde JSON olmali ve 'key' alani icermeli."}), 400

    new_key = raw.get('key')
    if not isinstance(new_key, str):
        return jsonify({'ok': False, 'error': "'key' alani metin (string) olmali."}), 400

    set_tavily_key(new_key)
    return jsonify({'ok': True, 'key': get_tavily_key()})

@app.route('/settings/search-enabled', methods=['GET'])
def get_search_enabled_route():
    return jsonify({'enabled': get_search_enabled()})

@app.route('/settings/serper-key', methods=['GET'])
def get_serper_key_route():
    k = get_serper_key()
    # 'key': Tavily alaniyla ayni desende, arayuzdeki girdi kutusunu dolu gostermek icin
    # anahtarin tamamini doner. 'configured'/'key_masked' geriye donuk uyumluluk icin kalir.
    return jsonify({'key': k, 'configured': bool(k), 'key_masked': _mask_key(k)})

@app.route('/settings/serper-key', methods=['POST'])
def update_serper_key_route():
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict) or 'key' not in raw:
        return jsonify({'ok': False, 'error': "Gecersiz istek: govde JSON olmali ve 'key' alani icermeli."}), 400
    new_key = raw.get('key')
    if not isinstance(new_key, str):
        return jsonify({'ok': False, 'error': "'key' alani metin (string) olmali."}), 400
    set_serper_key(new_key)
    k = get_serper_key()
    return jsonify({'ok': True, 'key': k, 'configured': bool(k), 'key_masked': _mask_key(k)})

@app.route('/settings/search-test', methods=['GET'])
def search_test_route():
    """Teshis: her arama saglayicisini canli olarak dener ve durum kodunu/hatayi gosterir.
    Ornek: /settings/search-test?q=bugun%20dolar%20kuru"""
    q = (request.args.get('q') or 'bugün İstanbul hava durumu').strip()[:200]
    out = {'search_enabled': get_search_enabled(), 'query': q,
           'serper': {'configured': bool(get_serper_key())},
           'tavily': {'configured': bool(get_tavily_key())}}
    if out['serper']['configured']:
        items, extras, err = _serper_fetch(q)
        out['serper'].update({'ok': bool(items) and not err, 'results': len(items), 'error': err,
                              'first_title': items[0]['title'] if items else None,
                              'answer_box': bool(extras)})
    if out['tavily']['configured']:
        items, err = _tavily_fetch(q, derin=False)
        out['tavily'].update({'ok': bool(items) and not err, 'results': len(items), 'error': err,
                              'first_title': items[0]['title'] if items else None})
    return jsonify(out)

@app.route('/settings/search-enabled', methods=['POST'])
def update_search_enabled_route():
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict) or 'enabled' not in raw:
        return jsonify({'ok': False, 'error': "Gecersiz istek: govde JSON olmali ve 'enabled' alani icermeli."}), 400
    enabled = raw.get('enabled')
    if not isinstance(enabled, bool):
        return jsonify({'ok': False, 'error': "'enabled' alani true/false olmali."}), 400
    set_search_enabled(enabled)
    return jsonify({'ok': True, 'enabled': get_search_enabled()})


# ============================================
# PWA (Ana Ekrana Ekle) - ikonlar ve manifest
# ============================================
# Ikonlar uygulamanin kendi muhur logosuyla (obsidian + brass 5 kollu yildiz)
# birebir uyumlu olarak uretildi ve tek-dosya mimarisini bozmamak icin
# base64 olarak dogrudan koda gomuldu (ayri statik dosya/klasor gerekmiyor).
ICON_192_B64 = "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAA9jUlEQVR4nO29eZhdZZ3v+3nftfa8d81zakxSiRnJPDAYMNqgDeKhtR1AbejTehpFpNVz2+7bHtTH597utp379Lk2gogoitpMIqgEIsqQAcg8p1KVmpOaa89rve/9Y+1VqQQCGXZV7V2p7/ME8lR27b32Wr/f+/v+ZsHkQWwAYzNowB73c2N2beUCpNmgbWsdQswXQjQqrQyBuAwwJvEaZzAh0PuBUa3FgBC8IoV+VSjdOmKJXT09PdFxL3RlROH8mXCISfgMmfljuT+YO6ukVknPO0FcCazRmmYhhF8IQDsaAqC1foO3m0G+QQgx7u+gNWjn4R4TsA0t/mib+netrd37xv2ae/CNPyyzf20T+N4y8/42QFNFRaXwiveAeJ+Ga6SUEXCEPCPomlNaL8a9xwzyH5px5xrO85VCCIQQjpAopYCXBfq/bFs+3trVtT/zeoEjBxOiCBOhAKdd8OzaysVa8HGhxUeFlJWgUUqT+XfNKUWZDGs0g9yCImMQBJiuQiilkgLxcy14oOV41+8yr3VlJKvUKNtCZ5KhOo7gi79Dc7OU0qu1QmnszAe6Qj+DGbjQgHKVQUoJDjv4nRbiX8cpgkFGcbLxodkSQvd99OzKygpM8X9rwSellF6lFBos4Vz4jNDP4FygNSgBQkoh0aC0+o2A/3m0vWd35jWSLFiDbAikgcvzaytvA/FVKWVNRvBtMXPaz+DiYJNRBKV0CiH+RYwmvn50YGCIcYzjQnGxgmkCVl1daY2J515DyGuV1iitZwR/BtmGDRiGlCilDmKL2452df2Ji6REFyqgYw5JY231tRJ9n5CiWiltMUN1ZjBx0BpsKYSptbaEFl842tH1rcy/XRAluhBBdUOTanZt1f9CiLudK9M2M0mrGUwOFCClYw1+nsb82/b29n7G0fFzxfkqwJiWNdVWfd8w5N/YtrI5FfqcwQwmC1qDbUppKm1vtYTv2ra2tgHOUwnORwEkoOrr64sNlfo/Usq/tJVKC8cPmKE8M5gSaEgbUni00q/ayvrr1s6Tr3IeSnCugisB6uvrC02dfFoKY7WllJUR/hnMYEqhwTakMLTSAwj+7Ojx7m2coxKcC21xlUSZKvmfGeFPzQj/DHIFAgxbaUsIUaw1P6+vry/GEf63lO9zUQAJqKbaqu8LKf/Cdk5+78Ve9AxmkE0IMJXWthSiydTJp2tqako5VXd0VrypAmxwTnm7aVbl3YYh/8ZWOs3MyT+D3IWhtLakMFZ7pXqIU7VmZ8WbaYcB2E3VFe8SpvFbrWdi/NmAeIu7N1MBfvFwHGPpUbb95ZaOnrs3gLn5LBnjsz0Oh/Y0VVSKtNyBoELrt9amSx0CQAiEcP/u1r47gq+UxrbV2bVAa6SUGIYY+52xIqvMfzR6RknODbYQwtCW/WctXb2/4yxO8dkUwAB046yqpwxDvEupmSTXmRA4jR6uLCsNtq2wbIVtK2zlCKphCAwpUVrj9RgUhn1v2OijASkEsXiaaDyNlAKlNZalEAKkFBhSYJoGRubv7u85PRWT9tXzBUoIBJpe27CXtbae6HV/Pv5Fb8TnDcBurKn6uGHId9kz4c4xyIzAa8CyFemUhWUrtAafxyAc8lFeHKAw4qeuuoCAz0N5cZCqshCptCIU9FBTEUEp/Toj4CpL32CckwMxvB6DkWiK1s5BtIaW9kGGo0l6+qKMRlNE42m01piGxGMamKZEihmFGAepNJYhZaW09P8L/BVvcIifaQEEIOrr6wsNldoroCJzHy9J6iMAIZ2OJUtpkikLy1IYUlAY8TOrMkJdVQGNs4porCmkuiJCUcRHOOjF6zHwegyn+Uc41sK2FfGENaZEZ36W1uD1GPi8hts2mGkQ0SRSFvGkxeBwgt7+KK2dQxxrH6Sta4j2nhH6BmOkLYUhJT6voxAwoww4VEhouLrleNfznEGFzlQA5/SfVfU9w5CfUkpdktRHZoQ+bSkSKQutIBzy0FRbzLzGEt7WVMac+mKqy8JEQl48HgOtNYmkTSyeYjiaom8wxuBwgnjCoqVjEI8pGYmmONY5hCHFGyqAZSsqS0NUlYVJpCyqy8KUlQTxeUyqy8OEAh4Kwj5HwA2JZSui8TQn+mO0tA+yv+UkB4/1cbhtgP6huGOZvI4iAmils9NFkl9QUgiplHrFCEQuP3z4cJpxLZrjFUACurGmZp6QajeXWKuiEA7FsZUmnrRQSlFSGGDhnHJWL6lhXmMJTbOKiIR8mIYkmbYZHE7Q3jNMW9cQHT0jdPSM0NMX5cRAlHjCIhpPA2BZauwzDEOevXA34yhnWkYxDIEUAq/HIBT0Ulzgp6IkSG1VIdXlIRpriqitKqC0KEDAb6KVJpawON49zOG2fl7Z282O/T10nRhFaU3Ab+IxpNN2dQmZBSdTLA3bUn91rLP7fsZZgfHC7YQ9ayvvl1J+zFbaFpfA6S8zzmQqbZNM2gQDJkvmVXDlynqWzqugrqqAoN+DrTR9g3Hae4Y53NrPvqMnOdzWT2fvKLFEGtvWjnNqCEzDcJxWw3nv8Tf5XMTuzMiP0ho7E0GybJXxIQQ+r0FFSYjZdcUsmF3KvMZS6qsLqSgJ4vUapFI23Sej7D1yghdea2frrk4GhhN4TInPa45Fpi4BKCGE0FodDBSULt27d28683M9fvqCbmioepu0eRXwMM1PfymdUGM8kUZpTXVZmHeub+Ly5XU01RZREPaRTtt0nRjl8PEBdh3sYfueLjp7R52TXWs8GZ7vUqaJDFWKTFzVDY26zm46rUilbZTW+H0m5cVBlsyvYOXCaubWl1BfXUDA7yGRtGjrGuLVfd385g9HOHJ8ANtWBAMeDOlYvmkOW0ppWNq+tfV4zw/JHPgCnIzvZrBmz6r8N2EYfzedIz8yEyqJxlMYhmTBnDI2rmviiuW1VJSGnUjMQJyDx/rYvLWVbXu66O2LYtlqzLF1Q5C5QCXEuLyDyihEMm0jBBQX+Fk4t5yNaxtZNLeC6vIwUgr6h+Ps2NfDU386wo59PQxHk4SD3umuCHbGF9je0tGzmkyuyz3ldVVVVXnAZLeA8swtmFanv8zEHZ0YO6xYWM37Ns5nxcIqCsI+kimb1s4hXnjtOH/Y2saR4wOkLRuf18RrGg5dyAGBfysIcWoQlZVx4gWC6oowVyyv5apVDcxvLCES8hFPpDnQ0sfjzx1i89ZWRmMpwkGvk4OYnoqghBBYSr2rraNnE2CIDRswN2/GaqipvNU0jXttpaYV93ed21jCQmvNykWO4K9cVE046GU0lmL7ni6e3dLKtt2d9A/FM6FIEymcUzXHZf6scMO44Pg4iaRFMOBhSXMFG1Y3cMXyWspLQqTSNvuPnhxThFg8TTjk1DtOJ0XQYBlSmkrZD7S093xswwZMwSnn99dSyncrpRXTRAEMKUilbWIJi6XzK7j5+sUsX1hNZJzgP/LMAV7Z241lq2kdJTkzymXbinmNpbz3mnlctaqe8uIgybTNwWN9PPzUPja93IIhJUG/mdeHwBnQAoTS9PjChc0HDhwYFQANDQ1V0k4eEhDW51BCmuuQwomzj0ST1FRE+MifL2bD6nrKioOMRMcLfhdKQSjgGYuITI/n/OZwnfZ40iKVtl+nCNF4iu17unnwiV3sOtBLMOjBY8jp4h8oKYS0lP7z1o7uJwVA46yqj5qG/JE1DeiPYQjiCQut4YZrmvngdQupqYxg25qXdnTwX7/ff5rgu7H3SxFCCKR4vSK8c30TkZCXgeEET//xCA88tovhaJJI0Jv31kCDZUppWrb6wbGO7v/uKsA9piH/Op/bHF3HbziaZHZtEX/zgRWsv2wWpiFp7Rzip0/u4XcvHMWyFeHg9OO3F4MzFWHV4ho++t4lXDa/EikFB4/1cc8vXuWlHR2EAh4MQ+bzvVNCCqlstTdYWLpcrFy50tPX3f6aNORC7fD/vKv7MaQgmbaxbc0N1zRzyw1LqCwNMRpL8cRzh/jpk3s4ORAjEvJdSsmf84YQAilhNJrC6zF599vncPP1S6gqy9zLzYd44NFdjMZShIIebDtf76NAo5PaYLmora1dbGJtEwhfluaNTioMQzASTVFREuIzt6zh8uWzEFLy2r5uHnhsF9t3d+L3e/B6ZB4/sMmFGwYdiaVomlXEzdcv5pq1jfg8Boda+/nOj7fw6r4eCiO+saK9PIOSUkit9e2isa76RgmPZBYW5I3zO0Z5RpOsX1bLHTevpq66gHgizaObDnLfr14jlVaEg568561TBcOQJJIWyZTF9Vc3c9tNyygrDjI8muT+R3fyq9/ux+s18Jp55yDbUgpDKf0dMbu26ktCii/nU9OLIQUpS5FO29xywxI++J5FhAIejncN890Ht/LSjvZLIbM5KXB7IIajKRpqCrnzo2tYsbAKpTSbXj7G9x7cxkgmk2zZk7LVKBtQwskKPy8aaysfNqR8f74ogGEIovE0BSEfn/rIKjauawLghdfa+c4DW+jtixIJefMipOnW9+QDhXCja4YU3HrTMt63cT5Bv4ddh3r55v0vc/BYH4URX77QTKfhVOsu0Tir8lUp5TKtc98BNgxBNJampjLCF25bz4oFVYzEUvz013v4yRO7MQyBz2PkxakvBFi2RiuFx2PmhRKM+QbRFBvXNfK3H15FTXmYzt4Rvn7fS2zd1Ukk5M2L++9CNM6q3C6lXJHrCmCakqGRJAvnlHH3pzdQURKkty/Gfzy0jd+/2EJhxA/kh0MmBCRTNg01xRQXBXll93ECfm9+XDsgDcngcIL5TaXc9fG1LJhdRipt890Ht/LopoOUFPjzhQ5pKYS4LHPjc1b4DUPSPxhn4ZwyvuwKf3+Mu//3Zn7/YgslhYHxy/ZyHlJKYvEkG9Y18/H3ryWeSI/1JeQ6NE7zf3GBn6PHB/iHb25i18FefF6DO25ezfveMY+TA7Ex3yHHISQ5zvulFAyPJtm4vom7P7WBqrIQOw/08rl/+R0HjvZRUhjIl9NmDFprTNNg7fIGVi+tp6q8gHSmhDlfYNlOk388afEP33qWX28+jM9rcPuHV/Hf37/c6arTbz0HaaqRs6c+OCf/SDTF9Vc38w+fuJKK0iCv7u/hS9/bTGfPCKGgJ++EXwhIpSzqa4pZNK+akqIgi+ZVEU+mT9unmw+wbWfUi20rvn7fizy66SB+n8mtN13GHbesJpkpxc5l5KwCmIZkYCjO1WsauPOWNRhSsOtgL1/67mZi8XTeZiKllMSSadYtb6K8JAwa1i5vxLZV3ikAOFl105AE/R6+/aMtPPbsQWxb895r5vGJD6xgYDiBaeSsmOWmAhiGYGg0yZJ5Fdz+oVV4PAZ7j5zgnzLC7/caeSn84NAfQ0iuXt8MQDJt8/Y1cykI+52pcXkIpZ05RwG/ybd/tIUnNh8C4MZ3zOe/vXM+/cOJsS66XEPOKYAb6nQd3qryMDsP9Jwu/HkUZhsPl/40zCpmxeJaEsk0qZTF3IZyFs+rJpZHzvCZcEc5ukrw2LMOHbrjltXc+I55DEedFtRcQ05dkSEF0ZhFTUWEuz+9gfKSIK/t6+ZL38t/4QenfCOesljYXE15cZh0WqG1JuAz2bCumXTayksa5GK8EnzrR1t4bNMBDENy5y1ruHpNAwND8ZyjQzlzNUJAKq0oCHv5wm3rx0Kd/3rvi4yMpvD78lv4IVO/pDRXrpmdEXRnGG4iZXHF6tkURgJ5S4NcjCmBz+R7P9nKjv09eDPRoSXzKhiNpXKKDuWMAoAgbdl86iOrWLGwit6+GHf/+2Y6e0cIBc285fwuhIB02qaiNMK65U3Ek2mklAgBiWSa5oYyls6vyaucwNmgtWPNpZR89T+eZ8f+HqrLwnz+1vUURvyk0ipnwqM5oQCG4cT6b7lhCRvXNTESTfEfD21j14FewkFv3gs/OKd/LJFixeI66quLSKWsU5OllSbg97J6WQOpPKdBLpTWeE3J8GiSr9/3Ih29I8yuK+bzt67LhK5z4ztOuQIYUjAymmL9slo+9J5FADz05J6xDG++xfnPBiEEWmmuvrwZQ8rTstZCCFJpi1WX1RPwe6dNw46tNOGgl7auYb55/0vEE2nWL6vl5usXMzyazAkqNKUKIIQTBqwoDXHHzasJBjy88Fo7Dz6xm8JI3tSTvCXG05+1yxqJZeiPCykF8USapfNraG4oI5FM5wxFuFhYtuPXvfhaBw89uQeAD71nEeuX1TISnXp/YGoVAGdk+GduWUNddQHHu4b5zgNbMAxns8F0wZvRHxe2rSiMBLhyzRwSKes0Bcl32LamIOzjwSd288Jr7QQDHu64eTUVpSGSU+wPTNlddpNdN1w9j/XLZhFPpPnug1vp7Yvi8xhMExYAvDn9Gf+adNri7WvnEvCa04YGuRBoDEPwnQe20N49TENNIbfdtMwpl5hCDZgSBXBMvs3suiJuvmEJhpQ8uukgL+1oz7t68rfCW9EfF1IKYok0i+dVM6+pYlrRIHDGSvo8Bj0no9zzy9eIJdJsXNfIxnWNDI+mxiZpTzamRAGcRmrF33xgBVVlIV7d1819v3qNcGbuzHTCudAfFy4NWru8kcRZFCWfYSuHCj3zYguPPXsQr9fk1puWUVUWIpmaGio06XfYkIKRaJIbrpnH+stmMRpL8cBjO0mlndVD00z+z4n+jH+tbStWLKnDYxp5099wPlBaEQp4eOjJPbR1DlFf7VChqQr/TqoCuNnemoowH7xuIYYhefy5Q2zf20046JlW1AdO0Z/yt6A/p17vWItVS+qoewtrka/QGjympG8wzg//aweJpMU1axtYd1kt0djkJwEnVQGcKc1pPvLnS6ipLKC1c4ifPbkHv9ecdtQHzl+gxyvMuuVNb6kw+QpbaSJBL5tebuHlHR34fR4+duNSfL5TSwUnC5N2dx0nz5nS/PZV9di24qe/3sOJgTher5x21AdOUZqVS+vPmdK4lGnD+remTHkNAYZhcP+jOxgYijO/sZR3XzmXaDyNMYlmb/KOl8zqoJuvX0xZSZCXdnTw+xePOlGfaVDq8EZwndo1yxrO2ak95TTXvqXTnM9QShPwOZPmHnnmAKYp+cC1C6goCZKyJs8hnhQFkNKZ5bNyYTXLF1YzGk3xX7/fj2W/fmH0dMGFhjVPK5pbcapobjpCaU3A5+GJzYc43jXErKoCrrtqLrF4emyjz0Rjcu6sdtrv37dxPpGgl+17uti+t4twwDPtEj4u3MTWhnXN553YGiubXj1nrGx6OsJZDC7pORnl6T8dBeDaK2ZTVRaaNCsw4QogpWA0c/qvXFTNaCzFI88cQOdOQeCEwKU/V6yefd6lDacaZ6ooyzTOTFdLqbQmGPDw1POHOd41RF114aRagYm3ABpMQ/C+jfMJjzv9Q9P49B9Pf5obys87qzu+dXLl4jpiidS0KJF+I2gNXlPSfYYVqKkIT4oVmFAFcAVhwZwyViysumRO/9Poj+/C6nrGmucvb0ZnFmNPV5xpBeprCnn7qgZno+cEf+8JVQB3d+3GdU0UhP1suwROf7g4+uPCHZ+ydlkjFaWRvBucdT440wpIIdiwup6iiM9Jjk7g954wBXBr/avLnf20yZTFc1tasa3pG/mBi6c/LlwaVFddxIppToPAOSj9XpM/bm+jpy/KgjnlXPa2ygm3AhOmAFIIEkmbjetmU1EaprVzkG27OwkEPNMy6+siG/THhdbO0KkN6+dOexqkNfh8Bi0dg7y2vxufx+C6K+fgMSc2STphCmArTShgcsXyWgxD8MKrHfQNxfEY06/gbTyyQX9cSClJpiyWzK/J68FZ5wUtePblVhJJi6XzKmmoKSSZsp0pthOACXlbp97fYsm8Cppqi+gbiPGHba34PAa5v7biwpEt+uPCnRgxt6GMRfOqp8XEiDeD0pqA3+S1/d0cau2npNDP5cvrSKUt5AQ5AhOiAAKwleLKFfUUhH0cPNbPkeMD+LwmahofYkIILMvm6vXN+C+S/rhwJ0ZsWNc8bSZGnBWZkPnQSJLte7uQUnL5slpnMsgEBU2yrgBCQNpWlBYFWDqvgrRl89zWVtJpxTQ+vABQShHweVi+qJa0ZWdFWKWUJJLpaTM4662g0fi8Bs9va6NvMEbjrEKaG0pIJO0JsX4ToACCRMJm4dxy6qoL6Ood5ZU9Xfh807Pk2YVDVyzmNJQxf3ZF1ujKqcFZ+T8/9FygFPi8JkeOD7DvyEkKI35WLa4hbdsTQoKyrwA4XT+rF9cQ9Hs40jZAT38U7wR781MNZ8RhmqvWzKGoIJjVk9qpnHTnh2bHsuQyZGaF1K7DJ9AaViyoIjJBNCjrCmArTSTkZV5DKbbS7DjUizWJ5a1TBUdIPbx97dysD7kdmx+6ajaFkekfDdKAxzTYfbCX4WiC2qoIsyoipNI22S6MzerbSSlIpGyaaotprC2kbyjOK3u68F5k9EcIh1o5/8+9P1IKkilrwmiKEJDM1AY1zCp2oiIyd++H+6wuFEpp/D6Dg8f6aDk+SGlRkKXzK0ml7KxvnDGz+WYCsNI28xpLKAj52HGgh87eEbwe46KiP8mU4wDlqhExDMloNMEVq+dQGAnQPxjN+ix821aUFIW4YtUcXt3Tjtdj5qwl0DhC7PVc+Po5QwqGokkOtvazYmE185tKkROQQ8qqAmgN0hC8rakM05Acbu1nNJ6m8CJm/QghqKspIhZLoZTOSQdQCIHf5+Gay5szHVzZv0YhBKmUxTWXN/Orp3Y4VjUHnSr3GQWDXnpPjl7UNQot2N/SRzJtM7e+mMKIj2RKYUiypghZUwAhwFKaooiPufXFJNM2+1r6EBdxoUKAZdtctXoOd3x8A0ppYokc3DSiNdKQhIO+TAfXBITrpCCeTLOouZrHfvAJlK3INcfKthVBvxcpBd+9fzM/e+IVTOPCgh8K8HgMDrf2Mzgcp6o8TF1VIfuOnMT0Z2+xeBYVwKmBmVNXRFV5mMGRBIdb+/F4DC7UUDtz5iX3/2ILbR0D/OOnr6W5qZyBoRhSCqTIrQmik0FJtNYUhP0T/jnnCrfiVylNcWGQQy0n+Nr3nubZFw8RCnov+KTWSuP1SDp7R+joGWHp/EqaG0rYub+HoD97xCV7CgBYlqK2qoBI0Muug70Z/i/RFxm+Koj4eOaFA+zY18GX7ryOG9+1lKGROCnbdqyB1kxtg4Hz+ZN1IFuWGvvMqYOzCsa2FaYhKYwE+K+nd/CVbz/FyYFRigoCFz3sYMwPONbPykU1NFQXku1sanZ9ADRNs4rwmgatncPEEmkiAS/2RZor29YUFQSJxlPc9dVfsX3XcT7711cTCfkYGU1imlNNiSZXEB1Fm2r6I7AsRSTsYzSW5Evf+DU/fmQrXo+Z3TyIFrR0DGJZNg01hQR8FxdQORNZkxytwes1aKgpQmno6B12ToAsPSfbVnhMg3DQx70/f4mP3fUA+w73UFYSQimdkw7hdIXOUJ6ykhD7Dvfwsbse4N6fv0Q46MNjGlkTfo1jBTp6R4inLKorwpQUBEjb2csrZUUBHAdYEQl6qakIk0hZtPeMOLM+s/EBGbzuxv9d5saHsnvjZ3B2jB1EocxB9HcTdxBprTFNyYm+KEMjCYoL/FSUhbCyVGcFWVMAxxyWFQcpiviIxlP09mU/Fu7CshThoA/Lsvmnf/s1n/3KLxmNpSgsmD4rlXIRlq0oLAgwGkvx2a/8kn/6t19jWXbmWUzMfTcNycBIgv6hBH6fh5ryCLats0YAsyahDk/3Ew55GYmmONkfxTQnbrSfrRRSCsqKQzz++9185M772fzyYUqLQmjNtO45nmw4JzuUFoXY/PJhPnLn/Tz++92UFYeQUmBPUI27u21yNJbiRF8Mn8egrDiQ1aLK7FgAnBqguqoCvKZJ32CcWNKa8I5+rZ1TqaQwSFtHP5/4+5/yzR88S8DvIeD3zFiDLMCy1dj9/OYPnuUTf/9T2jr6KSkMYtlqUgocldL0DkTRWlNXVZg5WLPz3lmzAG7FotcjGRxOEI2nJm3e//iH9I17NjkPqXNgUh/SdMNph0vnAJ/4+5/yjXs2TfrhIgRYluZEv5P7Cfo9ZHNSXlYUQAOmKSgvdhyheCLNZIfpXmemP/PDM8z0jBacK+xMOcMYvfzMD6eUXkopSCQtUmlFUYGfoN8ZrJANgpEdC6CdpXeVZWGEcOO2U1MCfZqj9tWMo2bbhIPeCXPUphOcAIMXy84EGL46tQEGNxTa0jFIImVRXhwgHMheb0BWKVAqM7zJM8WJqZmcwfljsmL7FwrTlEgpSFsqq4qYHQqkndLXcMDhhiOxqR/iNJMzOHdMZmz/QiClIBZPk0xZ+H0mwSxOFrxoBXCrQAvDPmZVRoglLY51DmU9CXahmMkZvDmmIrZ/PnCTYd0nRhgYSlBc6KeqPJKh2FkYOpCFawScqIHSToJispYbnCtmcgavx1TF9i8UbpeZs2I3x6JA+YCZnMEp5EJsP1eQ1WrQfID78LXWfOOeTby2p51/vONamhvL6R+KYUiZa30mWYPWjjUsKQxy6NgJvvbdp9n04kGKCgJOOcsldAi4uGQswHhcijmDXIvt5wouSQVwcankDHIttp9LyKoCuGdIPlGI6ZwzyPXY/vlgvEhl84lkTQGEEJiZ2h8rz/b+TsecQa7H9s8XttIorZFCYGSxLTIrCmBkxoKfHIzj8xpUlobGQqL5hOmSM8j12P75QAiBbWtKigJOC2wsxcBQPGu9Jhf9Llo7cf9oPMXJ/jg+j0FVaShvnao3yxnkC/Iltn+usCxFSVGQSMjLyGiSvsE4piGyYsWyRoGkFM4ECA2JlJWtt50SnC1nkA++jRBMu9i+OxrSthWmKfEYMmt+QFaL4UZiKbTWVJeFMabBKiTLVs5sG+D3z+9HytyecK21M0j398/vRwOhoDevqNvZoDIy5TENorE0ySxOyM5eU7ylae0cRAiHPogcHGF4IdAa0mmbG69dSjjoQ+UwjVDK8WFuvHYp6bSd08p6rnC7DcuKAwR8Jp29owyNJscCLheL7IZBMwkVn9fAa+bm7MrzhW0rCsJ+1q1oIpFMX9TSu4mGu01m3YqmabVUz5CCgN+pAM1WI4yLrHWEGVJytGOQeMqipiJMJOjBUvm9EzjbS+8mGkJMv20yDq2DuqpChBAc6xggnc6xuUDgdISNjCZJJC2Cfs+40Xj5+wBO3/mbH9vt3UUdzjaZ/F+qp7Qm6PdQHPGRthSDI8nsLh/Jxpu4Ndu9mQFGBWEfFaWhvN8MY9uKooIgV66eTSKV2/THhbuq6crVs7O+qmmy4UwH1xSEHHlKpCyOdw9ntdckSwoAphSMxFL0nIzi85rUVkawlcrb89/ZdZxm/uwK5jSUkUhaeaHMDg3K/rK+qYC7draiLExBxMfwaIITfdmdN5XVPEAskaa1awjTEFSXR/L2xkOG/lg2KxbXZehP/pyk7rrWFYvrsraudSogcAauVZWFCPo99JyMcmIghicX5wIBoOBYxyCWrWmsKcTnze4k38mEs6fKZMO6uVmdRTkZcE/ODevmZm1h91RBZSaO+zwmbV3OxHEj13wAcGcDSVq7honGU9RWF1BREnL2u+aP7AD5H03Jt+jV2aAUBHwmzfUl2ErR2jmU1YnjkNWeYI3HY9DRPcyJ/hilRQFm1xU7m/3y7O67a0mvXDMnb7ez27aiMBLgyjVzSKSsvHDgx0MISNs2FSUh6mcVMhpLceT4gLNyKYufk9WmeI8hOTkYo6V9kIDXw4LZpXlZFeqOeZyInb+TBTeE+/a1cwnkIQ1ylgLazK4rprQowIn+GC3tA3i92U2wZvVYEALSacX+lpNorZnXWJrhoNn8lIlFvtMfF/lOg9zdYwtmlxLwemhpH6R/KO4UwmVRl7PeEWYakgPH+ogm0tRXF1BeHMwrP2Ay6Y9SGttWEzY/KZ9pkNLg8xo0N5Si0Rw41kcqixlgF1muBXLqgI60DdDePUx5SYgl8ytJJu2cmxV0NkwG/XE70CIhH0WFQaSYmKb0fKVBQjrL0RtqipjbUMzQSIKdB3vwmEbWD4usF8N5DEnfUJzDbf34PCYrF1YhJNlt5JwgTAb9sW2FYUgKCwI8/OSrfPOeTUgpJ2Q2Ub7SIIkgmbJYOr+S0sIgXSdGOdY+iC/L/N/5rGxDAApe2dtNyrKZW19CcYGftMr9soiJpj+WrYiE/Vi24ov//Bhf/OfH+eYPnuO2Lzw4ts/AtrPbr5uPNEhrZ9z+ojllmIZk16ETDI4kM11g2f2srN8NpTU+n8mO/d10nxihvrqARXPLSSRyPxyqlCbgzT79UdoR6pLCIDv3dfCxux7gJ49uo7AgQFlJiK072/jIZ+7nkd/uoiDixzBk1pTvNBrkzX0aJASkLEV1eYTF8yqIJlJs2dmBYGJmzWZdAbQGrynpOjnK3iMnCfg9XLO2ESF0TtMgl/7Ma6rIKv2xbIXPYxIMeLnnZy9y8533s+9wN6VFIWxbYVlOv0E0nuTOL/+Cf/zXx0+zFBeL8TRoXlNFztMgKZwarCuW11FdHqa9e5i9R07g909Mf8mE2EMhnCzeC6+1k0haLJ5bTnVFhFQOV4e6zSRrlzdmhf5o7dCP4oIgfYNRPvHFh/jqd57CNA2CgdNbFZ1t6wbFhUF+8ug2PnbXA+zc10FJYdBpArnIU9ulQWuXN+Z8U4/S4PcZrFlSgyElr+ztpn8okfXwp4sJuRNKawJ+D1t3ddLWNUR1RYQrltc5lYk5qgFaazymwYolddj2xY3eVplGoJKiEE88s5u/vP1ennvpEMWFwczh8PonqbUTEi0tCrHvcDc333k/9/zsRSIhHz6veVEK6YwWUaxYUudEUnK0U8+pwLVYMLuMRXPLGR5N8NyW1gkdtT8hCuBEgwQDwwle3deDFIKrVtYRCnpycu6mEJBKWdRVF7FqSR2xxIUv+LAsRTDgxTQNvvKd33DnV35J/1BszKq8lexZ9rjf//Zv+OxXfjX2+xc63UEIQSyRYtWSOuqqi0ilcrO0WwCWbbNhTSORsI/DbQMcbuuf0IK+CbOFGo1pSH7z/GH6h+LMbyplSXMF8aSVc5lVKSWxZJp1y5soL42QTp9/4k6dNl0uc4I/9CKR4Pmf4OMtyOPP7HIsyMuHKC0KndWCvBmcDL1NeWmEdcubiOUgDXKd36qyEGuX1KCU5qk/HmE0lsrqJLgzMWF3wa3kO9LWz479PURCPjasbnDoxUR96AVCa40hJRvWN6OVPu/T3844uu580Y9/7senOLy+MA5/pg/xyS/+jG/cswnDuLCcgRACrTQb1jdjyIlbYH6hcJ3f1UtmUVddQGvnIFt2dTjN8BN4rRN7DGRa2p7605GMZ1/LvMYS4kmbXDECLv2pry5ixeLa86I/7gCtwkiA/qEYd331V2NjCCOh7ERxLFvh85qZHcjPctvnLyxn4NKgFYtrqc81GiSc0SfhoJf3XDUX0zB4fttxuk5E8Wax+eWNMKEKoLQmFPCwY18PB1r6KCsJccM180nlUIXlePpTcR70x6UppUUhnnv5EH95+708/syuCRlD6A6zLSsJsXXXG+QMzuGzXBpUkYM0yBCC0ViaDasbWDCnjN6+KM9tPYbfa07o6Q8TbQG0M9NlOJrk8ecOkUrbvH1VfU5ZgQuhP+6WGcOQfOOeTXzyiz+jbzBKccHEjiF8o5xBOjP09lx8jJykQWOnv4cbrm7G4zF4fnsbB1r6CPiMCR/uNeFHgK0d07Z5aysHjp6krDiYM1bgfOmPW8RWXBCkrXOA277wIN+4x9kz5vOakzKG8FTOIMBDj2/nA7ffyyu7j1NSFHKu700kJhdp0PjTf/7sMk4OxHjs2QN4PeakKOjE28CMFRiNpU9ZgZW5YQXOh/7YKlPEFjkleNt2HaesODSmGJMFJ2eQUcSOAf7q8w/ynw+9QDDgxec5e8QpF2nQ+NPfaxr8YVsbB4/1E/AZTMYtnZRv71gBD5u3tnKwpY/ykiDvzQErcK70x7KdmZtpy+aL//IYX/yXx0inbSJB35QOn3WpmGlIvvqdp/jEFx+ibyD6pjmHXKJBhiEYjaXYsLqBt80uo38oxmObJu/0h8naEZaxAtF4moef3kc0nuKd65tYtbiG0djUdFydC/0Zcz6LQ2zffZwP3H4vDz2+ncJI4Jydz4mG64wXFwZ57qVDfPCO+94065wrNEgISKUV5cVBPvSeRUgpePqPRzncNjBppz9M4pI8W2kiQS+bXm5h+55uIiEfH71hCV6vnJIKRSkl8cwg2TeiP6d26Xqd8OMXHqStY4DigmDOrRhycwaFkQB9A5m6o+8+hWm+PmdwGg1a0UR8imiQG/f/4LsX0VhTSGfvCD97as+kRH5Ou45J+yQA4QzRffCJXQwMx7nsbZW8+6q5jERTGMZkH0MO5bly9RwYR39OW47ROcAn/v6nTgJqgppWsgk3IRcMePnPn77ArZ9zcgZnKq0QApTmytVzMt97cpVZSkE0YbG4uYLrrpqDrTQPP7WPkwPxsSUrk3Ytk/dRjjkO+k12Hujl6T8eQUrBzdcvoam2iMQktk26zftlxWEWNlcRTzm+yOvWI43bpXshJQhTgbHeg6Kz0zYhBPGUxcLmKsqKw1mdtnwu0Ao8puATH1hOYcTPzgO9PPmHQ4QCHuxJtqyTbvuU1oQDXh54bBcHj/VRVRbi5usXk5xELury4JWL62iYVUwyaWEr5WRvrTfepZtDjOecYLuOe3qc455ZAqiUIpm0aJhVzMrFF1f8d74wDcnQaIJ3XzWXpfMrGRxO8P2Ht5O2nUXek90zMukK4La7DUdT3POLVxmNpblmbSPXb2hmeJKokBsJufryZqfTaFy31sc/9+O83aV7Jl4fuv3BaTkDgeDqyy+s/ulCIDOBkIVzy7nlhiVIKfjFb/ex6+AJQoGp6VabkiCw4xB7eGlHB088dxCvx+C2v1hGQ00h8cTEVouOdwJXLanHVopwyHdat1a+7tJ9I5yWvBuXMwiHfNhKsWpJ/XmVgFwoXAppGIJPfXgVFaUhXtvXw8+f2ksk5J0yejllWRClNSG/Q4UOtfZTVhzkzlvWYEgxFtqbCLj0Z+mCGhbPr6azZ4hPntmtlWe7dM8FZ+YMPvnFh+jsGWLx/GqWLqiZcBokhWQkmuK2m5axZF7FKeqTdvyuqTprpkwBtAbDdBIh3/3xFoZHk6xYVMWtNy1jJJbCmLDQnHOzb7ruMp7fcoQPfvqHPPfym3drTRecljN4+RAf/PQPeX7LEW667rKMAE6MApiGYGg0ycZ1jbxv43y01tz/yE52HzpBKDi1jfpTmgdXShMKenh1Xw/3P7ITpTTv2zifjWsbGRhOYGZpG/h4pC2LqrIIz/zpIB+960cMnEe31nTA+JzBwFCMj971I57500GqyiKkrezvd3Z4v0VDTQH/40Mr8ftMNr10jF/9bj8FIV9mjdbUQTTVVk35Y3c2zaf5h09ewbVXzKbrRJR//PazHD0+4LRRTsBNiifSBPwepBCTmnjJJbjf3b0X2Ybbi2wYkq999hqWv62Sw20DfO5ffkc8YWGaU79LOicKwjXOSMXv/WQbuw6doKYizF0fX0sk5CWZsifEKQ4FvQCXrPDDqe/u3otswnUnEimLv/3QSlYsqOLkYJyv3/ciw6PJSU94nQ25oQAaPKZkeDTJt+5/mc7eERbMLuMrd1yNxzSwLJX1JNl05vrni4m4F1IIRqJJPnPLGt591VxGYym+//Ar7D50gnDQmzPDEXJCAeBUrdCBY318/b6XSKVtls6v4G8/tJJE0sr6guQZTByMzESQ922cz/UbmjEMwUNP7uGJZw9RXJCdVtFsIWcUANz+Wh9bd3Xy3Qe3kkzZ/PmGuXz242uJJyy0ZkYJchyO8Ce5ceN8bv/wKjweySO/P8CDT+zOOeEHMKf6As6EbWsiIS+PPnMQAdz+4VW895p5AHz7R1sI+E2EICf44wxOh2EIBoaS3LhxHnfcvBrTkDy66SDffsB5bjoHZ2PmlAVwYStNSaGfX/1uPw/9Zg9CCK7f0MydH1szzhLMmIJcwqmTfx6f/sgqfF6DLbs6+NaPXs7pQysnFQDckuQADz6+m0c3HUBKwXuvmTemBErpnBuwdSlCCKfZaWAowY3vcE5+v89k58Fe/vXeF/F7TaSY+nDn2ZBzFGg8NBqfx+A7P95CImlx4zvmceM7HDr07z/ZhtICrylzJqJwqcHtJRgaTXHTu97G7R9ehWlIXnytnX+990VGoqmc31OcE4mwN4MQIHCiCv/tnfO545bVGIZkx/4evvofzzM8miQc9OacczXdIaXAshSJlMVnblnD9Vc34zEdzv+tH72M32timlPT7Xc+kGh9IMOnc/JKtXYSNiVFAR7ZdJDv/Hgrtq1ZuaiaL396A7MqIwyNJqego+zShWlI4gkLw5B8/tb13PiOeXg9huPw/mgLwUzRXa4LP6BF46zKbVLKlVprRQ77BOAO2Upx9ZoGbv/wKqrLwnT0jvDN+1/ixdc6KAj7AKZFGXMuQgDSEAyNJGmoKeRzt65n+YIqovEUD/16Dw8+sRuf18hZh/eNIBpnVf5WSvmufFAAAMOQDAzFWTKvgs/fup45dUVEExYPPbmHnzyxG8MQ+LzGlBdZTTfITJn6SDTFxvWN/I8PrqS6LEzfYJz/7+FXxpJcGp0Pwq8BoTXDYnZt1f8jpPh7pbQNGFN9ZecC05CMxlIURfx87tZ1rF9WCzgbab7zwBZ6+qIUhL2ZppYpvthpANOQRONpDENw203LeN/G+fh9JkfaBvi3+15k16ETOZnkehMoIYRUSm0Ts2urP4LgwXyxAC4MKUhZzo6tm69fzIfes4hgwEN79zD3/PI1nnmxhVDAg2cmSnTBkNJpHR0aTbJwbjmf+vAqlsyrQGvNppeP8e8/2cbwaJJQ0JtvraO2lMLQtr5XNNXWrgHrBRzhzytP0k2GDY8mWb+sljtuXk1DTQGxhMVjzx7koSf30D8YJxzygr60Kz/PB0Kc6t/1GAbvfvscbrlhCRWlIYaGE/zw0Z386rf78XqNfA1DOwqg1P8lysvLw2Gf3C2EbNCO95hXSgBOFnIkmqKiJMRtf7GMjesa8XoM2jqH+OEjO9n00jEMQzjb0vUMLXozGIYglVbE42kWz6vgEx9YztL5lUgpeG1/N9//+avsPtRLJJTXAQcNKK2NKwRAY231w4bk/fnkB5wJQwpSaZtkyuYd6xq59aZl1FcXkEjavLyznfsf2cmh1n4CPhOv15jxD86AIQW20ozGUpQXB/ngexZx3ZVzKIz4GBxJ8oun9/HwU3tJWbYzvyd/gwxaCCG0Vq2jSbXYUYBZ1XeahviWpZQlcjw7/GYQQiAEjIwmqSwLc9tfLOOaNQ34fR4GhuI88swBnth8iN6+KAG/B69HXvKKMF7ww0EvG1Y38OH3LKKhphBbaXYe6OH7D7/KroO9RELesWhQHsOWUhi24pfH2rve7yhATc18IdUuHOHPOwp0JgxDkEzZpNM26y6r5aM3LmF+UymmITneNczTfzrKU88fofvkKMGAqwh5a87PG0I4DStnCv4NVzfzttllSCno7B3h4af38eTmw6TtvD/1x6DBNqU0LK3+6tjx7vsFjvOrm2qrXpRCrFU6f2nQeLjWIBpL4/MavPuquXzgugXMqowAjFOEw3SfjOL3Gfi8jvHL8xPurJCZe5KyFPFE+nWC7/EY9A/FePqPR/nZb/ZyciBGKOCZDqe+Cy1AaBjFE5jb0tLSI1auxLN9O+nG2sr/aUjjn+08p0Fnwn140XiaipIg1101l2uvmE1ddQHgKsIR/rj9OMc6BtFAwOfBNARK579VcE97pTXxpIVlKarKwqxeUsN73j6XBbPL8HoMTgzEeH5bG49uOsDhtgH8XgOvx8jHCM+bwZZSGEqp37S097wHMFwLoGbNmtXsFfZOAb7MV857KjQebt4gFk9TVRbmuqvmcO0Vs6mvKUQKQU9flNf2d/PsllZ27OtmcCSJz+tYBSlO1STlA4QQuGM2U2mbRNLC5zVZMLuUDWsaWbukhrrqQkxD0NvvCP5jzx7k4LE+vB5j2kbLNNiGlIay1UdbOrp/vGEDpgD4ABgPg900q+on0pAfnm5WwIUb306lHUWoqQjz9lUNbFhdz8LZ5Xi9BomkxaHWfrbv7eL5bW0cOT5AKmVjmhK/zxxb2pxL1sGlewJHSZMpm1TaxpCS6oowVy6vY/WSGhbNLacg7IxEbO0c4vltbTy3tZUDLeMFP3e+V5ahhEBoRYsyfYtaW1uTOJQIcDi/3TCr8mpDymfzLSt8vnBpgWsRCiM+LptfyXVXzmHp/EpKCv1IKegbiLP36En2HOpl16ETHDzWRzSeBq3xeByK4CqExrESEy08QpxKALoCn04rUmkbpTU+r0lDTSGXza9g4ZxyFs+roLosjGEIhkeTHD4+wNPPH+HlXZ10nxhxdhBPb8EHTp3+Wqm7j7Z3fxnngLfG0xwJqKbaqmelFFfnc07gXOFaBNt2fASPKWmoKeTyZbVcvryOxlmFFEb8aK0ZHk3R0jHAoWP97Dt6ksNt/XT2jhKNp8d2jZmmxDSEM9ZRZBr49bg6c8059MWK0xr/x1dW2kpj2075h50Z3uv3mVSUhJhdV8yC2aU0N5Qyt6GY0sIApimJxdMc7x7mlb3dPLellcNt/YzGUuPCwNNb8DPQzqPQ/Qlbvq2rq6tv/M9dnGkFpr0CjIc7oDWZskilbcJBL80NJaxaXMOKBVXUVhVQWuQsmUinbQaGE3T0DHOwtZ+W9kE6e0fo6YsyOJJgNJZCKWeToxBOBSvaaeE0zbPfUoHTCmrZzhILpfRY66eUgqDfQ0HIS0VZiKrSME21RTQ3lFBfXUhpUcBpPNcwNJKg68Qouw71smVnJ3uPnKB/KIEhxRiNm44c/2zQYBlSmsq2v9zS0XM3GVmH1zu6jhWYVfm0NOSfXQpW4Ey4DqStNImkldkQ6WVWZYSl8yqY31TGnPpiqsvCFIR9eExJ2lYkEhZDIwn6hxOc6I/S2x/jRH+MRDJNS8cQpiGJJdJ0nRg962pYW2lKCwOUFAVIpW2qysKUFQcI+DzUVxdQVOCnvCREUcRH0O8Zi9JEYyl6+6O0tA9y4Fg/uw720NI+yOBIEiHA7zXxmNLJ/0+vqM65wOH+mp6kkos7OzsHyDBWOIsCZBJjO3GEP++K5LIFKQUCRzBTadexFBRE/NRVFdDcUEJDTSENNYVUl4UpLvTj95n4PE4UxYk82SSSzu8lUxZ9Q/GzTrlTShMJ+YiEnMlp3ozjrZRGSEHaskkmbYZGE/ScjNLWNUxr5yBHjg/Q0j5I/1CCtGVjGhKf13AsD6BVLg4kmRy43N+21F8d6+y+n3GnP7yBYLsRocaayq8ZpvEP0zUidL4YH1q0bEU6bZO2HKri95kUF/ipLA1RUxGhrDhIXVUBQb9JUYGfsuIglqXweU1KiwJvHE7VjsKNRFMMR5N4DMloPE3XiRG00rR0OCf68a4hTvTHODEQI5ZIZ5ZOSLweA9OQeReynWA4ZQ+2ev5YR8/VOPJuj3/BGx1FAhBz55aE7YT3VSHE7OkeFTpfCE6FHl1aYY1zTpXSY5Qj5PcQCnqwLEUw4KG6POzQkDOtgHYEuX8wTt9QHNOQpNI2w6NJEGCl1diWTdOUeEw5ZqGcCNSlw+nPERpQArCkWNPW1vUKGYYz/kVnozYSUA2zKtYZQv5Rn/rZJUmFzgWnhSfHR25sjVKnnNq0ZXO226jRmIbENJwN7kIKzMyiEFdfJivcOg1gSSlNy7buau3o/RZnUB8XbybQJmA1zKr4rGmY31RKpYHsD5G/BHDqsBevO/jPhCPXGeEeH0KdwTnDjfrYyv7lsfae928AczO84faPN30c7i821lb+wpTGX1hKpcWMEswgt2FLIQyldYstvSvb2tqGGBf1ORNvyus3OyZDKun7G6XtrYYUHt7AjMxgBjkCJYQwtNYDQvCXbW1tA5mfn9WQngunl4Cqr68vNu3UZiHFElvrmcjQDHINKkMvE1pzdUt791bOwvvH41wiOwow2traBhDqZq31gCGE+VZvPIMZTCKUEEghhFQ2t7e0d29d6VD1t5TRcw1t2oBxtL13l4ZrFfqQFMLQZ3EsZjCDyYIGWwghQYwopf76WGf3/RvA3A7pc/n98w1rGoBdW1tb4hXpp6QwVs84xjOYKmiwDCFMrfWARl/X0t6zhUz08lzf43yTWzZgtLe391vCd62t1dOmlB4cmpRXk5FmkNfQYwVu6EMarm1p79mSoT3nxUouNLE1llFrmlV5t5DyfwGoGed4BhMPG5BSSqGU+nlSyds7Ozv7OAeH941wMZldkfmjGmurr5Xo+6SU1bZSriWYKZ2YQTahncI2YSqlLaHFF452dH0r828XJPxwcUKqcayAeay96+m0SK+ytbpPCCEdpwSbGVo0g4uH1mAJIYQhpamUfkZrfWVG+N3ynAuOSGartmdMA2fXVGzEkF+TQq5VWqO0tjPtqjMWYQbnA6VBSYEphcRWqhP0P7W099yb+fcLPvXHI5vFbQLnoqyV4OmvrfwoQtwlhVyMowhagy2c18wU1c3gjeCyCoQQhhQCpVSP1vr/CIv/fbSnp5dx1DsbHzgRgjimmQsXLvTGh/tuAfHXQojLhRCZsl2ttFOq6pqwGYW4dKHI1AA61d6OOGil9mutf0Za/0dLb29P5rVZOfXHY6IEz6U8YxfbWFf9Z0LrDyN4lxRiFowpA2RuAqeUYUYhpic0pxemGU5fRWbUjFIjAp5Tml+YgfDPDh8+nMy8zq08yHpx7EQL2tjgLTIXP7ekpMAOeTYIJW5EskZrmoUQ/lM19DONHdMZbiMRgHLa1loE4hWEfiKlU79vb+/vGPdyg3GyMyHXM1Fv/AZwuf9YomIDmMdrK9+GEFegxTyFXic0jQhqOGURZpD/cHZywTBCHxBaviqkOios/YdRW+zs6emJjnutsQHE5gk68c/E/w+Z27aqgye6jwAAAABJRU5ErkJggg=="
ICON_512_B64 = "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"
ICON_APPLE_TOUCH_B64 = "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"

MANIFEST_JSON = {
    "name": "Karar ve Strateji Asistanım",
    "short_name": "Karar Asistanım",
    "description": "Kisisel AI asistan calisma alani",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#352D27",
    "theme_color": "#352D27",
    "orientation": "portrait",
    "id": "/",
    "scope": "/",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}
    ]
}

@app.route('/manifest.json')
def manifest():
    resp = jsonify(MANIFEST_JSON)
    resp.mimetype = 'application/manifest+json'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

# Service worker: Chrome/Android'in uygulamayi gercek PWA (WebAPK) olarak kurmasi icin gerekir.
# Bilerek hicbir istegi yakalamaz/onbellege almaz: sohbet akisi (SSE), API ve sayfa her zaman
# dogrudan agdan gelir, yani mevcut davranis aynen korunur. Kokten (/sw.js) servis edilir ki
# kapsami tum siteyi (/) kapsasin.
SW_JS = """// Karar Asistanim - minimal service worker (onbellek yok, istek yakalama yok)
self.addEventListener('install', function (event) { self.skipWaiting(); });
self.addEventListener('activate', function (event) { event.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', function (event) { /* ag davranisina dokunma */ });
"""

@app.route('/sw.js')
def service_worker():
    return Response(SW_JS, mimetype='application/javascript',
                    headers={'Cache-Control': 'no-cache', 'Service-Worker-Allowed': '/'})

@app.route('/icon-192.png')
def icon_192():
    return Response(base64.b64decode(ICON_192_B64), mimetype='image/png',
                     headers={'Cache-Control': 'public, max-age=604800'})

@app.route('/icon-512.png')
def icon_512():
    return Response(base64.b64decode(ICON_512_B64), mimetype='image/png',
                     headers={'Cache-Control': 'public, max-age=604800'})

@app.route('/apple-touch-icon.png')
def apple_touch_icon():
    return Response(base64.b64decode(ICON_APPLE_TOUCH_B64), mimetype='image/png',
                     headers={'Cache-Control': 'public, max-age=604800'})

# ============================================
# HTML
# ============================================
HTML = """<!DOCTYPE html>
<html lang="tr" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#352D27">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Karar Asistanım">
<meta name="mobile-web-app-capable" content="yes">
<title>Karar ve Strateji Asistanım</title>
<script>
window.onerror = function(msg, url, line, col, error){
  var d = document.createElement('div');
  d.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#ff0000;color:#fff;padding:10px;z-index:999999;font-size:12px;word-break:break-all;font-family:monospace';
  d.textContent = 'JS HATA: ' + msg + ' | satir:' + line + ':' + col;
  document.documentElement.appendChild(d);
};
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;0,9..144,600;1,9..144,500&family=Manrope:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js" crossorigin="anonymous"></script>
<style>
:root {
  --obsidian: #352D27;
  --obsidian-2: #3F362F;
  --obsidian-3: #493E36;
  --hairline: #5A4E44;
  --hairline-soft: #483E36;
  --ivory: #EDEAE2;
  --stone: #AAA196;
  --stone-dim: #A9A198;
  --brass: #C9A96A;
  --brass-bright: #E6C989;
  --brass-dim: rgba(201,169,106,0.10);
  --emerald: #3E8E7E;
  --danger: #B5564B;
  --code-rose: #C77B62;
  --code-bg: #2B2520;
  --meta-bright: #CEC7BD;
  --time-gray: #B9B1A7;
  --ink-dim: #D3CFC7;
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-xl: 20px;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--obsidian); color: var(--ivory); font-family: 'Manrope', sans-serif; font-size: 14px; line-height: 1.6; -webkit-font-smoothing: antialiased; overflow: hidden; }
::selection { background: rgba(237,230,216,0.18); color: var(--ivory); }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-thumb { background: var(--hairline); border-radius: 10px; }

.app-container { display: flex; height: 100vh; height: 100dvh; max-width: 100%; overflow: hidden; }

/* ============ SIDEBAR ============ */
.sidebar { width: 264px; min-width: 264px; background: var(--obsidian-2); border-right: 1px solid var(--hairline-soft); display: flex; flex-direction: column; height: 100%; overflow: hidden; flex-shrink: 0; }
.sidebar-header { padding: 20px 18px 16px; border-bottom: 1px solid var(--hairline-soft); display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.sidebar-header .seal { width: 28px; height: 28px; flex-shrink: 0; border-radius: 50%; background: radial-gradient(circle at 35% 30%, #4a3f36, #231d18 70%); border: 1px solid #BFA68A; display: flex; align-items: center; justify-content: center; box-shadow: 0 0 0 1px rgba(191,166,138,0.15), 0 0 14px rgba(191,166,138,0.12); font-family: 'Fraunces', serif; font-style: italic; font-weight: 500; font-size: 14px; color: #D3BC9F; line-height: 1; }
.sidebar-title { font-family: 'Fraunces', serif; font-size: 14px; font-weight: 600; letter-spacing: 0.03em; color: var(--ivory); line-height: 1.2; }
.sidebar-title .sub { display: block; font-family: 'Manrope', sans-serif; font-size: 9px; letter-spacing: 0.12em; color: var(--meta-bright); text-transform: uppercase; margin-top: 2px; font-weight: 500; }

.sidebar-list { flex: 1; overflow-y: auto; padding: 12px 10px; }
.sidebar-item { padding: 9px 10px; border-radius: var(--radius-sm); cursor: pointer; font-size: 12px; color: var(--stone); transition: background .15s ease, color .15s ease; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: flex; align-items: center; gap: 8px; border: 1px solid transparent; margin-bottom: 2px; }
.sidebar-item:hover { background: var(--obsidian-3); color: var(--ivory); }
.sidebar-item.active { background: var(--obsidian-3); border-color: var(--hairline); color: var(--ivory); }
.sidebar-item .icon { flex-shrink: 0; width: 13px; height: 13px; display: flex; align-items: center; justify-content: center; color: var(--stone); }
.sidebar-item .icon svg { width: 13px; height: 13px; }
.sidebar-item.new-chat { color: var(--ivory); border: 1px solid var(--hairline); background: rgba(237,230,216,0.04); font-weight: 600; margin-bottom: 10px; }
.sidebar-item.new-chat:hover { background: rgba(237,230,216,0.09); border-color: #6E6155; }

.sidebar-group-label { font-family: 'Manrope', sans-serif; font-size: 9.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.1em; color: var(--stone-dim); padding: 12px 10px 6px; }
.sidebar-foot { flex-shrink: 0; padding: 12px 18px 16px; border-top: 1px solid var(--hairline-soft); font-family: 'Manrope', sans-serif; font-size: 9.5px; color: var(--stone-dim); letter-spacing: 0.02em; }

/* ============ MAIN ============ */
.main-content { flex: 1; display: flex; flex-direction: column; height: 100%; overflow: hidden; background: var(--obsidian); position: relative; }

/* Ust bar: cam efekti. Bar sohbet alaninin ustune biner (absolute); mesajlar kayarken
   altindan gecer ve bulaniklasir. Alt cizgi yok. Sohbet alaninin ust bosluguna bar yuksekligi (54px) eklendi. */
.header { position: absolute; top: 0; left: 0; right: 0; display: flex; align-items: center; justify-content: space-between; padding: 0 16px 0 14px; height: 54px; border-bottom: none; background: rgba(53,45,39,0.72); -webkit-backdrop-filter: blur(16px) saturate(140%); backdrop-filter: blur(16px) saturate(140%); z-index: 10; }
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) { .header { background: var(--obsidian); } }
.header-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
.sidebar-toggle { display: none; width: 34px; height: 34px; background: none; border: none; border-radius: 50%; color: var(--stone); cursor: pointer; padding: 0; align-items: center; justify-content: center; transition: background-color .15s ease, color .15s ease; -webkit-tap-highlight-color: transparent; }
.sidebar-toggle svg { width: 18px; height: 18px; }
.sidebar-toggle:active { background: rgba(237,230,216,0.08); color: var(--ivory); }
.header-brand { display: flex; align-items: center; gap: 9px; min-width: 0; }
.header-brand .seal { width: 26px; height: 26px; flex-shrink: 0; border-radius: 50%; background: radial-gradient(circle at 35% 30%, #4a3f36, #231d18 70%); border: 1px solid #BFA68A; display: flex; align-items: center; justify-content: center; box-shadow: 0 0 0 1px rgba(191,166,138,0.15), 0 0 12px rgba(191,166,138,0.12); font-family: 'Fraunces', serif; font-style: italic; font-weight: 500; font-size: 13px; color: #D3BC9F; line-height: 1; }
.header-brand-text { min-width: 0; line-height: 1.2; }
.hdr-clock { display: flex; align-items: center; gap: 11px; min-width: 0; }
.hdr-clock-sep { width: 1px; height: 26px; flex-shrink: 0; background: linear-gradient(to bottom, transparent, rgba(191,166,138,0.45), transparent); }
.hdr-clock-body { display: flex; flex-direction: column; justify-content: center; gap: 1px; min-width: 0; overflow: hidden; line-height: 1.35; cursor: default; user-select: none; -webkit-user-select: none; }
.hdr-clock-date { font-family: 'Manrope', sans-serif; font-size: 11px; font-weight: 600; letter-spacing: 0.04em; color: #D3BC9F; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-shadow: 0 0 14px rgba(191,166,138,0.25); }
.hdr-clock-date .dot { display: inline-block; width: 2px; height: 2px; border-radius: 50%; background: #BFA68A; opacity: 0.7; margin: 0 6px; vertical-align: middle; }
.hdr-clock-date .wd { color: #A99C8C; text-shadow: none; }
.hdr-wx { display: flex; align-items: center; gap: 5px; min-width: 0; font-family: 'Manrope', sans-serif; font-size: 9.5px; font-weight: 600; letter-spacing: 0.04em; color: #A99C8C; white-space: nowrap; transition: opacity .35s ease; }
.hdr-wx.is-loading { opacity: 0.6; }
.hdr-wx svg { width: 10px; height: 10px; flex-shrink: 0; fill: none; stroke: #BFA68A; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; opacity: 0.9; }
.hdr-wx-place { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.hdr-wx-dot { width: 2px; height: 2px; border-radius: 50%; background: #BFA68A; opacity: 0.7; flex-shrink: 0; margin: 0 1px; }
.hdr-wx-temp { flex-shrink: 0; color: #D3BC9F; letter-spacing: 0.06em; font-variant-numeric: tabular-nums; }
@media (max-width: 340px) { .hdr-clock-date .wd, .hdr-clock-date .dot { display: none; } }
.header-brand-title { font-family: 'Fraunces', serif; font-size: 11px; font-weight: 600; letter-spacing: 0.03em; color: #BFA68A; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.header-brand-text .sub { display: block; font-family: 'Manrope', sans-serif; font-size: 9px; letter-spacing: 0.12em; color: var(--meta-bright); text-transform: uppercase; margin-top: 1px; font-weight: 500; }
.header-actions { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
.icon-btn { width: 34px; height: 34px; padding: 0; border: none; background: transparent; border-radius: 50%; display: flex; align-items: center; justify-content: center; cursor: pointer; color: var(--stone); transition: background-color .15s ease, color .15s ease, transform .15s ease; -webkit-tap-highlight-color: transparent; }
@media (hover: hover) { .icon-btn:hover { background: rgba(237,230,216,0.08); color: var(--ivory); } }
.icon-btn:active { background: rgba(237,230,216,0.12); color: var(--ivory); transform: scale(0.94); }
.icon-btn:disabled { opacity: .4; cursor: default; }
/* Ust bar uc nokta menusu: mesaj kutusu menulerinin kahve tonunda acilir menu */
.hdr-menu-wrap { position: relative; display: flex; }
.hdr-menu { position: absolute; top: calc(100% + 8px); right: 0; z-index: 30; width: min(264px, calc(100vw - 28px)); background: #4A4038; border: 1px solid #62564C; border-radius: 20px; padding: 6px; box-shadow: 0 14px 40px rgba(0,0,0,0.5); opacity: 0; visibility: hidden; transform: translateY(-6px) scale(0.98); transform-origin: top right; transition: opacity .16s ease, transform .16s ease, visibility 0s linear .16s; }
.hdr-menu.open { opacity: 1; visibility: visible; transform: none; transition: opacity .16s ease, transform .16s ease, visibility 0s; }
.hdr-menu-title { padding: 10px 12px 8px; font-family: 'Manrope', sans-serif; font-size: 13.5px; font-weight: 400; line-height: 1.35; color: #A0998F; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hdr-item { display: flex; align-items: center; gap: 14px; width: 100%; padding: 12px; border: none; background: none; border-radius: 14px; color: #F5F2EA; font-family: 'Manrope', sans-serif; font-size: 15px; font-weight: 500; text-align: left; cursor: pointer; transition: background-color .12s ease; -webkit-tap-highlight-color: transparent; -webkit-touch-callout: none; user-select: none; -webkit-user-select: none; }
@media (hover: hover) { .hdr-item:hover { background: rgba(237,230,216,0.07); } }
.hdr-item:active { background: rgba(237,230,216,0.10); }
.hdr-item:focus { outline: none; }
.hdr-item:focus-visible { outline: 2px solid rgba(245,242,234,0.5); outline-offset: -2px; }
.hdr-item svg { width: 20px; height: 20px; flex-shrink: 0; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
.hdr-item.hdr-item-pinned svg { fill: currentColor; stroke: none; }
.hdr-item-danger { color: #E0766B; }
.hdr-menu-sep { height: 1px; background: #62564C; margin: 6px 8px; }
.hdr-menu-sep.hidden { display: none; }
.hdr-menu-label { padding: 6px 12px 4px; font-family: 'Manrope', sans-serif; font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: #A0998F; }
.hdr-item:disabled { opacity: .4; cursor: default; }
.icon-btn:focus { outline: none; }
.icon-btn:focus-visible { outline: 2px solid rgba(245,242,234,0.5); outline-offset: 1px; }
.icon-btn svg { width: 17px; height: 17px; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }



.chat-area { flex: 1; min-height: 0; overflow-y: auto; padding: 76px 20px 22px; display: flex; flex-direction: column; gap: 22px; background: var(--obsidian); }
.chat-area.is-empty { justify-content: center; align-items: center; padding: 74px 20px 20px; }

/* ============ WELCOME / EMPTY STATE ============ */
.welcome-container { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 18px; max-width: 480px; padding: 0 8px; width: 100%; }
.pulse-orb { width: 48px; height: 48px; border-radius: 50%; background: radial-gradient(circle at 35% 30%, #50443a, #231d18 72%); border: 1px solid #BFA68A; display: flex; align-items: center; justify-content: center; box-shadow: 0 0 0 1px rgba(191,166,138,0.15), 0 0 26px rgba(191,166,138,0.14); position: relative; animation: orb-glow 2.6s infinite ease-in-out; font-family: 'Fraunces', serif; font-style: italic; font-weight: 500; font-size: 24px; color: #D3BC9F; line-height: 1; }
@keyframes orb-glow { 0%,100%{ box-shadow: 0 0 0 1px rgba(191,166,138,0.15), 0 0 26px rgba(191,166,138,0.14);} 50%{ box-shadow: 0 0 0 1px rgba(191,166,138,0.22), 0 0 34px rgba(191,166,138,0.22);} }
.welcome-title { font-family: 'Fraunces', serif; font-size: 22px; font-weight: 500; line-height: 1.3; color: var(--ivory); letter-spacing: -0.01em; }
.welcome-subtitle { font-family: 'Manrope', sans-serif; font-size: 13.5px; font-weight: 400; line-height: 1.5; color: var(--stone-dim); margin-top: 2px; letter-spacing: 0.01em; }


/* ============ MESSAGES ============ */
.msg-group { display: flex; flex-direction: column; gap: 5px; max-width: 100%; }
.msg-group.user-side { align-items: flex-end; }
.bubble { padding: 10px 14px; border-radius: var(--radius-md); line-height: 1.6; overflow-wrap: break-word; word-break: normal; font-size: 13px; }
.bubble.ai-bubble { background: transparent; border: none; padding: 0; color: var(--ivory); width: 100%; max-width: 660px; }
.bubble.ai-bubble h1, .bubble.ai-bubble h2, .bubble.ai-bubble h3, .bubble.ai-bubble h4 { font-family: 'Fraunces', serif; font-weight: 600; color: var(--ivory); margin: 16px 0 8px; padding-left: 11px; border-left: 2px solid rgba(237,230,216,0.28); line-height: 1.3; }
.bubble.ai-bubble h1:first-child, .bubble.ai-bubble h2:first-child, .bubble.ai-bubble h3:first-child, .bubble.ai-bubble h4:first-child { margin-top: 2px; }
.bubble.ai-bubble h1 { font-size: 16px; }
.bubble.ai-bubble h2 { font-size: 15px; }
.bubble.ai-bubble h3 { font-size: 13.5px; }
.bubble.ai-bubble h4 { font-size: 12.5px; color: var(--ink-dim); border-left-color: var(--stone-dim); }
.bubble.ai-bubble p { margin: 6px 0; }
.bubble.ai-bubble ul, .bubble.ai-bubble ol { margin: 8px 0; padding-left: 18px; display: flex; flex-direction: column; gap: 6px; }
.bubble.ai-bubble li { line-height: 1.55; }
.bubble.ai-bubble li::marker { color: var(--stone-dim); }
.bubble.ai-bubble li ul, .bubble.ai-bubble li ol { color: var(--ink-dim); }
.bubble.ai-bubble strong { color: var(--ivory); font-weight: 700; }
.bubble.ai-bubble hr { border: none; border-top: 1px solid var(--hairline-soft); margin: 16px 0; }
.bubble.ai-bubble code { font-family: 'IBM Plex Mono', monospace; font-size: 12px; background: rgba(237,230,216,0.09); color: #E8E3D8; padding: 1px 5px; border-radius: 4px; }
.bubble.ai-bubble a { color: var(--brass); text-decoration: underline; text-decoration-color: rgba(201,169,106,0.35); text-underline-offset: 2px; transition: color .15s ease, text-decoration-color .15s ease; overflow-wrap: anywhere; word-break: break-word; white-space: normal; }
.bubble.ai-bubble a:hover { color: var(--brass-bright); text-decoration-color: var(--brass-bright); }
.bubble.ai-bubble a:visited { color: var(--brass); }
.bubble.ai-bubble .src-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 12px 0 2px; }
.bubble.ai-bubble a.src-chip { display: inline-flex; align-items: center; justify-content: center; width: 30px; height: 30px; border-radius: 50%; background: rgba(237,230,216,0.08); border: 1px solid var(--hairline); text-decoration: none; overflow: hidden; transition: border-color .15s ease, background-color .15s ease, transform .15s ease; -webkit-tap-highlight-color: transparent; }
.bubble.ai-bubble a.src-chip img { width: 18px; height: 18px; border-radius: 4px; display: block; object-fit: contain; }
.bubble.ai-bubble a.src-chip:hover { border-color: var(--brass); background: rgba(201,169,106,0.14); transform: translateY(-1px); }
.bubble.ai-bubble a.src-chip.src-fallback { font-family: 'Manrope', sans-serif; font-size: 12px; font-weight: 700; color: var(--brass); }

/* ============ KOPYALANABİLİR BLOKLAR (kod / liste) ============ */
.block-copy-btn { position: absolute; top: 6px; right: 6px; width: 26px; height: 26px; border: 1px solid var(--hairline); background: rgba(53,45,39,0.75); color: var(--stone); border-radius: 7px; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: color .15s ease, border-color .15s ease, background .15s ease; z-index: 2; flex-shrink: 0; }
.block-copy-btn:hover { color: var(--ivory); border-color: var(--stone); }
.block-copy-btn:active { transform: scale(0.92); }
.block-copy-btn svg { width: 13px; height: 13px; stroke-width: 2; }
.block-copy-btn.copied { color: var(--emerald); border-color: var(--emerald); }

.code-block-wrapper { position: relative; margin: 12px 0; border-radius: var(--radius-md); overflow: hidden; border: 1px solid var(--hairline); background: var(--code-bg); max-width: 100%; }
.code-block-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 8px 6px 12px; background: var(--obsidian-2); border-bottom: 1px solid var(--hairline-soft); }
.code-lang-label { font-family: 'IBM Plex Mono', monospace; font-size: 10px; color: var(--stone); text-transform: lowercase; letter-spacing: 0.04em; }
.code-block-wrapper .block-copy-btn { position: static; }
.code-block-wrapper pre { margin: 0; padding: 12px 14px; overflow-x: auto; max-width: 100%; -webkit-overflow-scrolling: touch; }
.code-block-wrapper pre code.hljs { display: block; background: transparent !important; padding: 0; font-family: 'IBM Plex Mono', monospace; font-size: 12.5px; line-height: 1.6; white-space: pre; color: var(--ivory); }



/* ============ SÖZ DİZİMİ RENKLENDİRME (obsidian/brass paleti) ============ */
.hljs { color: var(--ivory); background: transparent; }
.hljs-comment, .hljs-quote { color: var(--stone-dim); font-style: italic; }
.hljs-keyword, .hljs-selector-tag, .hljs-literal, .hljs-subst, .hljs-doctag, .hljs-meta-keyword { color: var(--brass-bright); font-weight: 600; }
.hljs-string, .hljs-regexp, .hljs-addition, .hljs-attribute, .hljs-meta-string { color: var(--emerald); }
.hljs-number, .hljs-symbol, .hljs-bullet, .hljs-link { color: var(--code-rose); }
.hljs-title, .hljs-title.function_, .hljs-title.class_, .hljs-section { color: var(--brass); font-weight: 600; }
.hljs-name, .hljs-tag, .hljs-selector-id, .hljs-selector-class, .hljs-type { color: var(--code-rose); }
.hljs-variable, .hljs-template-variable, .hljs-attr, .hljs-params { color: var(--ivory); opacity: 0.85; }
.hljs-built_in, .hljs-builtin-name { color: var(--brass-bright); }
.hljs-deletion { color: var(--danger); }
.hljs-meta { color: var(--stone); }
.hljs-emphasis { font-style: italic; }
.hljs-strong { font-weight: 700; }
.bubble.user-bubble { background: var(--obsidian-3); border: 1px solid var(--hairline); border-radius: var(--radius-md) var(--radius-md) 4px var(--radius-md); color: var(--ivory); font-size: 13px; max-width: 78%; align-self: flex-end; }
.msg-meta { font-family: 'Manrope', sans-serif; font-size: 10px; font-weight: 600; color: var(--meta-bright); margin-bottom: 2px; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; letter-spacing: 0.03em; text-transform: uppercase; min-height: 16px; }
.msg-meta .seal-mini { width: 14px; height: 14px; flex-shrink: 0; border-radius: 50%; border: 1px solid #BFA68A; display: flex; align-items: center; justify-content: center; font-family: 'Fraunces', serif; font-style: italic; font-weight: 500; font-size: 8px; color: #D3BC9F; text-transform: none; line-height: 1; }
.speak-btn { display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; margin-left: 0; color: var(--meta-bright); cursor: pointer; flex-shrink: 0; transition: color .15s ease, transform .1s ease; background: none; border: none; padding: 0; }
.speak-btn svg { width: 100%; height: 100%; }
.speak-btn:hover { color: var(--ivory); }
.speak-btn:active { transform: scale(0.9); }
.speak-btn.speaking { color: var(--emerald); }
.model-tag { font-family: 'Manrope', sans-serif; font-size: 9.5px; font-weight: 600; background: var(--obsidian-2); padding: 2px 7px; border-radius: 10px; border: 1px solid var(--hairline); color: var(--meta-bright); text-transform: none; }
.search-badge { display: inline-flex; align-items: center; gap: 3px; font-family: 'Manrope', sans-serif; font-size: 8.5px; font-weight: 600; background: var(--obsidian-2); padding: 2px 7px 2px 5px; border-radius: 10px; border: 1px solid var(--hairline); color: var(--brass); }
.search-badge svg { width: 9px; height: 9px; flex-shrink: 0; }
.sources-footer { margin: 2px 0 10px 34px; }
.sources-footer-label { display: block; font-family: 'Manrope', sans-serif; font-size: 10px; color: var(--stone-dim); margin-bottom: 6px; }
.sources-footer-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; }
.source-link { display: block; width: 100%; box-sizing: border-box; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: 'Manrope', sans-serif; font-size: 10.5px; color: var(--brass); background: var(--obsidian-2); border: 1px solid var(--hairline); padding: 5px 10px; border-radius: 20px; text-decoration: none; text-align: center; transition: color .15s ease, border-color .15s ease; }
.source-link:hover { color: var(--brass-bright); border-color: var(--brass); }
.msg-time { font-family: 'Manrope', sans-serif; font-size: 8.5px; font-variant-numeric: tabular-nums; letter-spacing: 0.03em; color: var(--time-gray); margin-top: 4px; text-align: right; }
.bubble.user-bubble .msg-time { color: rgba(237,234,226,0.4); }

/* ============ INPUT ============ */
.input-area { flex-shrink: 0; padding: 12px 20px 16px; border-top: none; background: var(--obsidian); }
.file-preview-img { max-width: 100%; max-height: 84px; border-radius: var(--radius-sm); margin-bottom: 6px; display: none; border: 1px solid var(--hairline); object-fit: contain; }
.file-badge { display: none; align-items: center; gap: 6px; font-size: 10.5px; color: var(--ivory); padding: 3px 8px; margin-bottom: 6px; background: rgba(237,230,216,0.08); border: 1px solid rgba(237,230,216,0.16); border-radius: 6px; width: fit-content; cursor: pointer; font-family: 'Manrope', sans-serif; font-weight: 500; }
/* ============ MESAJ KUTUSU ============
   Claude uygulamasindaki mesaj kutusunun yapisi: ustte yazi alani, altta solda (+) dugmesi ve
   model kutusu, sagda mikrofon ve gonder dugmesi. Renkler .input-wrapper altindaki --mb-*
   degiskenlerinden gelir; tek yerden degistirilebilir. */
.input-wrapper { --ta-lh: 24px; --ta-py: 6px; --btn: 34px; --mb-bg: #3F362F; --mb-border: #5A4E44; --mb-btn: #38302A; --mb-btn-hover: #443A33; --mb-text: #F5F2EA; --mb-muted: #B0A89E; --mb-icon: #E8E3D8; --mb-menu-bg: #4A4038; --mb-menu-border: #62564C; --mb-coffee: #6F5848; --mb-coffee-hover: #7C6453; --mb-coffee-deep: #4A3A2F; --mb-coffee-ink: #F1E7D8; }
.input-wrapper { position: relative; display: flex; flex-direction: column; width: 100%; max-width: 720px; margin: 0 auto; background: var(--mb-bg); border: 1px solid var(--mb-border); border-radius: 24px; padding: 10px; gap: 0; box-shadow: 0 8px 24px rgba(0,0,0,0.28); transition: border-color .2s ease; }
.input-wrapper:focus-within { border-color: #6E6155; }
.input-wrapper textarea { width: 100%; background: transparent; border: none; outline: none; resize: none; font-family: 'Manrope', sans-serif; font-size: 16px; font-weight: 400; color: var(--mb-text); caret-color: var(--mb-text); line-height: var(--ta-lh); padding: var(--ta-py) 8px; margin: 0 0 8px; display: block; white-space: pre-wrap; word-wrap: break-word; overflow-y: hidden; overflow-x: hidden; height: calc(var(--ta-lh) + var(--ta-py) * 2); min-height: calc(var(--ta-lh) + var(--ta-py) * 2); max-height: 20vh; transition: height .18s cubic-bezier(.2,.8,.2,1); }
.input-wrapper textarea::placeholder { color: var(--mb-muted); font-weight: 400; }
.input-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: var(--btn); }
.input-row-left { display: flex; align-items: center; gap: 8px; flex: 1 1 auto; min-width: 0; }
.input-row-right { display: flex; align-items: center; gap: 8px; flex: none; }

/* Yuvarlak dugmeler: (+) ve mikrofon */
.act-btn { width: var(--btn); height: var(--btn); padding: 0; border: none; background: var(--mb-coffee); color: var(--mb-coffee-ink); border-radius: 50%; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: background-color .15s ease, transform .15s ease, color .15s ease; flex-shrink: 0; }
@media (hover: hover) { .act-btn:hover { background: var(--mb-coffee-hover); } }
.act-btn:active { transform: scale(0.94); }
.act-btn svg { width: 18px; height: 18px; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
#plusBtn svg { width: 20px; height: 20px; transition: transform .22s cubic-bezier(.2,.8,.2,1); }
#plusBtn.is-open svg { transform: rotate(45deg); }

/* Model kutusu: model adi belirgin, yanindaki ek bilgi soluk */
.model-pill { display: flex; align-items: center; gap: 5px; height: var(--btn); padding: 0 14px; border: none; border-radius: 999px; background: var(--mb-coffee); color: var(--mb-coffee-ink); font-family: 'Manrope', sans-serif; font-size: 14px; font-weight: 600; line-height: 1; cursor: pointer; flex: 0 1 auto; min-width: 0; overflow: hidden; transition: background-color .15s ease, transform .15s ease; }
@media (hover: hover) { .model-pill:hover { background: var(--mb-coffee-hover); } }
.model-pill:active { transform: scale(0.98); }
.model-pill .mp-name { white-space: nowrap; flex-shrink: 0; }
.model-pill .mp-sub { white-space: nowrap; flex-shrink: 0; color: var(--mb-coffee-ink); opacity: 0.72; font-weight: 500; }
.model-pill .mp-sub:empty { display: none; }
.model-pill.no-sub .mp-sub { display: none; }
.model-pill.no-sub .mp-name { flex-shrink: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }

/* Gonder dugmesi: acik renkli yuvarlak, ince cizgili ok */
.send-btn { position: relative; width: var(--btn); height: var(--btn); padding: 0; border: none; background: var(--mb-coffee); color: var(--mb-coffee-ink); border-radius: 50%; display: none; align-items: center; justify-content: center; cursor: pointer; flex-shrink: 0; transition: background-color .25s ease, transform .2s ease, opacity .2s ease; }
.send-btn svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; transition: transform .3s cubic-bezier(.2,.8,.2,1); }
@media (hover: hover) { .send-btn:hover { background: var(--mb-coffee-hover); } .send-btn:hover svg { transform: translateY(-1px); } }
.send-btn:active { transform: scale(0.94); }
.send-btn:disabled { cursor: default; opacity: .5; }
.send-btn, .act-btn, .model-pill, .mb-item { -webkit-tap-highlight-color: transparent; -webkit-touch-callout: none; user-select: none; -webkit-user-select: none; }
.send-btn:focus, .act-btn:focus, .model-pill:focus, .mb-item:focus { outline: none; }
.send-btn:focus-visible, .act-btn:focus-visible, .model-pill:focus-visible, .mb-item:focus-visible { outline: 2px solid rgba(245,242,234,0.5); outline-offset: 2px; }
.send-btn::after { content: ''; position: absolute; inset: 0; border-radius: 50%; pointer-events: none; opacity: 0; }
.send-btn.is-firing::after { animation: send-pulse .75s cubic-bezier(.2,.7,.2,1) 1; }
@keyframes send-pulse {
  0%   { opacity: 1; box-shadow: 0 0 0 0 rgba(185,139,98,0.55), 0 0 10px 2px rgba(185,139,98,0.4); }
  100% { opacity: 1; box-shadow: 0 0 0 10px rgba(185,139,98,0), 0 0 26px 8px rgba(185,139,98,0); }
}

/* Talk dugmesi: dalga ikonlu hap, kahve tonunda. Yazi yokken gorunur; yazmaya baslayinca yerini gonder oku alir. */
.input-wrapper.has-input .talk-btn { display: none; }
.input-wrapper.has-input .send-btn { display: flex; animation: mb-pop .22s cubic-bezier(.2,.8,.2,1); }
.talk-btn { position: relative; display: flex; align-items: center; justify-content: center; width: var(--btn); height: var(--btn); padding: 0; border: none; border-radius: 50%; background: var(--mb-coffee); color: var(--mb-coffee-ink); font-family: 'Manrope', sans-serif; font-size: 14px; font-weight: 600; line-height: 1; white-space: nowrap; cursor: pointer; flex-shrink: 0; animation: mb-pop .22s cubic-bezier(.2,.8,.2,1); transition: background-color .2s ease, color .2s ease, transform .15s ease; -webkit-tap-highlight-color: transparent; -webkit-touch-callout: none; user-select: none; -webkit-user-select: none; }
@media (hover: hover) { .talk-btn:hover { background: var(--mb-coffee-hover); } .talk-btn.is-active:hover { background: #5A4638; } }
.talk-btn:active { transform: scale(0.96); }
.talk-btn:focus { outline: none; }
.talk-btn:focus-visible { outline: 2px solid rgba(245,242,234,0.5); outline-offset: 2px; }
.talk-btn:disabled { opacity: .5; cursor: default; }
.talk-btn.is-active { background: var(--mb-coffee-deep); color: var(--mb-text); }
.talk-wave { display: flex; align-items: center; gap: 2px; height: 18px; }
.talk-wave i { display: block; width: 2px; height: var(--h, 8px); border-radius: 2px; background: currentColor; transform-origin: center; }
.input-wrapper.voice-listening { border-color: #6B4F3B; }
.input-wrapper.voice-listening .talk-btn { animation: talk-ring 1.8s ease-out infinite; }
.input-wrapper.voice-listening .talk-wave i { animation: talk-bar .9s ease-in-out infinite; animation-delay: calc(var(--i) * -0.11s); }
.input-wrapper.voice-thinking .talk-wave i { animation: talk-think 1.2s ease-in-out infinite; animation-delay: calc(var(--i) * -0.14s); }
.input-wrapper.voice-speaking .talk-wave i { animation: talk-bar .65s ease-in-out infinite; animation-delay: calc(var(--i) * -0.09s); }
@keyframes talk-bar { 0%, 100% { transform: scaleY(.35); } 50% { transform: scaleY(1.15); } }
@keyframes talk-think { 0%, 100% { transform: scaleY(.6); opacity: .45; } 50% { transform: scaleY(.9); opacity: 1; } }
@keyframes talk-ring { 0% { box-shadow: 0 0 0 0 rgba(185,139,98,0.55); } 70%, 100% { box-shadow: 0 0 0 10px rgba(185,139,98,0); } }
@keyframes mb-pop { from { opacity: 0; transform: scale(.8); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) { .talk-btn, .talk-wave i, .send-btn { animation: none !important; } }

/* Acilir menuler: (+) menusu ve model secici kutunun hemen ustunde acilir */
.mb-menu { position: absolute; bottom: calc(100% + 10px); left: 0; z-index: 30; min-width: 210px; background: var(--mb-menu-bg); border: 1px solid var(--mb-menu-border); border-radius: 20px; padding: 6px; box-shadow: 0 14px 40px rgba(0,0,0,0.5); opacity: 0; visibility: hidden; transform: translateY(6px) scale(0.98); transform-origin: bottom left; transition: opacity .16s ease, transform .16s ease, visibility 0s linear .16s; }
.mb-menu.open { opacity: 1; visibility: visible; transform: none; transition: opacity .16s ease, transform .16s ease, visibility 0s; }
.mb-menu-model { left: 52px; width: min(290px, calc(100% - 52px)); }
.mb-item { display: flex; align-items: center; gap: 12px; width: 100%; padding: 10px 12px; border: none; background: none; border-radius: 14px; color: var(--mb-text); font-family: 'Manrope', sans-serif; font-size: 15px; font-weight: 500; text-align: left; cursor: pointer; transition: background-color .12s ease; }
@media (hover: hover) { .mb-item:hover { background: rgba(237,230,216,0.07); } }
.mb-item:active { background: rgba(237,230,216,0.10); }
.mb-item svg { width: 20px; height: 20px; flex-shrink: 0; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
.mb-item-text { display: flex; flex-direction: column; min-width: 0; }
.mb-item-name { display: block; line-height: 1.25; }
.mb-item-desc { display: block; margin-top: 2px; font-size: 12.5px; font-weight: 400; line-height: 1.3; color: var(--mb-muted); }
.mb-item svg.mb-check { width: 18px; height: 18px; margin-left: auto; stroke-width: 2; opacity: 0; transition: opacity .12s ease; }
.mb-model.selected .mb-check { opacity: 1; }

@media (max-width: 640px) {
  .sidebar { position: fixed; left: -280px; top: 0; bottom: 0; width: 250px; z-index: 1000; transition: left 0.28s ease, box-shadow 0.28s ease; border-right: 1px solid var(--hairline-soft); background: var(--obsidian-2); box-shadow: none; }
  .sidebar.open { left: 0; box-shadow: 20px 0 40px rgba(0,0,0,0.4); }
  .sidebar-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 999; }
  .sidebar-overlay.open { display: block; }
  .sidebar-toggle { display: flex; }
  .header { padding: 0 14px; }
  .chat-area { padding: 70px 16px 16px; }
  .welcome-title { font-size: 19px; }
  .input-area { padding: 8px 12px 14px; }
  .bubble.user-bubble { max-width: 86%; }

  /* iOS Safari, 16px altindaki yazi kutularina odaklaninca sayfayi otomatik
     yakinlastirir (zoom). Asagidaki uc metin girisi alani mobilde 16px'e cikarilarak
     bu istenmeyen otomatik yakinlasma engellenir. Mesaj kutusu da 16px kullanir (bkz. .input-wrapper textarea). */
  .rename-input { font-size: 16px; }
  .settings-textarea { font-size: 16px; }
  .api-key-input { font-size: 16px; }
}

/* ============ CAMERA OVERLAY ============ */
.camera-overlay { position: fixed; inset: 0; background: #000; z-index: 1000; display: none; flex-direction: column; }
.camera-top-bar { flex-shrink: 0; display: flex; justify-content: flex-end; padding: 12px 14px; }
.camera-close-btn { width: 32px; height: 32px; border-radius: 50%; background: rgba(237,234,226,0.10); border: 1px solid var(--hairline); color: var(--ivory); display: flex; align-items: center; justify-content: center; cursor: pointer; }
.camera-close-btn svg { width: 14px; height: 14px; }
#cameraVideo { flex: 1; width: 100%; object-fit: cover; background: #000; }
.camera-bottom-bar { flex-shrink: 0; display: flex; justify-content: center; align-items: center; padding: 16px 0 24px; }
.camera-capture-btn { width: 56px; height: 56px; border-radius: 50%; background: #F5F2EA; border: 4px solid rgba(245,242,234,0.28); cursor: pointer; transition: transform .15s ease; padding: 0; }
.camera-capture-btn:active { transform: scale(0.9); }

/* ============ CONFIRM DIALOG ============ */
.confirm-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.60); z-index: 1001; display: none; align-items: center; justify-content: center; padding: 20px; backdrop-filter: blur(3px); }
.confirm-box { background: var(--obsidian-2); border: 1px solid var(--hairline); border-radius: 14px; padding: 22px 20px 18px; max-width: 280px; width: 100%; text-align: center; box-shadow: 0 20px 50px rgba(0,0,0,0.55); }
.confirm-icon { width: 38px; height: 38px; border-radius: 50%; background: rgba(181,86,75,0.10); border: 1px solid rgba(181,86,75,0.3); display: flex; align-items: center; justify-content: center; margin: 0 auto 12px; color: var(--danger); }
.confirm-icon svg { width: 16px; height: 16px; }
.confirm-title { font-family: 'Fraunces', serif; font-size: 14px; font-weight: 600; color: var(--ivory); margin-bottom: 5px; }
.confirm-desc { font-size: 11.5px; color: var(--stone); line-height: 1.5; margin-bottom: 16px; }
.confirm-actions { display: flex; gap: 8px; }
.confirm-btn { flex: 1; padding: 9px 0; border-radius: 8px; font-size: 12px; font-weight: 600; border: none; cursor: pointer; transition: opacity .15s ease; font-family: 'Manrope', sans-serif; }
.confirm-btn-cancel { background: var(--obsidian-3); color: var(--ivory); border: 1px solid var(--hairline); }
.confirm-btn-cancel:active { opacity: 0.7; }
.confirm-btn-danger { background: var(--danger); color: #fff; box-shadow: 0 0 10px rgba(181,86,75,0.2); }
.confirm-btn-danger:active { opacity: 0.85; }
.confirm-btn-primary { background: #F5F2EA; color: #171310; }
.confirm-btn-primary:active { opacity: 0.85; }

/* ============ AYARLAR MODALI ============ */
.sidebar-item.pinned-item .icon { color: var(--brass-bright); }

/* ============ SOHBET BAĞLAM MENÜSÜ (uzun basma) ============ */
.chat-menu-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55); z-index: 1002; display: none; align-items: flex-end; justify-content: center; }
.chat-menu-sheet { background: var(--obsidian-2); border: 1px solid var(--hairline); border-top-left-radius: 16px; border-top-right-radius: 16px; width: 100%; max-width: 420px; padding: 8px 10px calc(10px + env(safe-area-inset-bottom, 0px)); box-shadow: 0 -12px 40px rgba(0,0,0,0.5); }
.chat-menu-title { font-family: 'Fraunces', serif; font-size: 13px; color: var(--stone); padding: 12px 12px 8px; border-bottom: 1px solid var(--hairline-soft); margin-bottom: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chat-menu-item { display: flex; align-items: center; gap: 10px; width: 100%; background: none; border: none; color: var(--ivory); font-family: 'Manrope', sans-serif; font-size: 13.5px; padding: 12px; border-radius: 10px; cursor: pointer; text-align: left; }
.chat-menu-item:active { background: rgba(255,255,255,0.05); }
.chat-menu-icon { width: 18px; height: 18px; flex-shrink: 0; color: var(--ink-dim); display: flex; }
.chat-menu-icon svg { width: 100%; height: 100%; }
.chat-menu-item-danger { color: var(--danger); }
.chat-menu-item-danger .chat-menu-icon { color: var(--danger); }
.chat-menu-cancel { justify-content: center; color: var(--stone); margin-top: 6px; padding-top: 14px; border-top: 1px solid var(--hairline-soft); border-radius: 0; }

/* ============ YENİDEN ADLANDIRMA KUTUSU ============ */
.rename-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55); z-index: 1002; display: none; align-items: center; justify-content: center; padding: 20px; }
.rename-box { background: var(--obsidian-2); border: 1px solid var(--hairline); border-radius: 16px; padding: 18px; width: 100%; max-width: 340px; box-shadow: 0 20px 50px rgba(0,0,0,0.5); }
.rename-title { font-family: 'Fraunces', serif; font-size: 14px; font-weight: 600; color: var(--ivory); margin-bottom: 12px; }
.rename-input { width: 100%; background: var(--obsidian-3); border: 1px solid var(--hairline); border-radius: 9px; padding: 9px 11px; color: var(--ivory); font-family: 'Manrope', sans-serif; font-size: 13px; outline: none; margin-bottom: 14px; transition: border-color .15s ease; }
.rename-input:focus { border-color: #6E6155; }
.rename-actions { display: flex; gap: 8px; }

.settings-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.60); z-index: 1001; display: none; align-items: center; justify-content: center; padding: 20px; backdrop-filter: blur(3px); }
.settings-box { background: var(--obsidian-2); border: 1px solid var(--hairline); border-radius: 16px; padding: 18px 18px 16px; max-width: 420px; width: 100%; max-height: 86vh; overflow-y: auto; box-shadow: 0 20px 50px rgba(0,0,0,0.55); }
.settings-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.settings-title { font-family: 'Fraunces', serif; font-size: 16px; font-weight: 600; color: var(--ivory); }
.settings-body { display: flex; flex-direction: column; gap: 20px; }
.settings-field { display: flex; flex-direction: column; gap: 6px; }
.settings-label { font-family: 'Fraunces', serif; font-size: 13px; font-weight: 600; color: var(--ivory); padding-left: 9px; border-left: 2px solid rgba(237,230,216,0.28); }
.settings-hint { font-size: 11px; color: var(--stone); line-height: 1.5; }
.settings-textarea { width: 100%; min-height: 140px; max-height: 260px; background: var(--obsidian-3); border: 1px solid var(--hairline); border-radius: 10px; padding: 10px 12px; color: var(--ivory); font-family: 'IBM Plex Mono', monospace; font-size: 12px; line-height: 1.6; resize: vertical; outline: none; transition: border-color .15s ease; }
.settings-textarea:focus { border-color: #6E6155; }
.settings-slider-row { display: flex; align-items: center; gap: 10px; }
.settings-slider-cap { font-size: 10px; color: var(--stone-dim); font-family: 'Manrope', sans-serif; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; flex-shrink: 0; }
.settings-slider { flex: 1; accent-color: #E8E3D8; height: 4px; cursor: pointer; }
.settings-slider-value { text-align: center; font-family: 'Manrope', sans-serif; font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; color: var(--ivory); }
.settings-model-temps { display: flex; flex-direction: column; gap: 14px; }
.settings-model-temp-item { background: var(--obsidian-3); border: 1px solid var(--hairline); border-radius: 10px; padding: 10px 12px; }
.settings-model-temp-name { font-family: 'Manrope', sans-serif; font-size: 12px; font-weight: 600; color: var(--ivory); margin-bottom: 6px; display: flex; justify-content: space-between; }
.settings-model-temp-name .val { color: var(--ivory); font-variant-numeric: tabular-nums; }
.settings-danger-btn { display: flex; align-items: center; justify-content: center; gap: 8px; width: 100%; padding: 11px 14px; border-radius: 10px; border: 1px solid rgba(224,118,107,0.45); background: rgba(224,118,107,0.07); color: #E0766B; font-family: 'Manrope', sans-serif; font-size: 13px; font-weight: 600; cursor: pointer; transition: background-color .15s ease, border-color .15s ease; -webkit-tap-highlight-color: transparent; }
@media (hover: hover) { .settings-danger-btn:hover { background: rgba(224,118,107,0.13); border-color: rgba(224,118,107,0.65); } }
.settings-danger-btn:active { background: rgba(224,118,107,0.18); }
.settings-danger-btn:focus { outline: none; }
.settings-danger-btn:focus-visible { outline: 2px solid rgba(245,242,234,0.5); outline-offset: 2px; }
.settings-danger-btn svg { width: 16px; height: 16px; flex-shrink: 0; }
#confirmOverlay { z-index: 1003; }
.settings-actions { display: flex; gap: 8px; margin-top: 18px; }
.settings-saved-msg { display: none; text-align: center; font-size: 11px; color: var(--emerald); margin-top: 10px; }
.settings-saved-msg.show { display: block; }

.settings-tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 1px solid var(--hairline-soft); }
.settings-tab-btn { flex: 1; padding: 9px 0 10px; background: none; border: none; color: var(--stone); font-family: 'Manrope', sans-serif; font-size: 12px; font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent; transition: color .15s ease, border-color .15s ease; margin-bottom: -1px; }
.settings-tab-btn.active { color: var(--brass-bright); border-bottom-color: var(--brass); }
.settings-tab-content { display: none; }
.settings-tab-content.active { display: block; }

.api-keys-list { display: flex; flex-direction: column; gap: 12px; }
.api-key-item { background: var(--obsidian-3); border: 1px solid var(--hairline); border-radius: 10px; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.api-key-item-label-row { display: flex; align-items: center; justify-content: space-between; }
.api-key-item-label { font-family: 'Manrope', sans-serif; font-size: 12.5px; color: var(--ivory); font-weight: 600; }
.api-key-item-status { font-size: 9px; padding: 2px 8px; border-radius: 20px; font-family: 'Manrope', sans-serif; font-weight: 600; letter-spacing: 0.02em; }
.api-key-item-status.active { background: rgba(62,142,126,0.12); color: var(--emerald); border: 1px solid rgba(62,142,126,0.3); }
.api-key-item-status.empty { background: rgba(255,255,255,0.03); color: var(--stone-dim); border: 1px solid var(--hairline); }
.api-key-input-row { display: flex; gap: 6px; }
.api-key-input { flex: 1; min-width: 0; background: var(--obsidian-2); border: 1px solid var(--hairline); border-radius: 8px; padding: 8px 10px; color: var(--ivory); font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; outline: none; transition: border-color .15s ease; }
.api-key-input:focus { border-color: #6E6155; }
.api-key-toggle-btn { flex-shrink: 0; width: 34px; background: var(--obsidian-2); border: 1px solid var(--hairline); border-radius: 8px; color: var(--stone); display: flex; align-items: center; justify-content: center; cursor: pointer; }
.api-key-toggle-btn svg { width: 14px; height: 14px; }
.api-key-item-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.api-key-save-btn { padding: 7px 16px; border-radius: 7px; font-size: 11px; font-weight: 600; border: none; cursor: pointer; background: #F5F2EA; color: #171310; transition: opacity .15s ease; font-family: 'Manrope', sans-serif; }
.api-key-save-btn:active { opacity: 0.85; }
.api-key-saved-msg { font-size: 10px; color: var(--emerald); opacity: 0; transition: opacity .2s ease; }
.api-key-saved-msg.show { opacity: 1; }

.toggle-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; background: var(--obsidian-3); border: 1px solid var(--hairline); border-radius: 10px; padding: 12px; margin-bottom: 12px; }
.toggle-row-text { display: flex; flex-direction: column; gap: 2px; }
.toggle-row-label { font-family: 'Manrope', sans-serif; font-size: 12.5px; color: var(--ivory); font-weight: 600; }
.toggle-row-hint { font-size: 10px; color: var(--stone-dim); }
.toggle-switch { position: relative; flex-shrink: 0; width: 42px; height: 24px; }
.toggle-switch input { opacity: 0; width: 0; height: 0; }
.toggle-switch-track { position: absolute; cursor: pointer; inset: 0; background: var(--obsidian-2); border: 1px solid var(--hairline); border-radius: 20px; transition: background-color .15s ease, border-color .15s ease; }
.toggle-switch-track::before { content: ""; position: absolute; height: 16px; width: 16px; left: 3px; top: 50%; transform: translateY(-50%); background: var(--stone-dim); border-radius: 50%; transition: transform .15s ease, background-color .15s ease; }
.toggle-switch input:checked + .toggle-switch-track { background: rgba(62,142,126,0.18); border-color: rgba(62,142,126,0.4); }
.toggle-switch input:checked + .toggle-switch-track::before { transform: translate(18px, -50%); background: var(--emerald); }

@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }

/* ============ CLAUDE TEMASI: yalnizca renk, golge, kenarlik, yazi tipi. Yerlesim ve boyut kurallarina dokunmaz. ============ */
:root {
  --obsidian: #2B2B29; --obsidian-2: #232321; --obsidian-3: #373735;
  --hairline: #4C4C48; --hairline-soft: #3A3A37;
  --ivory: #FAF9F5; --stone: #C2C0B6; --stone-dim: #9C9A92;
  --brass: #D97757; --brass-bright: #E8906F; --brass-dim: rgba(217,119,87,0.12);
  --code-bg: #232321; --meta-bright: #C2C0B6; --time-gray: #9C9A92; --ink-dim: #DEDCD1;
  --cl-accent: #D97757; --cl-accent-hover: #C6613F;
}
html, body { background: #2B2B29; }
::selection { background: rgba(217,119,87,0.30); color: #FAF9F5; }
.main-content, .chat-area, .input-area { background: #2B2B29; }
.sidebar { background: #232321; border-right-color: #3A3A37; }
.header { background: rgba(43,43,41,0.80); }
.sidebar-header .seal, .header-brand .seal { background: #D97757; border-color: transparent; color: #FAF9F5; box-shadow: none; }
.pulse-orb { background: transparent; border: none; color: #D97757; box-shadow: none; }
.welcome-title { font-family: 'Fraunces', Georgia, serif; font-weight: 400; color: #FAF9F5; text-shadow: none; }
.welcome-subtitle { color: #9C9A92; }
.hdr-clock-date { color: #C2C0B6; text-shadow: none; }
.hdr-clock-date .wd { color: #9C9A92; }
.hdr-clock-sep { background: linear-gradient(to bottom, transparent, rgba(194,192,182,0.35), transparent); }
.bubble.user-bubble { background: #343432; border-color: transparent; color: #FAF9F5; }
.bubble.ai-bubble { font-family: 'Fraunces', Georgia, serif; font-weight: 400; color: #FAF9F5; }
.bubble.ai-bubble h1, .bubble.ai-bubble h2, .bubble.ai-bubble h3 { border-left-color: transparent; }
.bubble.ai-bubble a { color: #E8906F; text-decoration-color: rgba(232,144,111,0.40); }
.bubble.ai-bubble code { background: rgba(250,249,245,0.08); }

/* Giris alani: Claude kartı */
.input-wrapper { --mb-bg: #373735; --mb-border: #4C4C48; --mb-btn: #454542; --mb-btn-hover: #4C4C48; --mb-text: #FAF9F5; --mb-muted: #8B8A84; --mb-icon: #C2C0B6; --mb-menu-bg: #343432; --mb-menu-border: #4C4C48; --mb-coffee: transparent; --mb-coffee-hover: rgba(250,249,245,0.08); --mb-coffee-deep: #4C4C48; --mb-coffee-ink: #C2C0B6; }
.input-wrapper { background: #373735; border-color: #4C4C48; box-shadow: 0 4px 24px rgba(0,0,0,0.35); }
.input-wrapper:focus-within { border-color: #5E5E59; }
.input-wrapper.voice-listening { border-color: #D97757; }

/* Butonlar (yalnizca gorunum, konumlar ayni) */
.act-btn { background: #454542; color: #FAF9F5; box-shadow: none; }
.model-pill { background: #454542; color: #FAF9F5; border-radius: 999px; box-shadow: none; }
.model-pill .mp-sub { color: #C2C0B6; }
.send-btn { background: #D97757; color: #FAF9F5; box-shadow: none; }
.talk-btn { background: #F7F7F3; color: #1A1A19; box-shadow: none; }
.talk-btn.is-active { background: #D97757; color: #FAF9F5; }
@media (hover: hover) {
  .act-btn:hover { background: #4C4C48; }
  .model-pill:hover { background: #4C4C48; }
  .send-btn:hover:not(:disabled) { background: #C6613F; }
  .talk-btn:hover { background: #E8E6DC; }
}

/* Menuler */
.hdr-menu { background: #343432; border-color: #4C4C48; }
.hdr-menu-sep { background: rgba(222,220,209,0.12); }
.hdr-menu-title, .hdr-menu-label { color: #9C9A92; }
.mb-item:active { background: rgba(250,249,245,0.08); }
.rename-input:focus, .settings-textarea:focus, .api-key-input:focus { border-color: rgba(217,119,87,0.7); }

/* Yazi tipi ve punto */
.bubble.ai-bubble { font-family: 'Source Serif 4', Georgia, serif; font-size: 15.5px; line-height: 1.65; }
.bubble.ai-bubble h1, .bubble.ai-bubble h2, .bubble.ai-bubble h3, .bubble.ai-bubble h4 { font-family: 'Source Serif 4', Georgia, serif; }
.bubble.ai-bubble h1 { font-size: 18px; }
.bubble.ai-bubble h2 { font-size: 17px; }
.bubble.ai-bubble h3 { font-size: 16px; }
.bubble.ai-bubble h4 { font-size: 15px; }
.bubble.user-bubble { font-family: 'Source Serif 4', Georgia, serif; font-size: 15.5px; line-height: 1.65; }
.welcome-title { font-family: 'Source Serif 4', Georgia, serif; }

/* Logo ve marka yazisi gizlendi (HTML/JS'e dokunulmadi) */
.sidebar-header .seal, .sidebar-title, .header-brand, .pulse-orb { display: none; }

/* Karsilama ekrani: saate gore degisen CANLI ikon (sabah/ogle/aksam/gece) */
.welcome-icon { width: 92px; height: 92px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; animation: welcome-icon-in .8s ease both; }
.welcome-icon svg { width: 100%; height: 100%; display: block; overflow: visible; }
@keyframes welcome-icon-in { from { opacity: 0; transform: translateY(8px) scale(.9); } to { opacity: 1; transform: none; } }
.welcome-icon .wi-glow { transform-box: fill-box; transform-origin: center; animation: wi-glow 4.5s ease-in-out infinite; }
.welcome-icon .wi-spin { transform-box: view-box; animation: wi-spin 50s linear infinite; }
.welcome-icon .wi-ray { animation: wi-ray 3s ease-in-out infinite alternate; }
.welcome-icon .wi-rise { animation: wi-rise 8s ease-in-out infinite alternate; }
.welcome-icon .wi-sink { animation: wi-sink 9s ease-in-out infinite alternate; }
.welcome-icon .wi-shimmer { animation: wi-shimmer 3s ease-in-out infinite alternate; }
.welcome-icon .wi-cloud { animation: wi-drift 11s ease-in-out infinite alternate; }
.welcome-icon .wi-cloud-b { animation-duration: 15s; animation-direction: alternate-reverse; }
.welcome-icon .wi-twinkle { transform-box: fill-box; transform-origin: center; animation: wi-twinkle 3s ease-in-out infinite; }
.welcome-icon .wi-float { animation: wi-float 7s ease-in-out infinite alternate; }
.welcome-icon .wi-shoot { opacity: 0; animation: wi-shoot 9s ease-in 2s infinite; }
@keyframes wi-glow { 0%, 100% { opacity: .6; transform: scale(.93); } 50% { opacity: 1; transform: scale(1.08); } }
@keyframes wi-spin { to { transform: rotate(360deg); } }
@keyframes wi-ray { from { opacity: .4; } to { opacity: 1; } }
@keyframes wi-rise { from { transform: translateY(16px); } to { transform: translateY(-4px); } }
@keyframes wi-sink { from { transform: translateY(-6px); } to { transform: translateY(17px); } }
@keyframes wi-shimmer { from { opacity: .15; } to { opacity: .65; } }
@keyframes wi-drift { from { transform: translateX(-9px); } to { transform: translateX(9px); } }
@keyframes wi-twinkle { 0%, 100% { opacity: .2; transform: scale(.55); } 50% { opacity: 1; transform: scale(1.15); } }
@keyframes wi-float { from { transform: translateY(-3px); } to { transform: translateY(3px); } }
@keyframes wi-shoot { 0% { opacity: 0; transform: translate(0, 0); } 4% { opacity: 1; } 14% { opacity: 0; transform: translate(34px, 20px); } 100% { opacity: 0; transform: translate(34px, 20px); } }
/* Hava durumuna gore ikonlar */
.welcome-icon .wi-drop { animation: wi-fall 1.5s linear infinite; }
.welcome-icon .wi-drop-s { animation-duration: 2.4s; }
.welcome-icon .wi-drop-f { animation-duration: 1s; }
.welcome-icon .wi-flake { animation: wi-snow 4.2s ease-in-out infinite; }
.welcome-icon .wi-flash { animation: wi-flash 3.6s linear infinite; }
.welcome-icon .wi-mist { animation: wi-mist 7s ease-in-out infinite alternate; }
.welcome-icon .wi-mist-b { animation-duration: 9s; animation-direction: alternate-reverse; }
.welcome-icon .wi-bob { animation: wi-bob 5s ease-in-out infinite alternate; }
@keyframes wi-fall { 0% { transform: translateY(-4px); opacity: 0; } 15% { opacity: 1; } 80% { opacity: 1; } 100% { transform: translateY(26px); opacity: 0; } }
@keyframes wi-snow { 0% { transform: translate(-3px, -4px); opacity: 0; } 15% { opacity: 1; } 50% { transform: translate(3px, 10px); } 85% { opacity: 1; } 100% { transform: translate(-2px, 26px); opacity: 0; } }
@keyframes wi-flash { 0%, 48%, 100% { opacity: .2; } 52% { opacity: 1; } 56% { opacity: .3; } 60% { opacity: 1; } 78% { opacity: .85; } 90% { opacity: .3; } }
@keyframes wi-mist { from { transform: translateX(-8px); opacity: .55; } to { transform: translateX(8px); opacity: 1; } }
@keyframes wi-bob { from { transform: translateY(-2px); } to { transform: translateY(2px); } }
.sidebar-header { padding: 16px 18px 0; border-bottom: none; }

/* Mikrofon (sesle yaz) butonu: artı butonuyla ayni boyut ve renk */
.mic-btn { width: var(--btn); height: var(--btn); padding: 0; border: none; background: #454542; color: #C2C0B6; border-radius: 50%; display: flex; align-items: center; justify-content: center; cursor: pointer; flex-shrink: 0; transition: background-color .15s ease, color .15s ease, transform .15s ease; -webkit-tap-highlight-color: transparent; -webkit-touch-callout: none; user-select: none; -webkit-user-select: none; }
.mic-btn svg { width: 18px; height: 18px; }
.mic-btn:active { transform: scale(0.94); }
.mic-btn:focus { outline: none; }
.mic-btn:focus-visible { outline: 2px solid rgba(245,242,234,0.5); outline-offset: 2px; }
.mic-btn.is-listening { background: #D97757; color: #FAF9F5; animation: talk-ring 1.8s ease-out infinite; }
.input-wrapper.voice-on .mic-btn { opacity: .4; pointer-events: none; }
@media (hover: hover) { .mic-btn:hover { background: #4C4C48; } .mic-btn.is-listening:hover { background: #C6613F; } }
@media (max-width: 370px) { .mic-btn { width: 30px; height: 30px; } }
/* ============ /CLAUDE TEMASI ============ */
</style>
</head>
<body>
<div class="app-container" id="appContainer">
  <div class="sidebar-overlay" id="sidebarOverlay" onclick="closeSidebar()"></div>
  <div class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <div class="seal">E</div>
      <div class="sidebar-title">Karar ve Strateji Asistanım<span class="sub">Kişisel Asistan</span></div>
    </div>
    <div class="sidebar-list" id="sidebarList"></div>
  </div>
  <div class="main-content">
    <header class="header">
      <div class="header-left">
        <button class="sidebar-toggle" onclick="toggleSidebar()" id="sidebarToggle"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h18M3 6h18M3 18h18"/></svg></button>
        <div class="hdr-clock" id="hdrClock" lang="tr" aria-label="Tarih, konum ve sıcaklık">
          <span class="hdr-clock-sep" aria-hidden="true"></span>
          <div class="hdr-clock-body">
            <div class="hdr-clock-date" id="hdrClockDate"></div>
            <div class="hdr-wx is-loading" id="hdrWx">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.4"/></svg>
              <span class="hdr-wx-place" id="hdrWxPlace">Konum aranıyor…</span>
              <span class="hdr-wx-dot" id="hdrWxDot" style="display:none"></span>
              <span class="hdr-wx-temp" id="hdrWxTemp"></span>
            </div>
          </div>
        </div>
      </div>
      <div class="header-actions">
        <button class="icon-btn" id="hdrHomeBtn" type="button" onclick="goHome()" title="Yeni sohbet" aria-label="Yeni sohbet"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/><path d="M12 8.5v6M9 11.5h6"/></svg></button>
        <div class="hdr-menu-wrap">
          <button class="icon-btn" id="hdrMoreBtn" type="button" onclick="toggleHdrMenu(event)" title="Daha fazla" aria-label="Daha fazla" aria-haspopup="true" aria-expanded="false"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true"><circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="19" r="1.7"/></svg></button>
          <div class="hdr-menu" id="hdrMenu" role="menu">
            <div id="hdrChatSection">
            <div class="hdr-menu-title" id="hdrMenuTitle">Sohbet</div>
            <button class="hdr-item" id="hdrPinItem" type="button" role="menuitem" onclick="hdrMenuAction('pin')"><span id="hdrPinIcon"></span><span id="hdrPinLabel">Sabitle</span></button>
            <button class="hdr-item" type="button" role="menuitem" onclick="hdrMenuAction('rename')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg><span>Yeniden adlandır</span></button>
            <button class="hdr-item hdr-item-danger" id="hdrDeleteItem" type="button" role="menuitem" onclick="hdrMenuAction('delete')"><svg viewBox="0 0 24 24" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg><span>Sil</span></button>
            </div>
            <div class="hdr-menu-sep" id="hdrMenuSep" role="separator"></div>
            <div class="hdr-menu-section" id="hdrSettingsSection">
              <div class="hdr-menu-label">Uygulama</div>
              <button class="hdr-item" id="hdrSettingsItem" type="button" role="menuitem" onclick="hdrMenuAction('settings')"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09a1.65 1.65 0 00-1-1.51 1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09a1.65 1.65 0 001.51-1 1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg><span>Ayarlar</span></button>
            </div>
          </div>
        </div>
      </div>
    </header>
    <div class="chat-area is-empty" id="chatArea">
      <div class="welcome-container" id="welcomeContainer">
        <div class="pulse-orb">E</div>
        <div class="welcome-title">İyi günler, Emre.</div>
        <div class="welcome-subtitle" style="min-height:1.5em"></div>
      </div>
    </div>
    <div class="input-area">
      <img id="filePreviewImg" class="file-preview-img">
      <div class="file-badge" id="fileBadge" onclick="removeFile()"><span id="fileBadgeName"></span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:9px;height:9px;flex-shrink:0"><path d="M18 6L6 18M6 6l12 12"/></svg></div>
      <div class="input-wrapper" id="inputWrapper">
        <textarea id="msgInput" rows="1" placeholder="Sor, birlikte çözelim."></textarea>
        <div class="input-row">
          <div class="input-row-left">
            <button class="act-btn" id="plusBtn" type="button" onclick="toggleMbMenu('plus', event)" title="Ekle" aria-label="Ekle" aria-haspopup="true" aria-expanded="false">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
            </button>
            <button class="model-pill" id="modelPill" type="button" onclick="toggleMbMenu('model', event)" title="Model seç" aria-label="Model seç" aria-haspopup="true" aria-expanded="false"><span class="mp-name" id="modelPillName">Otomatik</span><span class="mp-sub" id="modelPillSub"></span></button>
          </div>
          <div class="input-row-right">
            <button class="mic-btn" id="micBtn" type="button" onclick="toggleDictation()" title="Sesle yaz" aria-label="Sesle yaz" aria-pressed="false"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0"/><path d="M12 17.5V21"/></svg></button>
            <button class="talk-btn" id="talkBtn" type="button" onclick="toggleVoiceMode()" title="Sesli konuşmayı başlat" aria-label="Sesli konuşmayı başlat" aria-pressed="false"><span class="talk-wave" aria-hidden="true"><i style="--h:8px;--i:0"></i><i style="--h:14px;--i:1"></i><i style="--h:18px;--i:2"></i><i style="--h:12px;--i:3"></i><i style="--h:7px;--i:4"></i></span></button>
            <button class="send-btn" id="sendBtn" type="button" onclick="sendMsg()" title="Gönder" aria-label="Gönder" disabled><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5.5"/><path d="M6.25 11.25L12 5.5l5.75 5.75"/></svg></button>
          </div>
        </div>
        <input type="file" id="fileInput" accept="*/*" style="display:none" onchange="handleFile(event)">
        <input type="file" id="fileInputPdf" accept=".pdf,application/pdf" style="display:none" onchange="handleFile(event)">
        <input type="file" id="fileInputImage" accept="image/*" style="display:none" onchange="handleFile(event)">

        <div class="mb-menu mb-menu-plus" id="plusMenu" role="menu">
          <button class="mb-item" type="button" role="menuitem" onclick="closeMbMenus(); openCamera();">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg>
            <span class="mb-item-text"><span class="mb-item-name">Kamera</span></span>
          </button>
          <button class="mb-item" type="button" role="menuitem" onclick="closeMbMenus(); triggerFile();">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
            <span class="mb-item-text"><span class="mb-item-name">Dosya ekle</span></span>
          </button>
        </div>

        <div class="mb-menu mb-menu-model" id="modelMenu" role="menu">
          <button class="mb-item mb-model selected" type="button" role="menuitemradio" data-model="auto" onclick="selectModel('auto')">
            <span class="mb-item-text"><span class="mb-item-name">Otomatik</span><span class="mb-item-desc">Soruya göre en uygun modeli seçer</span></span>
            <svg class="mb-check" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>
          </button>
          <button class="mb-item mb-model" type="button" role="menuitemradio" data-model="openai/gpt-oss-20b" onclick="selectModel('openai/gpt-oss-20b')">
            <span class="mb-item-text"><span class="mb-item-name">GPT-OSS 20B</span><span class="mb-item-desc">Hızlı, günlük sorular için</span></span>
            <svg class="mb-check" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>
          </button>
          <button class="mb-item mb-model" type="button" role="menuitemradio" data-model="qwen/qwen3.8-27b" onclick="selectModel('qwen/qwen3.8-27b')">
            <span class="mb-item-text"><span class="mb-item-name">Qwen3.8 27B</span><span class="mb-item-desc">Görsel analiz destekli</span></span>
            <svg class="mb-check" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>
          </button>
          <button class="mb-item mb-model" type="button" role="menuitemradio" data-model="openai/gpt-oss-120b" onclick="selectModel('openai/gpt-oss-120b')">
            <span class="mb-item-text"><span class="mb-item-name">GPT-OSS 120B</span><span class="mb-item-desc">Derin ve ayrıntılı analiz</span></span>
            <svg class="mb-check" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="camera-overlay" id="cameraOverlay">
  <div class="camera-top-bar"><button class="camera-close-btn" onclick="closeCamera()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
  <video id="cameraVideo" autoplay playsinline muted></video>
  <div class="camera-bottom-bar"><button class="camera-capture-btn" onclick="captureCameraPhoto()"></button></div>
</div>

<div class="confirm-overlay" id="confirmOverlay">
  <div class="confirm-box">
    <div class="confirm-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg></div>
    <div class="confirm-title">Sohbet geçmişini sil</div>
    <div class="confirm-desc">Tüm mesajlar kalıcı olarak silinecek. Bu işlem geri alınamaz.</div>
    <div class="confirm-actions">
      <button class="confirm-btn confirm-btn-cancel" onclick="hideClearConfirm()">İptal</button>
      <button class="confirm-btn confirm-btn-danger" onclick="confirmClearHistory()">Sil</button>
    </div>
  </div>
</div>

<div class="chat-menu-overlay" id="chatMenuOverlay" onclick="if(event.target===this) closeChatMenu()">
  <div class="chat-menu-sheet">
    <div class="chat-menu-title" id="chatMenuTitle">Sohbet</div>
    <button type="button" class="chat-menu-item" onclick="openRenameChat()">
      <span class="chat-menu-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4 12.5-12.5z"/></svg></span>
      Yeniden Adlandır
    </button>
    <button type="button" class="chat-menu-item" onclick="togglePinChat()">
      <span class="chat-menu-icon" id="chatMenuPinIcon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16h14v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg></span>
      <span id="chatMenuPinLabel">Sabitle</span>
    </button>
    <button type="button" class="chat-menu-item chat-menu-item-danger" onclick="openDeleteChatConfirm()">
      <span class="chat-menu-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg></span>
      Sil
    </button>
    <button type="button" class="chat-menu-item chat-menu-cancel" onclick="closeChatMenu()">İptal</button>
  </div>
</div>

<div class="rename-overlay" id="renameOverlay">
  <div class="rename-box">
    <div class="rename-title">Sohbeti Yeniden Adlandır</div>
    <input type="text" id="renameInput" class="rename-input" maxlength="60" spellcheck="false" autocomplete="off">
    <div class="rename-actions">
      <button class="confirm-btn confirm-btn-cancel" onclick="closeRenameChat()">İptal</button>
      <button class="confirm-btn confirm-btn-primary" onclick="confirmRenameChat()">Kaydet</button>
    </div>
  </div>
</div>

<div class="confirm-overlay" id="deleteChatConfirmOverlay">
  <div class="confirm-box">
    <div class="confirm-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg></div>
    <div class="confirm-title">Sohbeti sil</div>
    <div class="confirm-desc" id="deleteChatConfirmDesc">Bu sohbet kalıcı olarak silinecek. Bu işlem geri alınamaz.</div>
    <div class="confirm-actions">
      <button class="confirm-btn confirm-btn-cancel" onclick="closeDeleteChatConfirm()">İptal</button>
      <button class="confirm-btn confirm-btn-danger" onclick="confirmDeleteChatFromMenu()">Sil</button>
    </div>
  </div>
</div>

<div class="settings-overlay" id="settingsOverlay">
  <div class="settings-box">
    <div class="settings-header">
      <div class="settings-title">Ayarlar</div>
      <button class="icon-btn" onclick="closeSettings()" title="Kapat"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div class="settings-tabs">
      <button class="settings-tab-btn active" data-tab="general" onclick="switchSettingsTab('general')">Genel</button>
      <button class="settings-tab-btn" data-tab="apikeys" onclick="switchSettingsTab('apikeys')">API Anahtarları</button>
    </div>

    <div class="settings-tab-content active" id="settingsTabGeneral">
      <div class="settings-body">
        <div class="settings-field">
          <div class="settings-label">Sistem Talimatı</div>
          <div class="settings-hint">Asistanın nasıl davranacağını belirleyen talimat. Değişiklik sadece bundan sonraki mesajlarda geçerli olur, geçmiş sohbet aynen kalır.</div>
          <textarea id="settingsSystemPrompt" class="settings-textarea" spellcheck="false"></textarea>
        </div>
        <div class="settings-field">
          <div class="settings-label">Yaratıcılık Seviyesi</div>
          <div class="settings-hint">Düşük değer daha tutarlı/kararlı, yüksek değer daha yaratıcı/çeşitli yanıtlar üretir. Her model kendi ölçeğinde ayrı ayarlanır.</div>
          <div id="settingsModelTemps" class="settings-model-temps"></div>
        </div>
        <div class="settings-field">
          <div class="settings-label">Sohbet Geçmişi</div>
          <div class="settings-hint">Tüm sohbetler ve mesajlar kalıcı olarak silinir. Bu işlem geri alınamaz.</div>
          <button type="button" class="settings-danger-btn" id="clearAllHistoryBtn" onclick="askClearAllHistory()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg><span>Tüm geçmişi temizle</span></button>
        </div>
      </div>
      <div class="settings-actions">
        <button class="confirm-btn confirm-btn-cancel" onclick="resetSettingsToDefault()">Varsayılana Dön</button>
        <button class="confirm-btn confirm-btn-primary" onclick="saveSettings()">Kaydet</button>
      </div>
      <div class="settings-saved-msg" id="settingsSavedMsg">Kaydedildi ✓</div>
    </div>

    <div class="settings-tab-content" id="settingsTabApiKeys">
      <div class="settings-hint" style="margin-bottom:4px">Groq API anahtarlarını buradan yönetebilirsin. 1-3 numaralı alanlar aktif anahtarlar; 4-6 numaralı alanlar boşsa, yeni anahtar eklemek için hazır bekliyor.</div>
      <div id="apiKeysList" class="api-keys-list"></div>

      <div class="settings-label" style="margin-top: 20px;">Arama API'si (Tavily)</div>
      <div class="settings-hint" style="margin-bottom: 10px;">Güncel/gerçek zamanlı bilgi gerektiren sorularda (haber, fiyat, hava durumu vb.) internetten sonuç getirmek için kullanılır. Boş bırakılırsa internet araması sessizce devre dışı kalır, sohbet normal şekilde çalışmaya devam eder.</div>

      <div class="toggle-row">
        <div class="toggle-row-text">
          <span class="toggle-row-label">İnternetten Arama</span>
          <span class="toggle-row-hint" id="searchEnabledHint">Açık</span>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="searchEnabledToggle" onchange="saveSearchEnabled(this.checked)">
          <span class="toggle-switch-track"></span>
        </label>
      </div>

      <div id="searchKeyItem" class="api-key-item">
        <div class="api-key-item-label-row">
          <span class="api-key-item-label">Tavily Anahtarı</span>
          <span class="api-key-item-status empty" id="searchKeyStatus">Boş</span>
        </div>
        <div class="api-key-input-row">
          <input type="password" class="api-key-input" id="searchKeyInput" placeholder="tvly-..." spellcheck="false" autocomplete="off">
          <button type="button" class="api-key-toggle-btn" onclick="toggleApiKeyVisibility(this)" title="Göster/Gizle"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button>
        </div>
        <div class="api-key-item-footer">
          <span class="api-key-saved-msg" id="searchKeySavedMsg">Kaydedildi ✓</span>
          <button type="button" class="api-key-save-btn" onclick="saveSearchKey()">Güncelle ve Kaydet</button>
        </div>
      </div>

      <div class="settings-label" style="margin-top: 20px;">Arama API'si (Serper)</div>
      <div class="settings-hint" style="margin-bottom: 10px;">Google arama sonuçlarını (Serper) getirmek için kullanılır. Boş bırakılırsa yalnızca Tavily (varsa) kullanılır.</div>

      <div id="serperKeyItem" class="api-key-item">
        <div class="api-key-item-label-row">
          <span class="api-key-item-label">Serper Anahtarı</span>
          <span class="api-key-item-status empty" id="serperKeyStatus">Boş</span>
        </div>
        <div class="api-key-input-row">
          <input type="password" class="api-key-input" id="serperKeyInput" placeholder="serper anahtarınız..." spellcheck="false" autocomplete="off">
          <button type="button" class="api-key-toggle-btn" onclick="toggleApiKeyVisibility(this)" title="Göster/Gizle"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button>
        </div>
        <div class="api-key-item-footer">
          <span class="api-key-saved-msg" id="serperKeySavedMsg">Kaydedildi ✓</span>
          <button type="button" class="api-key-save-btn" onclick="saveSerperKey()">Güncelle ve Kaydet</button>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
// ============================================
// SIDEBAR + SOHBET YÖNETİMİ
// ============================================
let chats = [];
let currentChatId = null;

// KRITIK: her sohbete GLOBAL olarak benzersiz bir kimlik (UUID) veriyoruz -
// basit bir sayac (0,1,2...) DEGIL. Sunucu artik hafizayi bu id'ye gore
// ayirdigi icin (bkz. backend'deki CHAT_HISTORIES), eger id'ler sadece
// tarayici basina sifirdan sayilsaydi, ayni sunucuyu kullanan FARKLI bir
// tarayici/cihazdaki "1 numarali sohbet" ile bu cihazdaki "1 numarali sohbet"
// CARPISIR ve yine sohbetler birbirine karisirdi. UUID bu riski tamamen ortadan kaldirir.
function generateChatId() {
  if (window.crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return 'c-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

class Chat {
  constructor(title = 'Yeni Sohbet') {
    this.id = generateChatId();
    this.title = title;
    this.messages = [];
    this.createdAt = new Date().toISOString();
    this.pinned = false;
  }
}

const ICON_PLUS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>';
const ICON_CHAT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
const ICON_CLOSE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>';
const ICON_COPY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
const ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
const ICON_RENAME = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>';
const ICON_PIN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16h14v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>';
const ICON_PIN_FILLED = '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 17v5h-1v-5z"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16h14v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>';
const ICON_TRASH_SM = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>';
const ICON_SPEAK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>';
const ICON_SPEAK_STOP = '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
const SEARCH_BADGE_HTML = '<span class="search-badge"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>İnternetten arandı</span>';

function extractSourceDomain(url) {
  // Pilde makale basligi yerine sade site adini (domain) gostermek icin.
  // "www." on eki kaldirilir; URL ayristirilamazsa (beklenmedik bir durum)
  // guvenli sekilde orijinal adrese geri doner.
  try {
    return new URL(url).hostname.replace(/^www\\./i, '');
  } catch (e) {
    return url;
  }
}

function buildSourcesFooter(sources) {
  // Kaynaklar footer'i (kutu/chip listesi) kullanicinin istegiyle tamamen
  // kaldirildi - artik kaynak, modelin cevap metninin sonuna "Kaynak;" basligi
  // altinda tiklanabilir link olarak eklendigi icin bu ayri bolume gerek yok.
  return null;
}

// ============================================
// KOD / LİSTE BLOKLARI: renklendirme + kopyala butonu
// ============================================
function copyTextToClipboard(text, btn) {
  const done = () => {
    btn.classList.add('copied');
    btn.innerHTML = ICON_CHECK;
    setTimeout(() => { btn.classList.remove('copied'); btn.innerHTML = ICON_COPY; }, 1400);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}
function fallbackCopy(text, done) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0;top:0;left:0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    done();
  } catch (e) {}
}
function makeCopyBtn(getText) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'block-copy-btn';
  btn.title = 'Kopyala';
  btn.innerHTML = ICON_COPY;
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    copyTextToClipboard(getText(), btn);
  });
  return btn;
}
// ============================================
// CEVAP METNI DUZENLEME (tum modeller icin ortak)
// ============================================
// 1) Dar/bolunmez bosluklar normal bosluga cevrilir (kelimeler yapismasin, satir kaymasin).
// 2) Ingilizce tarihler Turkceye cevrilir (03 Oct 2026 -> 03 Ekim 2026).
// 3) Uzun ve kisa tire karakterleri (em/en dash) kaldirilir.
// 4) bigpara adresi duzeltilir, duz yazilmis alan adlari tiklanabilir link olur.
// 5) Kaynaklar, cevabin sonunda tiklanabilir site ikonlari olarak gosterilir.
// Kod bloklari (pre/code) hicbir zaman degistirilmez.
// ===== CEVAP METNI BASLANGIC =====
const ODD_SPACES_RE = /[\\u00A0\\u1680\\u2000-\\u200A\\u202F\\u205F\\u3000]/g;
const EN_MONTH_TR = { january: 'Ocak', jan: 'Ocak', february: 'Şubat', feb: 'Şubat', march: 'Mart', mar: 'Mart', april: 'Nisan', apr: 'Nisan', may: 'Mayıs', june: 'Haziran', jun: 'Haziran', july: 'Temmuz', jul: 'Temmuz', august: 'Ağustos', aug: 'Ağustos', september: 'Eylül', sept: 'Eylül', sep: 'Eylül', october: 'Ekim', oct: 'Ekim', november: 'Kasım', nov: 'Kasım', december: 'Aralık', dec: 'Aralık' };
const EN_DAY_TR = { monday: 'Pazartesi', mon: 'Pazartesi', tuesday: 'Salı', tues: 'Salı', tue: 'Salı', wednesday: 'Çarşamba', wed: 'Çarşamba', thursday: 'Perşembe', thurs: 'Perşembe', thur: 'Perşembe', thu: 'Perşembe', friday: 'Cuma', fri: 'Cuma', saturday: 'Cumartesi', sat: 'Cumartesi', sunday: 'Pazar', sun: 'Pazar' };
const EN_MONTH_PAT = 'January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec';
const EN_DAY_PAT = 'Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tues|Tue|Wed|Thurs|Thur|Thu|Fri|Sat|Sun';
const TR_MONTH_PAT = 'Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık';
const RE_DATE_DMY = new RegExp(String.raw`\\b(\\d{1,2})(?:st|nd|rd|th)?\\s+(${EN_MONTH_PAT})\\b\\.?,?\\s+(\\d{4})\\b`, 'g');
const RE_DATE_MDY = new RegExp(String.raw`\\b(${EN_MONTH_PAT})\\b\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?,?\\s+(\\d{4})\\b`, 'g');
const RE_DATE_DM = new RegExp(String.raw`\\b(\\d{1,2})(?:st|nd|rd|th)?\\s+(${EN_MONTH_PAT})\\b`, 'g');
const RE_DATE_MD = new RegExp(String.raw`\\b(${EN_MONTH_PAT})\\s+(\\d{1,2})(?:st|nd|rd|th)?\\b(?![\\d:])`, 'g');
const RE_DATE_MY = new RegExp(String.raw`\\b(${EN_MONTH_PAT})\\s+(\\d{4})\\b`, 'g');
const RE_DATE_DAY = new RegExp(String.raw`\\b(${EN_DAY_PAT})\\b(?=,?\\s+\\d{1,2}\\s+(?:${TR_MONTH_PAT}))`, 'g');

function polishAnswerText(t, hasPrev) {
  if (!t) return t;
  t = t.replace(ODD_SPACES_RE, ' ');
  // Ingilizce tarihler -> Turkce
  t = t.replace(RE_DATE_DMY, (m, d, mo, y) => d + ' ' + EN_MONTH_TR[mo.toLowerCase()] + ' ' + y);
  t = t.replace(RE_DATE_MDY, (m, mo, d, y) => d + ' ' + EN_MONTH_TR[mo.toLowerCase()] + ' ' + y);
  t = t.replace(RE_DATE_DM, (m, d, mo) => d + ' ' + EN_MONTH_TR[mo.toLowerCase()]);
  t = t.replace(RE_DATE_MD, (m, mo, d) => d + ' ' + EN_MONTH_TR[mo.toLowerCase()]);
  t = t.replace(RE_DATE_MY, (m, mo, y) => EN_MONTH_TR[mo.toLowerCase()] + ' ' + y);
  t = t.replace(RE_DATE_DAY, m => EN_DAY_TR[m.toLowerCase()]);
  // Tireler: eksi isareti ve sayi araliklari normal kisa cizgiye, digerleri virgule
  t = t.replace(/\\u2212/g, '-');
  t = t.replace(/(\\d)\\s*[\\u2013\\u2012]\\s*(\\d)/g, '$1-$2');
  if (/[\\u2014\\u2013\\u2015\\u2012]/.test(t)) {
    t = t.replace(/^\\s*[\\u2014\\u2013\\u2015\\u2012]\\s*/, hasPrev ? ', ' : '');
    t = t.replace(/\\s*[\\u2014\\u2013\\u2015\\u2012]\\s*/g, ', ');
    t = t.replace(/([,.:;!?])\\s*,\\s/g, '$1 ');
  }
  return t;
}
// ===== CEVAP METNI BITIS =====

function polishAnswerDom(container) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue) return NodeFilter.FILTER_REJECT;
      if (node.parentElement && node.parentElement.closest('pre, code, .code-block-wrapper, .msg-time')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(n => {
    const before = n.nodeValue;
    const after = polishAnswerText(before, !!n.previousSibling);
    if (after !== before) n.nodeValue = after;
  });
}

// Eski sohbetlerde metnin sonuna eklenmis "Kaynak;" blogunu goruntuden cikarir
// (kaynaklar artik ikon satiri olarak gosteriliyor).
(function () {
  try {
    if (window.marked && typeof marked.parse === 'function' && !marked.__kaynakStrip) {
      const origParse = marked.parse.bind(marked);
      const KAYNAK_RE = /\\n*Kaynak;\\s*(?:\\[[^\\]\\n]+\\]\\([^)\\n]+\\)\\s*)+$/;
      marked.parse = function (src, opts) {
        return origParse(typeof src === 'string' ? src.replace(KAYNAK_RE, '') : src, opts);
      };
      marked.__kaynakStrip = true;
    }
  } catch (e) {}
})();

const BUYUKPARA_FIX_RE = /\\bb[üuÜU]y[üuÜU]kpara(?=\\.hurriyet\\.com)/gi;
const BARE_DOMAIN_RE = /((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\\.)+(?:com\\.tr|org\\.tr|net\\.tr|gov\\.tr|edu\\.tr|co\\.uk|com|net|org|edu|gov|io|info|biz|tr|co|ai|dev|app|tv|eu))(?![a-z0-9-])((?:\\/[^\\s<>"'\\)\\]]*)?)/gi;

function fixBuyukparaInContainer(container) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(n => {
    BUYUKPARA_FIX_RE.lastIndex = 0;
    if (BUYUKPARA_FIX_RE.test(n.nodeValue)) {
      BUYUKPARA_FIX_RE.lastIndex = 0;
      n.nodeValue = n.nodeValue.replace(BUYUKPARA_FIX_RE, 'bigpara');
    }
    BUYUKPARA_FIX_RE.lastIndex = 0;
  });
  container.querySelectorAll('a[href]').forEach(a => {
    const h = a.getAttribute('href') || '';
    const fixed = h.replace(BUYUKPARA_FIX_RE, 'bigpara');
    BUYUKPARA_FIX_RE.lastIndex = 0;
    if (fixed !== h) a.setAttribute('href', fixed);
  });
}

function linkifyBareDomains(container) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || node.nodeValue.indexOf('.') === -1) return NodeFilter.FILTER_REJECT;
      if (node.parentElement && node.parentElement.closest('a, pre, code, script, style, .code-block-wrapper, .msg-time')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(node => {
    const text = node.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0, changed = false, m;
    BARE_DOMAIN_RE.lastIndex = 0;
    while ((m = BARE_DOMAIN_RE.exec(text)) !== null) {
      const start = m.index;
      const prev = start > 0 ? text[start - 1] : '';
      let full = m[0];
      if (prev === '@' || /[A-Za-z0-9_\\-\\.\\/]/.test(prev)) continue;
      const trail = full.match(/[.,;:!?]+$/);
      if (trail) full = full.slice(0, full.length - trail[0].length);
      if (!full) continue;
      if (start > last) frag.appendChild(document.createTextNode(text.slice(last, start)));
      const a = document.createElement('a');
      a.href = 'https://' + full;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = full;
      frag.appendChild(a);
      last = start + full.length;
      BARE_DOMAIN_RE.lastIndex = last;
      changed = true;
    }
    if (!changed) return;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  });
}

// Kaynak ikonlari: her site icin tek bir tiklanabilir daire (favicon). Ayni site tekrar eklenmez.
// Ikon yuklenemezse sitenin bas harfi gosterilir. Sadece http/https adresleri kabul edilir.
function buildSourceRow(sources) {
  if (!Array.isArray(sources) || !sources.length) return null;
  const row = document.createElement('div');
  row.className = 'src-row';
  const seen = new Set();
  sources.forEach(src => {
    if (!src || typeof src.url !== 'string') return;
    let u;
    try { u = new URL(src.url.trim()); } catch (e) { return; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return;
    const host = u.hostname.replace(/^www\\./i, '');
    if (!host || seen.has(host)) return;
    seen.add(host);
    const a = document.createElement('a');
    a.className = 'src-chip';
    a.href = u.href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.title = host;
    a.setAttribute('aria-label', host + ' kaynağını aç');
    const img = document.createElement('img');
    img.alt = '';
    img.width = 18;
    img.height = 18;
    img.decoding = 'async';
    img.referrerPolicy = 'no-referrer';
    const fallback = () => {
      if (!img.parentNode) return;
      img.remove();
      a.classList.add('src-fallback');
      a.textContent = host.charAt(0).toUpperCase();
    };
    img.addEventListener('error', fallback);
    img.addEventListener('load', () => { if (img.naturalWidth < 2) fallback(); });
    img.src = 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(host) + '&sz=64';
    a.appendChild(img);
    row.appendChild(a);
  });
  return row.children.length ? row : null;
}

function enhanceContentBlocks(container) {
  if (!container) return;
  try { polishAnswerDom(container); fixBuyukparaInContainer(container); linkifyBareDomains(container); } catch (e) {}
  try {
    container.querySelectorAll(':scope > .src-row').forEach(el => el.remove());
    if (container.dataset && container.dataset.sources) {
      const srcRow = buildSourceRow(JSON.parse(container.dataset.sources));
      if (srcRow) container.insertBefore(srcRow, container.querySelector(':scope > .msg-time'));
    }
  } catch (e) {}
  // Kod blokları: renklendir + başlık çubuğu + kopyala
  container.querySelectorAll('pre > code').forEach(codeEl => {
    const preEl = codeEl.parentElement;
    if (!preEl || preEl.closest('.code-block-wrapper')) return;
    let lang = '';
    const langClass = [...codeEl.classList].find(c => c.startsWith('language-'));
    if (langClass) lang = langClass.replace('language-', '');
    if (window.hljs) {
      try { hljs.highlightElement(codeEl); } catch (e) {}
    }
    const wrapper = document.createElement('div');
    wrapper.className = 'code-block-wrapper';
    const header = document.createElement('div');
    header.className = 'code-block-header';
    const label = document.createElement('span');
    label.className = 'code-lang-label';
    label.textContent = lang || 'metin';
    header.appendChild(label);
    header.appendChild(makeCopyBtn(() => codeEl.textContent));
    preEl.parentElement.insertBefore(wrapper, preEl);
    wrapper.appendChild(header);
    wrapper.appendChild(preEl);
  });
  addSpeakButtonToBubble(container);
}

// ============================================
// SESLİ OKUMA (Web Speech API - speechSynthesis)
// ============================================
// ============================================
// DOĞAL SES: sunucudaki /tts (Microsoft neural kadın sesi). Olmazsa tarayıcı sesine döner.
// ============================================
const NeuralTTS = (function(){
  let audio = null;
  let unlocked = false;
  let disabledUntil = 0;
  let curUrl = null;
  let runToken = 0;
  let playing = false;
  const SILENT = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';

  function ensure(){
    if (!audio) { audio = new Audio(); audio.preload = 'auto'; }
    return audio;
  }
  function available(){
    return typeof Audio !== 'undefined' && Date.now() >= disabledUntil;
  }
  function unlock(){
    if (unlocked || typeof Audio === 'undefined') return;
    try {
      const a = ensure();
      a.src = SILENT;
      const p = a.play();
      if (p && p.then) p.then(function(){ unlocked = true; }).catch(function(){});
    } catch (e) {}
  }
  function fetchBlob(text){
    const ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    const tm = setTimeout(function(){ if (ctrl) ctrl.abort(); }, 30000);
    return fetch('/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text }),
      signal: ctrl ? ctrl.signal : undefined
    }).then(function(r){
      if (!r.ok) throw new Error('tts ' + r.status);
      return r.blob();
    }).then(function(b){
      clearTimeout(tm);
      if (!b || !b.size) throw new Error('bos ses');
      return b;
    }).catch(function(e){ clearTimeout(tm); throw e; });
  }
  function split(text){
    const sents = (text.match(/[^.!?…]+[.!?…]*/g) || [text]).map(function(s){ return s.trim(); }).filter(Boolean);
    const out = [];
    let cur = '';
    for (let k = 0; k < sents.length; k++) {
      let s = sents[k];
      const limit = out.length === 0 ? 170 : 340;
      if (cur && (cur.length + 1 + s.length) > limit) { out.push(cur); cur = ''; }
      while (s.length > 600) {
        let cut = s.lastIndexOf(' ', 600);
        if (cut < 200) cut = 600;
        if (cur) { out.push(cur); cur = ''; }
        out.push(s.slice(0, cut).trim());
        s = s.slice(cut).trim();
      }
      if (s) cur = cur ? cur + ' ' + s : s;
    }
    if (cur) out.push(cur);
    const res = [];
    let total = 0;
    for (let k = 0; k < out.length; k++) {
      total += out[k].length;
      if (total > 5000) { res.push('Devamını ekranda okuyabilirsin.'); break; }
      res.push(out[k]);
    }
    return res;
  }
  function stop(){
    runToken++;
    playing = false;
    if (audio) {
      audio.onended = null;
      audio.onerror = null;
      try { audio.pause(); } catch (e) {}
    }
  }
  function speakChunks(chunks, hooks){
    hooks = hooks || {};
    stop();
    const my = ++runToken;
    if (!chunks || !chunks.length) { if (hooks.onend) hooks.onend(); return; }
    const a = ensure();
    const cache = {};
    let idx = 0;
    let started = false;
    playing = true;
    function get(i){
      if (!cache[i]) cache[i] = fetchBlob(chunks[i]);
      return cache[i];
    }
    function fail(){
      if (my !== runToken) return;
      runToken++;
      playing = false;
      if (!started) {
        disabledUntil = Date.now() + 60000;
        if (hooks.onfail) hooks.onfail();
      } else if (hooks.onend) {
        hooks.onend();
      }
    }
    function playNext(){
      if (my !== runToken) return;
      if (idx >= chunks.length) { playing = false; if (hooks.onend) hooks.onend(); return; }
      const i = idx++;
      get(i).then(function(blob){
        if (my !== runToken) return;
        if (i + 1 < chunks.length) get(i + 1).catch(function(){});
        const url = URL.createObjectURL(blob);
        if (curUrl) { try { URL.revokeObjectURL(curUrl); } catch (e) {} }
        curUrl = url;
        a.onended = function(){ playNext(); };
        a.onerror = function(){ fail(); };
        a.src = url;
        const p = a.play();
        if (p && p.catch) p.catch(function(){ fail(); });
        started = true;
      }).catch(function(){ fail(); });
    }
    playNext();
  }
  function isPlaying(){ return playing; }
  return { available: available, unlock: unlock, split: split, speakChunks: speakChunks, stop: stop, isPlaying: isPlaying };
})();

let ttsCurrentBtn = null;
let ttsVoicesCache = null;

function ttsSupported() {
  return typeof window !== 'undefined' && 'speechSynthesis' in window && typeof window.SpeechSynthesisUtterance !== 'undefined';
}

function getTurkishVoice() {
  if (!ttsSupported()) return null;
  try {
    const voices = window.speechSynthesis.getVoices();
    if (voices && voices.length) {
      ttsVoicesCache = voices;
      return voices.find(v => v.lang && v.lang.toLowerCase().startsWith('tr')) || null;
    }
  } catch (e) {}
  return null;
}

if (ttsSupported()) {
  // Bazı tarayıcılarda (özellikle Chrome) ses listesi asenkron yükleniyor;
  // ilk cagrida bos donebilir, hazir oldugunda cache'i tazeliyoruz.
  try {
    window.speechSynthesis.addEventListener('voiceschanged', () => { ttsVoicesCache = window.speechSynthesis.getVoices(); });
  } catch (e) {}
}

function extractSpeakableText(bubbleEl) {
  if (!bubbleEl) return '';
  const clone = bubbleEl.cloneNode(true);
  clone.querySelectorAll('.msg-time, .code-block-header, .block-copy-btn, .src-row').forEach(el => el.remove());
  return (clone.textContent || '').replace(/\\s+/g, ' ').trim();
}

function resetSpeakBtnIcon(btn) {
  if (!btn) return;
  btn.innerHTML = ICON_SPEAK;
  btn.classList.remove('speaking');
  btn.title = 'Sesli oku';
}

function stopSpeaking() {
  NeuralTTS.stop();
  if (ttsSupported()) {
    try { window.speechSynthesis.cancel(); } catch (e) {}
  }
  if (ttsCurrentBtn) resetSpeakBtnIcon(ttsCurrentBtn);
  ttsCurrentBtn = null;
}

function toggleSpeak(btn, bubbleEl) {
  if (!NeuralTTS.available()) { toggleSpeakBrowser(btn, bubbleEl); return; }
  if (ttsCurrentBtn === btn && NeuralTTS.isPlaying()) { stopSpeaking(); return; }
  if (ttsCurrentBtn && ttsCurrentBtn !== btn) stopSpeaking();
  const text = extractSpeakableText(bubbleEl);
  if (!text) return;
  try { if (ttsSupported()) window.speechSynthesis.cancel(); } catch (e) {}
  NeuralTTS.unlock();
  btn.innerHTML = ICON_SPEAK_STOP;
  btn.classList.add('speaking');
  btn.title = 'Okumayı durdur';
  ttsCurrentBtn = btn;
  const reset = function(){ resetSpeakBtnIcon(btn); if (ttsCurrentBtn === btn) ttsCurrentBtn = null; };
  NeuralTTS.speakChunks(NeuralTTS.split(text), {
    onend: reset,
    onfail: function(){ reset(); toggleSpeakBrowser(btn, bubbleEl); }
  });
}

function toggleSpeakBrowser(btn, bubbleEl) {
  if (!ttsSupported()) {
    alert('Bu tarayıcı sesli okumayı desteklemiyor.');
    return;
  }
  // Ayni butona tekrar tiklandiysa: durdur.
  if (ttsCurrentBtn === btn && window.speechSynthesis.speaking) {
    stopSpeaking();
    return;
  }
  // Baska bir mesaj okunuyorsa once onu durdur (aynı anda sadece bir okuma olur).
  if (ttsCurrentBtn && ttsCurrentBtn !== btn) {
    stopSpeaking();
  }

  const text = extractSpeakableText(bubbleEl);
  if (!text) return;

  try { window.speechSynthesis.cancel(); } catch (e) {}

  const utter = new SpeechSynthesisUtterance(text);
  utter.lang = 'tr-TR';
  const trVoice = getTurkishVoice();
  if (trVoice) utter.voice = trVoice;
  utter.rate = 1.0;
  utter.pitch = 1.0;

  utter.onend = () => { resetSpeakBtnIcon(btn); if (ttsCurrentBtn === btn) ttsCurrentBtn = null; };
  utter.onerror = () => { resetSpeakBtnIcon(btn); if (ttsCurrentBtn === btn) ttsCurrentBtn = null; };

  btn.innerHTML = ICON_SPEAK_STOP;
  btn.classList.add('speaking');
  btn.title = 'Okumayı durdur';
  ttsCurrentBtn = btn;

  window.speechSynthesis.speak(utter);
}

function addSpeakButtonToBubble(bubbleEl) {
  if (!ttsSupported() || !bubbleEl) return;
  const group = bubbleEl.closest('.msg-group');
  if (!group) return;
  const meta = group.querySelector('.msg-meta');
  if (!meta || meta.querySelector('.speak-btn')) return;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'speak-btn';
  btn.title = 'Sesli oku';
  btn.innerHTML = ICON_SPEAK;
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    toggleSpeak(btn, bubbleEl);
  });
  meta.appendChild(btn);
}

function loadChatsFromStorage() {
  try {
    const saved = localStorage.getItem('ai_chats');
    if (saved) {
      const parsed = JSON.parse(saved);
      if (parsed && parsed.length) {
        // Eski surumden kalma sohbetlerin id'si sayisal olabilir (ör. 0, 1, 2...).
        // Hepsini STRING'e normalize ediyoruz ki artik string olan yeni UUID
        // id'lerle ve HTML data-id niteligiyle karsilastirmalar (===) hep tutarli calissin.
        chats = parsed.map(c => ({ ...c, id: String(c.id) }));
        return true;
      }
    }
  } catch (e) {}
  return false;
}

function saveChatsToStorage() {
  try {
    localStorage.setItem('ai_chats', JSON.stringify(chats));
  } catch (e) {}
}

function createChat(title = 'Yeni Sohbet') {
  const chat = new Chat(title);
  chats.push(chat);
  saveChatsToStorage();
  return chat;
}

if (!loadChatsFromStorage()) {
  createChat('Yeni Sohbet');
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function renderChatItemHtml(c, iconSvg) {
  return `<div class="sidebar-item ${c.id === currentChatId ? 'active' : ''} ${c.pinned ? 'pinned-item' : ''}" data-id="${c.id}">
    <span class="icon">${iconSvg}</span> ${escapeHtml(c.title)}
  </div>`;
}

function renderSidebar() {
  const list = document.getElementById('sidebarList');
  if (!list) return;

  const now = new Date();
  const today = now.toDateString();
  const weekAgo = new Date(now);
  weekAgo.setDate(weekAgo.getDate() - 7);

  let html = '';

  // "Yeni Sohbet" butonu (her zaman en üstte)
  html += `<div class="sidebar-group-label">Sohbetler</div>`;
  const isNewActive = chats.some(c => c.id === currentChatId && c.title === 'Yeni Sohbet' && c.messages.length === 0);
  html += `<div class="sidebar-item new-chat ${isNewActive ? 'active' : ''}" onclick="newChat()">
    <span class="icon">${ICON_PLUS}</span> Yeni Sohbet
  </div>`;

  // Diğer sohbetler (Yeni Sohbet haricindekiler veya mesajı olanlar)
  const otherChats = chats.filter(c => c.title !== 'Yeni Sohbet' || c.messages.length > 0);

  if (otherChats.length > 0) {
    // Sabitlenenler (tarihten bağımsız, her zaman en üstte)
    const pinnedChats = otherChats.filter(c => c.pinned);
    if (pinnedChats.length) {
      html += '<div class="sidebar-group-label">Sabitlenenler</div>';
      pinnedChats.forEach(c => { html += renderChatItemHtml(c, ICON_PIN_FILLED); });
    }

    const unpinnedChats = otherChats.filter(c => !c.pinned);

    // Bugün
    const todayChats = unpinnedChats.filter(c => new Date(c.createdAt).toDateString() === today);
    if (todayChats.length) {
      html += '<div class="sidebar-group-label">Bugün</div>';
      todayChats.forEach(c => { html += renderChatItemHtml(c, ICON_CHAT); });
    }

    // Bu Hafta
    const weekChats = unpinnedChats.filter(c => {
      const date = new Date(c.createdAt);
      return date.toDateString() !== today && date > weekAgo;
    });
    if (weekChats.length) {
      html += '<div class="sidebar-group-label">Bu Hafta</div>';
      weekChats.forEach(c => { html += renderChatItemHtml(c, ICON_CHAT); });
    }

    // Geçen Hafta
    const olderChats = unpinnedChats.filter(c => {
      const date = new Date(c.createdAt);
      return date <= weekAgo;
    });
    if (olderChats.length) {
      html += '<div class="sidebar-group-label">Geçen Hafta</div>';
      olderChats.forEach(c => { html += renderChatItemHtml(c, ICON_CHAT); });
    }
  }

  list.innerHTML = html;

  // Sidebar item'larına tıklama + uzun basma (bağlam menüsü) olaylarını ata
  const LONG_PRESS_MS = 480;
  list.querySelectorAll('.sidebar-item[data-id]').forEach(el => {
    let pressTimer = null;
    let longPressFired = false;

    const startPress = () => {
      longPressFired = false;
      clearTimeout(pressTimer);
      pressTimer = setTimeout(() => {
        longPressFired = true;
        if (navigator.vibrate) { try { navigator.vibrate(15); } catch (e) {} }
        openChatMenu(el.dataset.id);
      }, LONG_PRESS_MS);
    };
    const cancelPress = () => clearTimeout(pressTimer);

    el.addEventListener('touchstart', startPress, { passive: true });
    el.addEventListener('touchend', cancelPress);
    el.addEventListener('touchmove', cancelPress);
    el.addEventListener('touchcancel', cancelPress);
    el.addEventListener('mousedown', startPress);
    el.addEventListener('mouseup', cancelPress);
    el.addEventListener('mouseleave', cancelPress);
    el.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      openChatMenu(el.dataset.id);
    });

    el.addEventListener('click', function (e) {
      if (longPressFired) { longPressFired = false; return; }
      const id = this.dataset.id;
      switchChat(id);
    });
  });
}

// ============================================
// DİNAMİK KARŞILAMA BAŞLIĞI (saat bazlı selamlama + rastgele profesyonel alt metin)
// ============================================
const WELCOME_USER_NAME = 'Emre';
function getWelcomeTimeBlock() {
  const h = new Date().getHours();
  if (h >= 5 && h < 12) return 'morning';
  if (h >= 12 && h < 18) return 'afternoon';
  if (h >= 18 && h < 22) return 'evening';
  return 'night';
}
function getWelcomeGreeting() {
  const block = getWelcomeTimeBlock();
  if (block === 'morning') return 'Günaydın';
  if (block === 'afternoon') return 'İyi günler';
  if (block === 'evening') return 'İyi akşamlar';
  return 'İyi geceler';
}
function getWelcomeTitle() {
  return `${getWelcomeGreeting()}, ${WELCOME_USER_NAME}.`;
}
function getWelcomeSubtitle() {
  return pickWelcomeSubtitle().text;
}

// ---- Hava durumuna gore karsilama cumleleri ----
var WELCOME_WX = null;                       // ust bar hava verisi (renderWx doldurur)
var WX_MAX_AGE_MS = 30 * 60 * 1000;          // 30 dakikadan eski veriden cumle uretilmez
var WX_LOOKAHEAD_MS = 16 * 3600 * 1000;      // yaklasan degisim icin bakilan sure
var WX_PRECIP = { drizzle: 1, rain: 1, shower: 1, sleet: 1, snow: 1, thunder: 1 };
var WX_PRECIP_NAME = {
  drizzle: 'çisenti',
  rain: 'yağmur',
  shower: 'sağanak yağış',
  sleet: 'karla karışık yağmur',
  snow: 'kar yağışı',
  thunder: 'gök gürültülü fırtına'
};
var WX_RANK = { none: 0, cached: 1, live: 2 };

function wxPick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
function wxEsc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function wxCap(s) {
  s = String(s || '');
  return s.charAt(0).toLocaleUpperCase('tr-TR') + s.slice(1);
}

// WMO hava kodunu sade bir kategoriye cevirir.
function wxCategory(code) {
  if (code === null || code === undefined) return null;
  code = Number(code);
  if (!isFinite(code)) return null;
  if (code === 0 || code === 1) return 'clear';
  if (code === 2) return 'partly';
  if (code === 3) return 'cloudy';
  if (code === 45 || code === 48) return 'fog';
  if (code >= 51 && code <= 55) return 'drizzle';
  if (code === 56 || code === 57 || code === 66 || code === 67) return 'sleet';
  if (code === 61 || code === 63) return 'rain';
  if (code === 65 || (code >= 80 && code <= 82)) return 'shower';
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return 'snow';
  if (code >= 95 && code <= 99) return 'thunder';
  return null;
}

// Gecerli (taze) hava verisi ya da null.
function wxFreshData() {
  var d = WELCOME_WX;
  if (!d || !d.wx || typeof d.wx.t !== 'number') return null;
  if (!d._at || (Date.now() - d._at) > WX_MAX_AGE_MS) return null;
  return d;
}
function getWelcomeWxState() {
  var d = wxFreshData();
  if (!d) return 'none';
  return d._cached ? 'cached' : 'live';
}

// "Orhangazi" -> "Orhangazi'de", "Bursa" -> "Bursa'da", "Kars" -> "Kars'ta"
function trLocative(name) {
  name = String(name || '').trim();
  if (!name) return '';
  var lower = name.toLocaleLowerCase('tr-TR');
  var back = 'aıou', front = 'eiöü', vowel = '', i;
  for (i = lower.length - 1; i >= 0; i--) {
    if ((back + front).indexOf(lower.charAt(i)) !== -1) { vowel = lower.charAt(i); break; }
  }
  if (!vowel) return '';
  var a = back.indexOf(vowel) !== -1 ? 'a' : 'e';
  var hard = 'çfhkpsşt'.indexOf(lower.charAt(lower.length - 1)) !== -1;
  return name + "'" + (hard ? 't' : 'd') + a;
}

// Cumle icinde tire isareti kullanmamak icin eksi sicakliklar "eksi 2°C" diye yazilir.
function wxTempText(t) {
  var r = Math.round(t);
  if (r === 0) r = 0;
  return r < 0 ? 'eksi ' + Math.abs(r) + '°C' : r + '°C';
}

// Anlik durum cumleleri (her kategori icin birkac farkli cumle)
function buildWxCurrent(d) {
  var w = d.wx;
  var cat = wxCategory(w.code);
  if (!cat) return [];
  var n = WELCOME_USER_NAME;
  var t = wxTempText(w.t);
  var f = '';
  if (typeof w.feels === 'number' && Math.abs(Math.round(w.feels) - Math.round(w.t)) >= 3) {
    f = ', hissedilen ' + wxTempText(w.feels);
  }
  var tf = t + f;
  var loc = trLocative(d._wxPlace);
  var wh = loc ? loc + ' ' : '';
  var day = w.day !== false;

  switch (cat) {
    case 'clear':
      if (day) {
        return [
          n + ', ' + wh + 'şu an hava güneşli, ' + tf + '. Güneşin tadını çıkar.',
          wh + 'gökyüzü açık ve güneşli, ' + t + '. Dışarı çıkmak için güzel bir an.',
          'Güneş açık, ' + t + '. Güneş gözlüğün işine yarar.',
          n + ', hava şu an güneşli, ' + tf + '. Güzel bir gün seni bekliyor.'
        ];
      }
      return [
        n + ', ' + wh + 'şu an gökyüzü açık, ' + tf + '.',
        wh + 'hava açık ve sakin, ' + t + '. Huzurlu bir saat.',
        'Gökyüzü açık, ' + t + '. Yıldızları görmek için iyi bir zaman olabilir.'
      ];
    case 'partly':
      return [
        n + ', ' + wh + 'şu an hava parçalı bulutlu, ' + tf + '.',
        'Gökyüzünde dağınık bulutlar var, ' + t + '. Hava dengeli görünüyor.',
        wh + 'hava az bulutlu, ' + t + '. Dışarısı keyifli görünüyor.'
      ];
    case 'cloudy':
      return [
        n + ', ' + wh + 'şu an hava kapalı, ' + tf + '.',
        'Gökyüzü bulutlarla kaplı, ' + t + '. Sakin ve gri bir hava.',
        wh + 'hava bulutlu, ' + t + '. Bir fincan kahve iyi gider.'
      ];
    case 'fog':
      return [
        n + ', ' + wh + 'şu an hava sisli, ' + tf + '. Yolda görüş mesafesine dikkat.',
        'Sis var, ' + t + '. Sürüş yapacaksan temkinli ol.',
        'Dışarısı sisli, ' + t + '. Araç kullanırken farları açmayı unutma.'
      ];
    case 'drizzle':
      return [
        n + ', ' + wh + 'şu an çiseliyor, ' + tf + '. Yanına bir şemsiye al.',
        'İnce bir çisenti var, ' + t + '. Hafif bir yağmurluk işini görür.',
        wh + 'hafif çisenti var, ' + t + '. Şemsiye bulundurmakta fayda var.'
      ];
    case 'rain':
      return [
        n + ', ' + wh + 'şu an yağmur yağıyor, ' + tf + '. Şemsiyeni unutma.',
        'Hava yağmurlu, ' + t + '. Çıkarken şemsiye almayı unutma.',
        'Dışarıda yağmur var, ' + t + '. Yollar kayganlaşmış olabilir, dikkatli ol.',
        wh + 'yağmur yağıyor, ' + t + '. Sıcak bir çay tam zamanı.'
      ];
    case 'shower':
      return [
        n + ', ' + wh + 'şu an sağanak yağış var, ' + tf + '. Mümkünse biraz bekle.',
        'Sağanak yağmur yağıyor, ' + t + '. Şemsiye yetmeyebilir, yağmurluk daha iyi.',
        wh + 'hava sağanak yağışlı, ' + t + '. Yola çıkacaksan dikkatli ol.'
      ];
    case 'snow':
      return [
        n + ', ' + wh + 'şu an kar yağıyor, ' + tf + '. Sıcak giyin.',
        'Kar yağışı var, ' + t + '. Yollar kaygan olabilir, dikkatli ol.',
        'Dışarıda kar yağıyor, ' + t + '. Eldivenini ve atkını unutma.',
        wh + 'kar yağıyor, ' + t + '. Manzara güzel, ama dışarı çıkarken ayağına dikkat.'
      ];
    case 'sleet':
      return [
        n + ', ' + wh + 'şu an karla karışık yağmur yağıyor, ' + tf + '. Şemsiye ve sıcak bir mont iyi olur.',
        'Karla karışık yağmur var, ' + t + '. Yollar buzlanabilir, dikkatli ol.',
        wh + 'hava karla karışık yağmurlu, ' + t + '. Su geçirmez ayakkabı giymek akıllıca olur.'
      ];
    case 'thunder':
      return [
        n + ', ' + wh + 'şu an gök gürültülü fırtına var, ' + tf + '. Mümkünse içeride kal.',
        'Hava fırtınalı, gök gürlüyor, ' + t + '. Açık alanlardan ve ağaç altından uzak dur.',
        wh + 'gök gürültülü yağış var, ' + t + '. Yola çıkmadan önce biraz bekle.'
      ];
  }
  return [];
}

function wxDayKey(dt) { return dt.getFullYear() * 10000 + dt.getMonth() * 100 + dt.getDate(); }

// "Akşama doğru", "Gece", "Yarın sabah" gibi zaman ifadesi
function wxWhenText(ms, now, nowD) {
  if (ms - now <= 90 * 60 * 1000) return 'Kısa süre içinde';
  var dt = new Date(ms);
  var h = dt.getHours();
  if (h >= 22 || h < 5) return 'Gece';
  var part = h < 12 ? 'sabah' : (h < 18 ? 'öğleden sonra' : 'akşam');
  if (wxDayKey(dt) !== wxDayKey(nowD)) return 'Yarın ' + part;
  if (part === 'sabah') return 'Sabah saatlerinde';
  if (part === 'akşam') return 'Akşama doğru';
  return 'Öğleden sonra';
}

// Oncelik katmanlari: 1 kritik uyari, 2 yaklasan yagis, 3 anlik durum, 4 diger bilgiler.
var WX_P_CRIT = 1, WX_P_UPCOMING = 2, WX_P_CURRENT = 3, WX_P_OTHER = 4;
function wxItem(p, s) { return { p: p, s: s }; }

// Yaklasan degisim uyarilari (saatlik tahminden)
function buildWxAlerts(d) {
  var out = [];
  var w = d.wx;
  var nowD = new Date();
  var now = nowD.getTime();
  var src = Array.isArray(w.hourly) ? w.hourly : [];
  var list = [];
  var i, k;
  for (i = 0; i < src.length; i++) {
    if (src[i] && typeof src[i].ts === 'number') list.push(src[i]);
  }
  list.sort(function (a, b) { return a.ts - b.ts; });

  // 1) Yagis baslangici ya da bitisi (onumuzdeki 16 saat)
  var fut = list.filter(function (e) {
    var ms = e.ts * 1000;
    return ms > now && ms <= now + WX_LOOKAHEAD_MS;
  });
  var curCat = wxCategory(w.code);
  var raining = !!(curCat && WX_PRECIP[curCat]);
  if (!raining) {
    for (k = 0; k < fut.length; k++) {
      var c = wxCategory(fut[k].code);
      if (c && WX_PRECIP[c] && (typeof fut[k].pp !== 'number' || fut[k].pp >= 40)) {
        out.push(wxItem(c === 'thunder' ? WX_P_CRIT : WX_P_UPCOMING, wxWhenText(fut[k].ts * 1000, now, nowD) + ' ' + WX_PRECIP_NAME[c] + ' bekleniyor.'));
        break;
      }
    }
  } else {
    for (k = 0; k + 1 < fut.length; k++) {
      var c1 = wxCategory(fut[k].code), c2 = wxCategory(fut[k + 1].code);
      if (c1 && c2 && !WX_PRECIP[c1] && !WX_PRECIP[c2]) {
        out.push(wxItem(WX_P_UPCOMING, wxWhenText(fut[k].ts * 1000, now, nowD) + ' yağışın dinmesi bekleniyor.'));
        break;
      }
    }
  }

  // 2) Yarin ile bugun arasindaki sicaklik farki (en az 5 derece)
  var tmD = new Date(nowD.getFullYear(), nowD.getMonth(), nowD.getDate() + 1);
  var todayKey = wxDayKey(nowD), tomKey = wxDayKey(tmD);
  var days = {};
  for (i = 0; i < list.length; i++) {
    var e = list[i];
    if (typeof e.t !== 'number') continue;
    var dt = new Date(e.ts * 1000);
    var key = wxDayKey(dt), h = dt.getHours();
    var b = days[key];
    if (!b) { b = { max: e.t, mid: false }; days[key] = b; }
    if (e.t > b.max) b.max = e.t;
    if (h >= 12 && h <= 16) b.mid = true;
  }
  var bt = days[todayKey], bm = days[tomKey];
  if (bt && bm && bt.mid && bm.mid) {
    var diff = Math.round(bm.max) - Math.round(bt.max);
    var ad = Math.abs(diff);
    if (diff <= -5) {
      out.push(wxItem(WX_P_OTHER, wxPick([
        'Yarın bugünden ' + ad + ' derece soğuk.',
        'Yarın hava bugüne göre ' + ad + ' derece daha soğuk olacak, hazırlıklı ol.'
      ])));
    } else if (diff >= 5) {
      out.push(wxItem(WX_P_OTHER, wxPick([
        'Yarın bugünden ' + ad + ' derece sıcak.',
        'Yarın hava bugüne göre ' + ad + ' derece daha sıcak olacak.'
      ])));
    }
  }
  return out;
}

// ---- Hissedilen sicaklik, ruzgar, don, asiri sicak, UV ----
function wxCurUV(w, now) {
  var h = Array.isArray(w.hourly) ? w.hourly : [];
  var best = null, bd = 90 * 60 * 1000, i, e, dd;
  for (i = 0; i < h.length; i++) {
    e = h[i];
    if (!e || typeof e.ts !== 'number' || typeof e.uv !== 'number') continue;
    dd = Math.abs(e.ts * 1000 - now);
    if (dd <= bd) { bd = dd; best = e.uv; }
  }
  return best;
}

function buildWxConditions(d) {
  var w = d.wx, out = [], i, e;
  var nowD = new Date();
  var now = nowD.getTime();
  var t = w.t;
  var hasFeels = typeof w.feels === 'number';
  var eff = hasFeels ? w.feels : t;
  var ld = (hasFeels ? 'hissedilen ' : 'sıcaklık ') + wxTempText(eff);

  // Soguk (hissedilen sicakliga gore)
  if (eff <= -10) {
    out.push(wxItem(WX_P_CRIT, ld + ', çok sert bir soğuk. Dışarı çıkacaksan kat kat ve kalın giyin.'));
    out.push(wxItem(WX_P_CRIT, ld + ', dondurucu soğuk. Eldiven, atkı ve bere şart.'));
  } else if (eff <= 0) {
    out.push(wxItem(WX_P_OTHER, ld + ', kalın giyin. Eldiven ve atkı iyi olur.'));
    out.push(wxItem(WX_P_OTHER, ld + ', hava çok soğuk. Kalın bir mont şart.'));
  } else if (eff <= 10) {
    out.push(wxItem(WX_P_OTHER, ld + ', kalın giyin.'));
    out.push(wxItem(WX_P_OTHER, ld + ', hava serin. Üstüne kalın bir şey al.'));
  }

  // Asiri sicak
  if (eff >= 35) {
    out.push(wxItem(WX_P_CRIT, 'aşırı sıcak, ' + ld + '. Bol su iç, öğle saatlerinde gölgede kal.'));
    out.push(wxItem(WX_P_CRIT, 'hava aşırı sıcak, ' + ld + '. Mümkünse öğle sıcağında dışarı çıkma.'));
  } else if (eff >= 30) {
    out.push(wxItem(WX_P_OTHER, 'hava çok sıcak, ' + ld + '. Bol su iç ve gölgede dinlen.'));
  }

  // Ruzgar
  if (typeof w.wind === 'number') {
    var wk = Math.round(w.wind);
    if (wk >= 60) {
      out.push(wxItem(WX_P_CRIT, 'rüzgâr çok şiddetli, saatte yaklaşık ' + wk + ' km. Mümkünse dışarı çıkma.'));
      out.push(wxItem(WX_P_CRIT, 'şiddetli rüzgâr var, saatte yaklaşık ' + wk + ' km. Hafif eşyaları içeri al.'));
    } else if (wk >= 40) {
      out.push(wxItem(WX_P_OTHER, 'rüzgâr sert, dışarı çıkacaksan dikkat.'));
      out.push(wxItem(WX_P_OTHER, 'rüzgâr sert esiyor, saatte yaklaşık ' + wk + ' km. Balkondaki hafif eşyalara dikkat.'));
      out.push(wxItem(WX_P_OTHER, 'sert rüzgâr var, çıkarken üstüne rüzgâr geçirmeyen bir şey al.'));
    }
  }

  // Don riski (su an ve onumuzdeki 12 saat)
  var list = Array.isArray(w.hourly) ? w.hourly : [];
  var minT = t, minTs = now;
  for (i = 0; i < list.length; i++) {
    e = list[i];
    if (!e || typeof e.ts !== 'number' || typeof e.t !== 'number') continue;
    var ms = e.ts * 1000;
    if (ms > now && ms <= now + 12 * 3600 * 1000 && e.t < minT) { minT = e.t; minTs = ms; }
  }
  if (t <= 0) {
    out.push(wxItem(WX_P_CRIT, 'hava şu an donma noktasında, yollar ve zemin buzlanabilir.'));
    out.push(wxItem(WX_P_CRIT, 'sıcaklık sıfırın altında, buzlanmaya dikkat. Araç kullanacaksan temkinli ol.'));
  } else if (minT <= 0) {
    out.push(wxItem(WX_P_CRIT, wxWhenText(minTs, now, nowD) + ' don bekleniyor, sıcaklık ' + wxTempText(minT) + ' civarına düşecek.'));
  } else if (minT <= 3) {
    if (minTs === now) {
      out.push(wxItem(WX_P_OTHER, 'şu an don riski var, sıcaklık ' + wxTempText(minT) + '. Yollar ve zemin kaygan olabilir.'));
    } else {
      out.push(wxItem(WX_P_OTHER, wxWhenText(minTs, now, nowD) + ' don riski var, sıcaklık ' + wxTempText(minT) + ' civarına düşecek.'));
    }
  }

  // Yuksek UV (sadece gunduz)
  var uv = wxCurUV(w, now);
  if (uv !== null && w.day !== false) {
    var u = Math.round(uv);
    if (uv >= 11) {
      out.push(wxItem(WX_P_CRIT, 'UV indeksi ' + u + ', aşırı yüksek. Öğle saatlerinde güneşten uzak dur, güneş kremi şart.'));
    } else if (uv >= 8) {
      out.push(wxItem(WX_P_OTHER, 'UV indeksi ' + u + ', çok yüksek. Güneş kremi sür, öğle saatlerinde gölgede kal.'));
    } else if (uv >= 6) {
      out.push(wxItem(WX_P_OTHER, 'UV indeksi ' + u + ', yüksek. Güneş kremi ve güneş gözlüğü iyi olur.'));
      out.push(wxItem(WX_P_OTHER, 'UV yüksek (' + u + '). Uzun süre güneşte kalacaksan güneş kremi sür.'));
    }
  }
  return out;
}

// ---- Gun dogumu ve batimi ----
var WX_UNIT_SFX = ['da', 'de', 'de', 'te', 'te', 'te', 'da', 'de', 'de', 'da'];
var WX_TENS_SFX = ['', 'da', 'de', 'da', 'ta', 'de'];
// Sayinin okunusundaki son kelimeye gore bulunma eki (21 "bir" -> de, 40 "kırk" -> ta)
function wxNumSfx(n) {
  var u = n % 10;
  if (u !== 0 || n === 0) return WX_UNIT_SFX[u];
  return WX_TENS_SFX[n / 10];
}
function wxHM(ms) {
  var dt = new Date(ms);
  var h = dt.getHours(), m = dt.getMinutes();
  return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
}
// "18:21'de", "18:00'de", "06:40'ta"
function wxTimeLoc(ms) {
  var dt = new Date(ms);
  var h = dt.getHours(), m = dt.getMinutes();
  return wxHM(ms) + "'" + wxNumSfx(m === 0 ? h : m);
}
function wxDurText(mins) {
  if (mins <= 5) return 'birkaç dakika';
  var r = Math.round(mins / 5) * 5;
  if (r < 60) return 'yaklaşık ' + r + ' dakika';
  var h = Math.floor(r / 60), rem = r % 60;
  return 'yaklaşık ' + h + ' saat' + (rem ? ' ' + rem + ' dakika' : '');
}

function buildWxSun(d) {
  var sun = d.wx.sun;
  if (!sun) return [];
  var now = Date.now();
  var MIN = 60 * 1000;
  function toMs(arr) {
    var o = [], i;
    if (!Array.isArray(arr)) return o;
    for (i = 0; i < arr.length; i++) {
      if (typeof arr[i] === 'number' && isFinite(arr[i])) o.push(arr[i] * 1000);
    }
    return o;
  }
  var rises = toMs(sun.rise), sets = toMs(sun.set);
  var nextSet = null, lastSet = null, nextRise = null, lastRise = null, i;
  for (i = 0; i < sets.length; i++) {
    if (sets[i] > now) { if (nextSet === null || sets[i] < nextSet) nextSet = sets[i]; }
    else if (lastSet === null || sets[i] > lastSet) lastSet = sets[i];
  }
  for (i = 0; i < rises.length; i++) {
    if (rises[i] > now) { if (nextRise === null || rises[i] < nextRise) nextRise = rises[i]; }
    else if (lastRise === null || rises[i] > lastRise) lastRise = rises[i];
  }
  var out = [], dur;
  if (nextSet !== null && nextSet - now <= 120 * MIN) {
    dur = wxDurText((nextSet - now) / MIN);
    out.push(wxItem(WX_P_OTHER, 'güneş ' + wxTimeLoc(nextSet) + ' batıyor, ' + dur + ' kaldı.'));
    out.push(wxItem(WX_P_OTHER, 'gün batımına ' + dur + ' var, güneş ' + wxTimeLoc(nextSet) + ' batıyor.'));
  } else if (lastSet !== null && now - lastSet <= 45 * MIN) {
    out.push(wxItem(WX_P_OTHER, 'güneş ' + wxTimeLoc(lastSet) + ' battı.'));
  }
  if (nextRise !== null && nextRise - now <= 120 * MIN) {
    dur = wxDurText((nextRise - now) / MIN);
    out.push(wxItem(WX_P_OTHER, 'güneş ' + wxTimeLoc(nextRise) + ' doğuyor, ' + dur + ' kaldı.'));
    out.push(wxItem(WX_P_OTHER, 'gün doğumuna ' + dur + ' var, güneş ' + wxTimeLoc(nextRise) + ' doğuyor.'));
  } else if (lastRise !== null && now - lastRise <= 60 * MIN) {
    out.push(wxItem(WX_P_OTHER, 'güneş ' + wxTimeLoc(lastRise) + ' doğdu.'));
  }
  return out;
}

// Alt metin havuzuna eklenecek hava cumleleri: oncelik katmanina gore gruplanir.
// Donus: { 1: [...], 2: [...], 3: [...], 4: [...] } (hazir HTML metinleri)
function buildWxTiers() {
  var d = wxFreshData();
  var tiers = { 1: [], 2: [], 3: [], 4: [] };
  if (!d) return tiers;
  var items = [], cur = [], i, it, p;
  try {
    cur = buildWxCurrent(d);
    for (i = 0; i < cur.length; i++) {
      items.push(wxItem(wxCategory(d.wx.code) === 'thunder' ? WX_P_CRIT : WX_P_CURRENT, cur[i]));
    }
  } catch (e) {}
  try { items = items.concat(buildWxAlerts(d)); } catch (e) {}
  try { items = items.concat(buildWxConditions(d)); } catch (e) {}
  try { items = items.concat(buildWxSun(d)); } catch (e) {}
  for (i = 0; i < items.length; i++) {
    it = items[i];
    if (!it || typeof it.s !== 'string' || !it.s) continue;
    p = (it.p >= 1 && it.p <= 4) ? it.p : WX_P_OTHER;
    tiers[p].push(wxEsc(wxCap(it.s)));
  }
  return tiers;
}

// Geriye uyumluluk: tum cumleler tek listede (oncelik sirasiyla).
function buildWxSentences() {
  var t = buildWxTiers();
  return t[1].concat(t[2], t[3], t[4]);
}

// Dolu olan en yuksek oncelikli katmandan rastgele bir cumle secer.
// Sira: kritik uyari, yaklasan yagis, anlik durum, diger bilgiler.
// Hava verisi yoksa alt metin bos kalir (eski cumleler kaldirildi).
function pickWelcomeSubtitle() {
  var t = null, p, list;
  try { t = buildWxTiers(); } catch (e) {}
  if (t) {
    for (p = 1; p <= 4; p++) {
      list = t[p];
      if (list && list.length) return { text: list[Math.floor(Math.random() * list.length)], wx: true, tier: p };
    }
  }
  return { text: '', wx: false, tier: 9 };
}
function welcomeSubtitleHTML() {
  var st = getWelcomeWxState();
  var p = pickWelcomeSubtitle();
  return '<div class="welcome-subtitle" data-wx="' + st + '" data-wxs="' + (p.wx ? 1 : 0) + '" data-wxp="' + p.tier + '" style="min-height:1.5em">' + p.text + '</div>';
}
// Hava verisi karsilama ekranindan sonra gelirse alt metni (gerekirse) hava cumlesiyle gunceller.
function refreshWelcomeWx() {
  try {
    var el = document.querySelector('#welcomeContainer .welcome-subtitle');
    if (!el) return;
    refreshWelcomeIcon();
    var was = el.getAttribute('data-wx') || 'none';
    var now = getWelcomeWxState();
    if (WX_RANK[now] <= WX_RANK[was]) return;
    el.setAttribute('data-wx', now);
    var p = pickWelcomeSubtitle();
    if (!p.wx) return;
    if (el.getAttribute('data-wxs') === '1') {
      // Zaten bir hava cumlesi var: yalnizca yeni veri daha yuksek oncelikliyse degistir.
      var oldTier = parseInt(el.getAttribute('data-wxp'), 10);
      if (!isFinite(oldTier) || p.tier >= oldTier) return;
    }
    el.innerHTML = p.text;
    el.setAttribute('data-wxs', '1');
    el.setAttribute('data-wxp', String(p.tier));
  } catch (e) {}
}

// Saate gore karsilama ikonu (satir ici SVG; renk/boyut .welcome-icon CSS'inden gelir).
const WELCOME_ICONS = {
  morning: '<svg viewBox="0 0 120 120" aria-hidden="true"><defs><radialGradient id="wiMg" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#FFE0A0" stop-opacity=".65"/><stop offset="1" stop-color="#FFE0A0" stop-opacity="0"/></radialGradient><radialGradient id="wiMs" cx="40%" cy="35%" r="75%"><stop offset="0" stop-color="#FFF1BC"/><stop offset=".6" stop-color="#FFC456"/><stop offset="1" stop-color="#F59A45"/></radialGradient><clipPath id="wiMc"><rect x="0" y="0" width="120" height="80"/></clipPath></defs><circle class="wi-glow" cx="60" cy="80" r="54" fill="url(#wiMg)"/><g clip-path="url(#wiMc)"><g class="wi-rise"><g class="wi-spin wi-spin-h" style="transform-origin:60px 80px"><g stroke="#FFC456" stroke-width="3.4" stroke-linecap="round"><line x1="60" y1="51" x2="60" y2="37"/><line class="wi-ray" style="animation-delay:0.2s" x1="74.5" y1="54.9" x2="78.5" y2="48"/><line x1="85.1" y1="65.5" x2="97.2" y2="58.5"/><line class="wi-ray" style="animation-delay:0.8s" x1="89" y1="80" x2="97" y2="80"/><line x1="85.1" y1="94.5" x2="97.2" y2="101.5"/><line class="wi-ray" style="animation-delay:1.2s" x1="74.5" y1="105.1" x2="78.5" y2="112"/><line x1="60" y1="109" x2="60" y2="123"/><line class="wi-ray" style="animation-delay:1.8s" x1="45.5" y1="105.1" x2="41.5" y2="112"/><line x1="34.9" y1="94.5" x2="22.8" y2="101.5"/><line class="wi-ray" style="animation-delay:2.2s" x1="31" y1="80" x2="23" y2="80"/><line x1="34.9" y1="65.5" x2="22.8" y2="58.5"/><line class="wi-ray" style="animation-delay:2.8s" x1="45.5" y1="54.9" x2="41.5" y2="48"/></g></g><circle cx="60" cy="80" r="22" fill="url(#wiMs)"/></g></g><path d="M12 80 H108" stroke="#E8B48A" stroke-width="2.6" stroke-linecap="round" fill="none"/><path class="wi-shimmer" d="M30 88 H90" stroke="#E8B48A" stroke-width="2.2" stroke-linecap="round" fill="none" opacity=".5"/><path class="wi-shimmer" style="animation-delay:.9s" d="M44 95 H76" stroke="#E8B48A" stroke-width="2" stroke-linecap="round" fill="none" opacity=".35"/><g class="wi-cloud" opacity=".45" fill="#F6E3D3"><g transform="translate(84 38) scale(1)"><ellipse cx="0" cy="0" rx="12" ry="4.5"/><ellipse cx="-8" cy="3" rx="8" ry="3.5"/><ellipse cx="9" cy="3" rx="7" ry="3"/></g></g><g class="wi-cloud wi-cloud-b" opacity=".35" fill="#F6E3D3"><g transform="translate(26 52) scale(0.7)"><ellipse cx="0" cy="0" rx="12" ry="4.5"/><ellipse cx="-8" cy="3" rx="8" ry="3.5"/><ellipse cx="9" cy="3" rx="7" ry="3"/></g></g></svg>',
  afternoon: '<svg viewBox="0 0 120 120" aria-hidden="true"><defs><radialGradient id="wiAg" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#FFE9A8" stop-opacity=".6"/><stop offset="1" stop-color="#FFB45E" stop-opacity="0"/></radialGradient><radialGradient id="wiAs" cx="38%" cy="34%" r="72%"><stop offset="0" stop-color="#FFF4C2"/><stop offset=".55" stop-color="#FFC84F"/><stop offset="1" stop-color="#F28A35"/></radialGradient></defs><circle class="wi-glow" cx="60" cy="60" r="58" fill="url(#wiAg)"/><g class="wi-spin" style="transform-origin:60px 60px"><g stroke="#FFC84F" stroke-width="3.6" stroke-linecap="round"><line x1="60" y1="30" x2="60" y2="15"/><line class="wi-ray" style="animation-delay:0.2s" x1="75" y1="34" x2="79" y2="27.1"/><line x1="86" y1="45" x2="99" y2="37.5"/><line class="wi-ray" style="animation-delay:0.8s" x1="90" y1="60" x2="98" y2="60"/><line x1="86" y1="75" x2="99" y2="82.5"/><line class="wi-ray" style="animation-delay:1.2s" x1="75" y1="86" x2="79" y2="92.9"/><line x1="60" y1="90" x2="60" y2="105"/><line class="wi-ray" style="animation-delay:1.8s" x1="45" y1="86" x2="41" y2="92.9"/><line x1="34" y1="75" x2="21" y2="82.5"/><line class="wi-ray" style="animation-delay:2.2s" x1="30" y1="60" x2="22" y2="60"/><line x1="34" y1="45" x2="21" y2="37.5"/><line class="wi-ray" style="animation-delay:2.8s" x1="45" y1="34" x2="41" y2="27.1"/></g></g><circle cx="60" cy="60" r="21" fill="url(#wiAs)"/><ellipse cx="53" cy="52" rx="7" ry="4.5" fill="#fff" opacity=".35" transform="rotate(-30 53 52)"/></svg>',
  evening: '<svg viewBox="0 0 120 120" aria-hidden="true"><defs><radialGradient id="wiEg" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#FF8FA0" stop-opacity=".65"/><stop offset="1" stop-color="#FF8FA0" stop-opacity="0"/></radialGradient><radialGradient id="wiEs" cx="40%" cy="35%" r="75%"><stop offset="0" stop-color="#FFD08A"/><stop offset=".6" stop-color="#FF8A5E"/><stop offset="1" stop-color="#E5516F"/></radialGradient><clipPath id="wiEc"><rect x="0" y="0" width="120" height="82"/></clipPath></defs><circle class="wi-glow" cx="60" cy="82" r="54" fill="url(#wiEg)"/><g clip-path="url(#wiEc)"><g class="wi-sink"><g class="wi-spin wi-spin-h" style="transform-origin:60px 82px"><g stroke="#FF9A70" stroke-width="3.4" stroke-linecap="round"><line x1="60" y1="53" x2="60" y2="39"/><line class="wi-ray" style="animation-delay:0.2s" x1="74.5" y1="56.9" x2="78.5" y2="50"/><line x1="85.1" y1="67.5" x2="97.2" y2="60.5"/><line class="wi-ray" style="animation-delay:0.8s" x1="89" y1="82" x2="97" y2="82"/><line x1="85.1" y1="96.5" x2="97.2" y2="103.5"/><line class="wi-ray" style="animation-delay:1.2s" x1="74.5" y1="107.1" x2="78.5" y2="114"/><line x1="60" y1="111" x2="60" y2="125"/><line class="wi-ray" style="animation-delay:1.8s" x1="45.5" y1="107.1" x2="41.5" y2="114"/><line x1="34.9" y1="96.5" x2="22.8" y2="103.5"/><line class="wi-ray" style="animation-delay:2.2s" x1="31" y1="82" x2="23" y2="82"/><line x1="34.9" y1="67.5" x2="22.8" y2="60.5"/><line class="wi-ray" style="animation-delay:2.8s" x1="45.5" y1="56.9" x2="41.5" y2="50"/></g></g><circle cx="60" cy="82" r="22" fill="url(#wiEs)"/></g></g><path d="M12 82 H108" stroke="#E98A93" stroke-width="2.6" stroke-linecap="round" fill="none"/><path class="wi-shimmer" d="M30 90 H90" stroke="#E98A93" stroke-width="2.2" stroke-linecap="round" fill="none" opacity=".5"/><path class="wi-shimmer" style="animation-delay:.9s" d="M44 97 H76" stroke="#E98A93" stroke-width="2" stroke-linecap="round" fill="none" opacity=".35"/><g transform="translate(30 26) scale(0.8)"><path class="wi-twinkle" style="animation-delay:0s" fill="#FFE7C2" d="M0 -6 L1.6 -1.6 L6 0 L1.6 1.6 L0 6 L-1.6 1.6 L-6 0 L-1.6 -1.6Z"/></g><g transform="translate(94 22) scale(1)"><path class="wi-twinkle" style="animation-delay:0.9s" fill="#FFE7C2" d="M0 -6 L1.6 -1.6 L6 0 L1.6 1.6 L0 6 L-1.6 1.6 L-6 0 L-1.6 -1.6Z"/></g><g transform="translate(104 48) scale(0.6)"><path class="wi-twinkle" style="animation-delay:1.6s" fill="#FFE7C2" d="M0 -6 L1.6 -1.6 L6 0 L1.6 1.6 L0 6 L-1.6 1.6 L-6 0 L-1.6 -1.6Z"/></g><g class="wi-cloud" opacity=".3" fill="#F6E3D3"><g transform="translate(80 44) scale(0.8)"><ellipse cx="0" cy="0" rx="12" ry="4.5"/><ellipse cx="-8" cy="3" rx="8" ry="3.5"/><ellipse cx="9" cy="3" rx="7" ry="3"/></g></g></svg>',
  night: '<svg viewBox="0 0 120 120" aria-hidden="true"><defs><radialGradient id="wiNg" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#FFF1C9" stop-opacity=".5"/><stop offset="1" stop-color="#FFE6A8" stop-opacity="0"/></radialGradient><linearGradient id="wiNm" x1="15%" y1="10%" x2="85%" y2="90%"><stop offset="0" stop-color="#FFF8E1"/><stop offset="1" stop-color="#EBC987"/></linearGradient><mask id="wiNk" maskUnits="userSpaceOnUse" x="0" y="0" width="120" height="120"><rect width="120" height="120" fill="#fff"/><circle cx="73" cy="50" r="27" fill="#000"/></mask></defs><g transform="translate(88 30) scale(1.1)"><path class="wi-twinkle" style="animation-delay:0s" fill="#FFF3C9" d="M0 -6 L1.6 -1.6 L6 0 L1.6 1.6 L0 6 L-1.6 1.6 L-6 0 L-1.6 -1.6Z"/></g><g transform="translate(102 58) scale(0.7)"><path class="wi-twinkle" style="animation-delay:1.1s" fill="#FFF3C9" d="M0 -6 L1.6 -1.6 L6 0 L1.6 1.6 L0 6 L-1.6 1.6 L-6 0 L-1.6 -1.6Z"/></g><g transform="translate(72 14) scale(0.6)"><path class="wi-twinkle" style="animation-delay:0.5s" fill="#FFF3C9" d="M0 -6 L1.6 -1.6 L6 0 L1.6 1.6 L0 6 L-1.6 1.6 L-6 0 L-1.6 -1.6Z"/></g><g transform="translate(20 30) scale(0.65)"><path class="wi-twinkle" style="animation-delay:1.6s" fill="#FFF3C9" d="M0 -6 L1.6 -1.6 L6 0 L1.6 1.6 L0 6 L-1.6 1.6 L-6 0 L-1.6 -1.6Z"/></g><g transform="translate(98 92) scale(0.6)"><path class="wi-twinkle" style="animation-delay:2.1s" fill="#FFF3C9" d="M0 -6 L1.6 -1.6 L6 0 L1.6 1.6 L0 6 L-1.6 1.6 L-6 0 L-1.6 -1.6Z"/></g><g transform="translate(24 96) scale(0.55)"><path class="wi-twinkle" style="animation-delay:0.8s" fill="#FFF3C9" d="M0 -6 L1.6 -1.6 L6 0 L1.6 1.6 L0 6 L-1.6 1.6 L-6 0 L-1.6 -1.6Z"/></g><g class="wi-float"><circle class="wi-glow" cx="52" cy="66" r="46" fill="url(#wiNg)"/><circle cx="52" cy="66" r="30" fill="url(#wiNm)" mask="url(#wiNk)"/></g><line class="wi-shoot" x1="26" y1="12" x2="38" y2="19" stroke="#FFF8E1" stroke-width="2" stroke-linecap="round"/></svg>'
};
// ---- Hava durumuna gore ikonlar ----
// Bulut sekli: 60x42 kutu (merkez 30,24). fill ve ince alt golge rengi parametre.
function wiCloud(x, y, sc, fill, shade, cls, style) {
  var body = '<rect x="2" y="28" width="56" height="14" rx="7"/><circle cx="18" cy="28" r="11"/><circle cx="32" cy="20" r="15"/><circle cx="47" cy="28" r="11"/>';
  return '<g transform="translate(' + x + ' ' + y + ') scale(' + sc + ')"><g' + (cls ? ' class="' + cls + '"' : '') + (style ? ' style="' + style + '"' : '') + '>' +
    '<g fill="' + shade + '" transform="translate(0 2.5)">' + body + '</g><g fill="' + fill + '">' + body + '</g>' +
    '<ellipse cx="30" cy="14" rx="9" ry="4" fill="#fff" opacity=".35"/></g></g>';
}
// Yagmur damlasi (x,y mutlak; gecikme saniye)
function wiDrop(x, y, delay, cls, color, len) {
  return '<g transform="translate(' + x + ' ' + y + ')"><g class="wi-drop ' + (cls || '') + '" style="animation-delay:' + delay + 's"><line x1="0" y1="0" x2="-2.2" y2="' + (len || 9) + '" stroke="' + (color || '#8CC8FF') + '" stroke-width="2.6" stroke-linecap="round"/></g></g>';
}
function wiFlake(x, y, delay, color) {
  return '<g transform="translate(' + x + ' ' + y + ')"><g class="wi-flake" style="animation-delay:' + delay + 's"><path d="M0 -4.2V4.2M-3.6 -2.1L3.6 2.1M-3.6 2.1L3.6 -2.1" stroke="' + (color || '#F2F8FF') + '" stroke-width="1.5" stroke-linecap="round" fill="none"/></g></g>';
}
function wiBolt(x, y, delay) {
  return '<g transform="translate(' + x + ' ' + y + ')"><g class="wi-flash" style="animation-delay:' + delay + 's"><path d="M5 0L-6 17H0L-4 32L10 11H3.5L9 0Z" fill="#FFD84A" stroke="#FFF3B0" stroke-width=".8" stroke-linejoin="round"/></g></g>';
}
function wiSunSmall(cx, cy) {
  var r = '', k;
  for (k = 0; k < 8; k++) r += '<line x1="0" y1="-21" x2="0" y2="-27" transform="rotate(' + (k * 45) + ')" stroke="#FFC84F" stroke-width="3" stroke-linecap="round"/>';
  return '<g transform="translate(' + cx + ' ' + cy + ')"><circle class="wi-glow" r="34" fill="url(#wxSg)"/><g class="wi-spin" style="transform-box:fill-box;transform-origin:center">' + r + '</g><circle r="16" fill="url(#wxSs)"/></g>';
}
function wiMoonSmall(cx, cy) {
  return '<g transform="translate(' + cx + ' ' + cy + ')"><circle class="wi-glow" r="32" fill="url(#wxMg)"/>' +
    '<circle r="17" fill="url(#wxMm)" mask="url(#wxMk)"/>' +
    '<g transform="translate(22 -20) scale(.8)"><path class="wi-twinkle" fill="#FFF3C9" d="M0 -6 L1.6 -1.6 L6 0 L1.6 1.6 L0 6 L-1.6 1.6 L-6 0 L-1.6 -1.6Z"/></g></g>';
}
var WX_ICON_DEFS =
  '<defs>' +
  '<radialGradient id="wxSg" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#FFE9A8" stop-opacity=".6"/><stop offset="1" stop-color="#FFB45E" stop-opacity="0"/></radialGradient>' +
  '<radialGradient id="wxSs" cx="38%" cy="34%" r="72%"><stop offset="0" stop-color="#FFF4C2"/><stop offset=".55" stop-color="#FFC84F"/><stop offset="1" stop-color="#FF9F3C"/></radialGradient>' +
  '<radialGradient id="wxMg" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#FFF1C9" stop-opacity=".5"/><stop offset="1" stop-color="#FFE6A8" stop-opacity="0"/></radialGradient>' +
  '<linearGradient id="wxMm" x1="15%" y1="10%" x2="85%" y2="90%"><stop offset="0" stop-color="#FFF8E1"/><stop offset="1" stop-color="#EBC987"/></linearGradient>' +
  '<mask id="wxMk" maskUnits="userSpaceOnUse" x="-40" y="-40" width="80" height="80"><rect x="-40" y="-40" width="80" height="80" fill="#fff"/><circle cx="9" cy="-6" r="15" fill="#000"/></mask>' +
  '</defs>';
function wiWrap(inner) {
  return '<svg viewBox="0 0 120 120" aria-hidden="true">' + WX_ICON_DEFS + inner + '</svg>';
}
// night: gece ise bulut renkleri biraz koyulasir.
function wiTone(night, light, dark) {
  return night ? dark : light;
}
var WX_ICON_BUILD = {
  partly: function (n) {
    var f = wiTone(n, '#F4F7FB', '#C9D2E0'), sh = wiTone(n, '#C6D2E2', '#8F9BB2');
    return wiWrap('<g class="wi-bob">' + (n ? wiMoonSmall(76, 40) : wiSunSmall(76, 40)) + '</g>' + wiCloud(18, 50, 1.25, f, sh, 'wi-cloud'));
  },
  cloudy: function (n) {
    var f = wiTone(n, '#E8EDF4', '#B9C3D3'), sh = wiTone(n, '#BCC8D8', '#8190A6');
    var f2 = wiTone(n, '#CBD5E2', '#98A4B8'), sh2 = wiTone(n, '#A5B3C6', '#6F7C92');
    return wiWrap(wiCloud(44, 24, 1.0, f2, sh2, 'wi-cloud wi-cloud-b') + wiCloud(14, 44, 1.4, f, sh, 'wi-cloud'));
  },
  fog: function (n) {
    var f = wiTone(n, '#E6ECF3', '#B4BECF'), sh = wiTone(n, '#BAC6D6', '#8190A6'), m = wiTone(n, '#D5DEE9', '#A3AEC1');
    var lines = '';
    lines += '<g transform="translate(22 78)"><rect class="wi-mist" style="animation-delay:0s" width="64" height="5" rx="2.5" fill="' + m + '"/></g>';
    lines += '<g transform="translate(32 89)"><rect class="wi-mist wi-mist-b" style="animation-delay:.6s" width="62" height="5" rx="2.5" fill="' + m + '"/></g>';
    lines += '<g transform="translate(16 100)"><rect class="wi-mist" style="animation-delay:1.2s" width="58" height="5" rx="2.5" fill="' + m + '"/></g>';
    return wiWrap(wiCloud(17, 20, 1.45, f, sh, 'wi-cloud') + lines);
  },
  drizzle: function (n) {
    var f = wiTone(n, '#D5DDE8', '#A3AEC1'), sh = wiTone(n, '#A9B6C8', '#7A869C');
    var d = wiDrop(38, 76, 0, 'wi-drop-s', '#A9D6FF', 6) + wiDrop(58, 80, .8, 'wi-drop-s', '#A9D6FF', 6) + wiDrop(78, 76, 1.5, 'wi-drop-s', '#A9D6FF', 6) + wiDrop(48, 78, 1.9, 'wi-drop-s', '#A9D6FF', 6);
    return wiWrap(d + wiCloud(18, 24, 1.4, f, sh, 'wi-cloud'));
  },
  rain: function (n) {
    var f = wiTone(n, '#BAC5D4', '#8C98AC'), sh = wiTone(n, '#8E9BB0', '#66728A');
    var d = wiDrop(36, 76, 0, '', '#7DBEFF') + wiDrop(52, 78, .5, '', '#7DBEFF') + wiDrop(68, 76, .25, '', '#7DBEFF') + wiDrop(84, 78, .75, '', '#7DBEFF');
    return wiWrap(d + wiCloud(18, 24, 1.4, f, sh, 'wi-cloud'));
  },
  shower: function (n) {
    var f = wiTone(n, '#8F9CB1', '#6B7790'), sh = wiTone(n, '#69768D', '#4D586E');
    var d = wiDrop(30, 74, 0, 'wi-drop-f', '#6FB4FF', 11) + wiDrop(44, 77, .3, 'wi-drop-f', '#6FB4FF', 11) + wiDrop(58, 74, .6, 'wi-drop-f', '#6FB4FF', 11) + wiDrop(72, 77, .15, 'wi-drop-f', '#6FB4FF', 11) + wiDrop(86, 74, .45, 'wi-drop-f', '#6FB4FF', 11);
    return wiWrap(d + wiCloud(18, 22, 1.4, f, sh, 'wi-cloud'));
  },
  sleet: function (n) {
    var f = wiTone(n, '#B4BFCF', '#8793A8'), sh = wiTone(n, '#8A97AB', '#626E85');
    var d = wiDrop(36, 76, 0, '', '#7DBEFF') + wiFlake(52, 80, .6) + wiDrop(68, 76, .3, '', '#7DBEFF') + wiFlake(84, 80, 1.2);
    return wiWrap(d + wiCloud(18, 24, 1.4, f, sh, 'wi-cloud'));
  },
  snow: function (n) {
    var f = wiTone(n, '#E2E9F2', '#B1BBCD'), sh = wiTone(n, '#B5C2D4', '#8190A6');
    var d = wiFlake(34, 76, 0) + wiFlake(50, 80, .9) + wiFlake(66, 76, 1.8) + wiFlake(82, 80, .4) + wiFlake(58, 78, 2.6);
    return wiWrap(d + wiCloud(18, 24, 1.4, f, sh, 'wi-cloud'));
  },
  thunder: function (n) {
    var f = wiTone(n, '#7E8AA0', '#5E6A82'), sh = wiTone(n, '#5C6880', '#434D63');
    var d = wiDrop(34, 78, .2, '', '#7DBEFF') + wiDrop(88, 76, .7, '', '#7DBEFF');
    return wiWrap(d + wiCloud(18, 20, 1.4, f, sh, 'wi-cloud') + wiBolt(55, 66, 0));
  }
};
// Gecerli hava verisine gore ikon anahtari: 'clear' haric kategori adi + gece bayragi, yoksa saat blogu.
function getWelcomeIconKey() {
  var block = getWelcomeTimeBlock(), d = null, cat = null, night = false;
  try {
    d = wxFreshData();
    if (d) {
      cat = wxCategory(d.wx.code);
      night = (d.wx.day === false) || block === 'night';
    }
  } catch (e) { cat = null; }
  if (cat && typeof WX_ICON_BUILD !== 'undefined' && WX_ICON_BUILD[cat]) return cat + (night ? '|n' : '|d');
  // Acik hava (ya da veri yok): saat blogu ikonu. Veri geceyi soyluyorsa gece ikonu.
  if (cat === 'clear' && night) return 'night';
  return block;
}
function getWelcomeIconSVG() {
  var key = getWelcomeIconKey(), parts = key.split('|');
  try {
    if (typeof WX_ICON_BUILD !== 'undefined' && WX_ICON_BUILD[parts[0]]) return WX_ICON_BUILD[parts[0]](parts[1] === 'n');
  } catch (e) {}
  return WELCOME_ICONS[WELCOME_ICONS[parts[0]] ? parts[0] : getWelcomeTimeBlock()] || WELCOME_ICONS.afternoon;
}
// Hava verisi sonradan gelir ya da degisirse ikonu (yalnizca anahtar degistiyse) yeniler.
function refreshWelcomeIcon() {
  try {
    var box = document.querySelector('#welcomeContainer .welcome-icon');
    if (!box) return;
    var key = getWelcomeIconKey();
    if (box.getAttribute('data-wxi') === key) return;
    box.innerHTML = getWelcomeIconSVG();
    box.setAttribute('data-wxi', key);
  } catch (e) {}
}

function switchChat(id) {
  stopSpeaking();
  currentChatId = id;
  liveModel = null;
  syncModelPill();
  const chat = chats.find(c => c.id === id);
  if (!chat) return;

  const chatArea = document.getElementById('chatArea');
  chatArea.innerHTML = '';
  chatArea.classList.remove('is-empty');

  chat.messages.forEach(msg => {
    const g = document.createElement('div');
    g.className = 'msg-group';
    if (msg.role === 'user') {
      g.className += ' user-side';
      const time = new Date(msg.timestamp || Date.now()).toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' });
      g.innerHTML = `<div class="bubble user-bubble">${msg.content}<div class="msg-time">${time}</div></div>`;
    } else {
      const time = new Date(msg.timestamp || Date.now()).toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' });
      g.innerHTML = `<div class="msg-meta"></div><div class="bubble ai-bubble">${marked.parse(msg.content)}<div class="msg-time">${time}</div></div>`;
      try { g.querySelector('.ai-bubble').dataset.sources = JSON.stringify(Array.isArray(msg.sources) ? msg.sources : []); } catch (e) {}
      enhanceContentBlocks(g.querySelector('.ai-bubble'));
      const footerEl = buildSourcesFooter(msg.sources);
      if (footerEl) g.appendChild(footerEl);
    }
    chatArea.appendChild(g);
  });

  if (chat.messages.length === 0) {
    chatArea.innerHTML = `<div class="welcome-container" id="welcomeContainer">
      <div class="welcome-icon" aria-hidden="true" data-wxi="${getWelcomeIconKey()}">${getWelcomeIconSVG()}</div>
      <div class="pulse-orb">E</div>
      <div class="welcome-title">${getWelcomeTitle()}</div>
      ${welcomeSubtitleHTML()}
    </div>`;
    chatArea.classList.add('is-empty');
  }

  renderSidebar();
  closeSidebar();
}

function newChat() {
  const chat = createChat('Yeni Sohbet');
  currentChatId = chat.id;
  switchChat(chat.id);
}

function deleteChat(id) {
  if (chats.length <= 1) {
    alert('En az bir sohbet kalmalı.');
    return;
  }
  const index = chats.findIndex(c => c.id === id);
  if (index === -1) return;
  
  // Silinen sohbet aktifse başka bir sohbet seç
  if (chats[index].id === currentChatId) {
    const nextIndex = index + 1 < chats.length ? index + 1 : index - 1;
    currentChatId = chats[nextIndex].id;
  }
  
  chats.splice(index, 1);
  saveChatsToStorage();
  renderSidebar();
  switchChat(currentChatId);

  // Sunucudaki bu sohbete ait gecmisi de temizle (fire-and-forget - basarisiz
  // olsa bile kullaniciyi bekletmeye/uyarmaya gerek yok, en kotu ihtimalle
  // kullanilmayan birkac KB veri diskte kalir).
  fetch('/history/delete-chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: id })
  }).catch(() => {});
}

// ============================================
// ÜST BAR: ANA SAYFA (yeni sohbet) VE ÜÇ NOKTA MENÜSÜ
// ============================================
function closeHdrMenu() {
  const m = document.getElementById('hdrMenu');
  const b = document.getElementById('hdrMoreBtn');
  if (m) m.classList.remove('open');
  if (b) b.setAttribute('aria-expanded', 'false');
}

function toggleHdrMenu(ev) {
  if (ev) ev.stopPropagation();
  const m = document.getElementById('hdrMenu');
  const b = document.getElementById('hdrMoreBtn');
  if (!m || !b) return;
  if (m.classList.contains('open')) { closeHdrMenu(); return; }
  const chat = chats.find(c => c.id === currentChatId);
  closeMbMenus();
  // Sohbet bolumu sadece gecerli bir sohbet varsa gosterilir; Ayarlar her durumda erisilebilir.
  const chatSec = document.getElementById('hdrChatSection');
  const sep = document.getElementById('hdrMenuSep');
  if (chatSec) chatSec.style.display = chat ? '' : 'none';
  if (sep) sep.classList.toggle('hidden', !chat);
  if (chat) {
    document.getElementById('hdrMenuTitle').textContent = chat.title;
    document.getElementById('hdrPinLabel').textContent = chat.pinned ? 'Sabitlemeyi kaldır' : 'Sabitle';
    document.getElementById('hdrPinIcon').innerHTML = chat.pinned ? ICON_PIN_FILLED : ICON_PIN;
    document.getElementById('hdrPinItem').classList.toggle('hdr-item-pinned', !!chat.pinned);
    document.getElementById('hdrDeleteItem').disabled = !!streaming;
  }
  m.classList.add('open');
  b.setAttribute('aria-expanded', 'true');
}

function hdrMenuAction(action) {
  closeHdrMenu();
  if (action === 'settings') { closeMbMenus(); openSettings(); return; }
  const chat = chats.find(c => c.id === currentChatId);
  if (!chat) return;
  contextMenuChatId = chat.id;
  if (action === 'pin') togglePinChat();
  else if (action === 'rename') openRenameChat();
  else if (action === 'delete') { if (!streaming) openDeleteChatConfirm(); }
}

// Ana sayfa: bos bir "Yeni Sohbet" varsa ona gecer, yoksa yenisini acar.
function goHome() {
  if (streaming) return;
  closeHdrMenu();
  closeMbMenus();
  if (window.VoiceMode && window.VoiceMode.isActive()) window.VoiceMode.end();
  const cur = chats.find(c => c.id === currentChatId);
  if (cur && cur.messages.length === 0) { closeSidebar(); return; }
  const empty = chats.find(c => c.messages.length === 0 && c.title === 'Yeni Sohbet');
  if (empty) { currentChatId = empty.id; switchChat(empty.id); }
  else newChat();
}

document.addEventListener('click', function (e) {
  if (!e.target.closest('.hdr-menu-wrap')) closeHdrMenu();
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') closeHdrMenu();
});

// ============================================
// ÜST BAR: CANLI TARİH + OTOMATİK KONUM VE SICAKLIK
// ============================================
(function () {
  var MONTHS = ['Ocak','Şubat','Mart','Nisan','Mayıs','Haziran','Temmuz','Ağustos','Eylül','Ekim','Kasım','Aralık'];
  var DAYS = ['Pazar','Pazartesi','Salı','Çarşamba','Perşembe','Cuma','Cumartesi'];
  var CACHE_KEY = 'ai_hdr_wx_v2';   // v2: eski (yanlis "Hessen" gibi) onbellek sonuclari atilir
  var REFRESH_MS = 10 * 60 * 1000;   // konum + sicaklik 10 dakikada bir tazelenir
  var RETRY_MS = 60 * 1000;          // basarisizsa en erken 1 dakika sonra tekrar dener
  var lastDateKey = '', lastOk = 0, lastTry = 0, fetching = false, shown = false;

  function $(id) { return document.getElementById(id); }

  // ---- Tarih (gece yarisi kendiliginden degisir) ----
  function renderDate() {
    try {
      var el = $('hdrClockDate');
      if (!el) return;
      var d = new Date();
      var key = d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
      if (key === lastDateKey) return;
      lastDateKey = key;
      el.innerHTML = d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear() +
        '<span class="dot"></span><span class="wd">' + DAYS[d.getDay()] + '</span>';
    } catch (e) {}
  }

  // ---- Konum + sicaklik gosterimi ----
  function fmtTemp(t) {
    if (t === null || t === undefined || isNaN(t)) return '';
    var r = Math.round(t);
    if (r === 0) r = 0;
    return (r < 0 ? '\u2212' + Math.abs(r) : '' + r) + '\u00B0C';
  }
  // Yer adlarini Turkce kurallarla "Orhangazı", "Bursa" biciminde (sadece bas harf buyuk) yazar.
  function titleTr(s) {
    return String(s || '').split(' ').map(function (w) {
      return w.split('-').map(function (p) {
        if (!p) return p;
        return p.charAt(0).toLocaleUpperCase('tr-TR') + p.slice(1).toLocaleLowerCase('tr-TR');
      }).join('-');
    }).join(' ');
  }
  function placeText(d) {
    var parts = [];
    if (d.district) parts.push(titleTr(d.district));
    if (d.city && d.city !== d.district) parts.push(titleTr(d.city));
    return parts.join(', ');
  }
  function renderWx(d) {
    try {
      d._wxPlace = titleTr(d.district || d.city || '');
      window.WELCOME_WX = d;
      refreshWelcomeWx();
    } catch (e) {}
    try {
      var box = $('hdrWx'), pEl = $('hdrWxPlace'), tEl = $('hdrWxTemp'), dot = $('hdrWxDot');
      if (!box || !pEl || !tEl || !dot) return;
      var place = placeText(d), temp = fmtTemp(d.temp);
      pEl.textContent = place || 'Konum';
      tEl.textContent = temp;
      dot.style.display = (place && temp) ? '' : 'none';
      box.classList.remove('is-loading');
      box.title = (place ? place : '') + (temp ? ' · ' + temp : '');
      shown = true;
    } catch (e) {}
  }
  function showMsg(msg) {
    var box = $('hdrWx'), pEl = $('hdrWxPlace'), tEl = $('hdrWxTemp'), dot = $('hdrWxDot');
    if (!box || !pEl) return;
    pEl.textContent = msg;
    if (tEl) tEl.textContent = '';
    if (dot) dot.style.display = 'none';
    box.classList.remove('is-loading');
  }
  function saveCache(d) {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify({ t: Date.now(), d: d })); } catch (e) {}
  }
  function loadCache() {
    try {
      var raw = localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      var o = JSON.parse(raw);
      if (!o || !o.d || Date.now() - o.t > 6 * 3600 * 1000) return null;
      o.d._at = o.t;
      o.d._cached = true;
      return o.d;
    } catch (e) { return null; }
  }

  // ---- Tarayici (GPS) konumu: once yuksek hassasiyet, olmazsa dusuk hassasiyet, o da olmazsa son bilinen konum ----
  var POS_KEY = 'ai_hdr_pos_v1';
  function savePos(p) {
    try { localStorage.setItem(POS_KEY, JSON.stringify({ t: Date.now(), lat: p.lat, lon: p.lon })); } catch (e) {}
  }
  function loadPos() {
    try {
      var o = JSON.parse(localStorage.getItem(POS_KEY) || 'null');
      if (!o || Date.now() - o.t > 24 * 3600 * 1000) return null;
      return { lat: o.lat, lon: o.lon };
    } catch (e) { return null; }
  }
  function getPos(opts, ms) {
    return new Promise(function (resolve) {
      var done = false;
      var timer = setTimeout(function () { if (!done) { done = true; resolve(null); } }, ms + 1500);
      try {
        navigator.geolocation.getCurrentPosition(
          function (p) { if (done) return; done = true; clearTimeout(timer); resolve({ lat: p.coords.latitude, lon: p.coords.longitude }); },
          function () { if (done) return; done = true; clearTimeout(timer); resolve(null); },
          opts
        );
      } catch (e) { done = true; clearTimeout(timer); resolve(null); }
    });
  }
  async function locate() {
    if (!navigator.geolocation || window.isSecureContext === false) return loadPos();
    var p = await getPos({ enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }, 15000);
    if (!p) p = await getPos({ enableHighAccuracy: false, timeout: 10000, maximumAge: 10 * 60 * 1000 }, 10000);
    if (p) { savePos(p); return p; }
    return loadPos();
  }

  function numOrNull(v) { return (typeof v === 'number' && isFinite(v)) ? v : null; }
  function parseOmWx(j) {
    var c = j && j.current;
    if (!c || typeof c.temperature_2m !== 'number' || !isFinite(c.temperature_2m)) return null;
    var h = j.hourly || {};
    var times = h.time || [], temps = h.temperature_2m || [], codes = h.weather_code || [], pps = h.precipitation_probability || [], uvs = h.uv_index || [];
    var hrs = [];
    for (var i = 0; i < times.length; i++) {
      var ts = Number(times[i]);
      if (!isFinite(ts)) continue;
      hrs.push({ ts: ts, t: numOrNull(temps[i]), code: numOrNull(codes[i]), pp: numOrNull(pps[i]), uv: numOrNull(uvs[i]) });
    }
    var dl = j.daily || {};
    var sun = { rise: [], set: [] };
    (dl.sunrise || []).forEach(function (v) { var x = Number(v); if (v !== null && isFinite(x)) sun.rise.push(x); });
    (dl.sunset || []).forEach(function (v) { var x = Number(v); if (v !== null && isFinite(x)) sun.set.push(x); });
    return {
      t: Math.round(c.temperature_2m * 10) / 10,
      feels: numOrNull(c.apparent_temperature),
      code: numOrNull(c.weather_code),
      day: c.is_day !== 0,
      wind: numOrNull(c.wind_speed_10m),
      hourly: hrs,
      sun: sun,
      src: 'om'
    };
  }
  async function clientWx(pos) {
    try {
      var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      var to = setTimeout(function () { if (ctrl) ctrl.abort(); }, 8000);
      var r = await fetch('https://api.open-meteo.com/v1/forecast?latitude=' + pos.lat.toFixed(4) +
        '&longitude=' + pos.lon.toFixed(4) +
        '&current=temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m' +
        '&hourly=temperature_2m,weather_code,precipitation_probability,uv_index' +
        '&daily=sunrise,sunset' +
        '&forecast_days=2&timeformat=unixtime&timezone=auto',
        { cache: 'no-store', signal: ctrl ? ctrl.signal : undefined });
      clearTimeout(to);
      return parseOmWx(await r.json());
    } catch (e) { return null; }
  }

  async function refreshWx(force) {
    if (fetching) return;
    var now = Date.now();
    if (!force) {
      if (now - lastOk < REFRESH_MS) return;
      if (now - lastTry < RETRY_MS) return;
    }
    fetching = true;
    lastTry = now;
    try {
      var pos = await locate();
      if (!pos) {
        // Konum alinamadi: yanlis bir yer gostermek yerine (varsa) onceki dogru sonucu koru.
        if (!shown) showMsg('Konum izni gerekli · dokun');
        return;
      }
      var url = '/api/weather?lat=' + pos.lat.toFixed(5) + '&lon=' + pos.lon.toFixed(5);
      var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      var to = setTimeout(function () { if (ctrl) ctrl.abort(); }, 15000);
      var res = await fetch(url, { cache: 'no-store', signal: ctrl ? ctrl.signal : undefined });
      clearTimeout(to);
      var d = await res.json();
      if (d && d.ok) {
        lastOk = Date.now();
        // Sunucu hava verisini alamadiysa (ör. Render IP'si engelliyse) telefon kendi baglantisiyla alir.
        if (!d.wx) {
          var cw = await clientWx(pos);
          if (cw) {
            d.wx = cw;
            if (d.temp === null || d.temp === undefined) d.temp = cw.t;
          }
        }
        d._at = Date.now();
        renderWx(d);
        saveCache(d);
      } else if (!shown) {
        showMsg('Konum alınamadı');
      }
    } catch (e) {
      if (!shown) showMsg('Konum alınamadı');
    } finally {
      fetching = false;
    }
  }

  function init() {
    renderDate();
    var c = loadCache();
    if (c) renderWx(c);           // onceki sonuc aninda gorunur, arkada tazelenir
    var wxBox = $('hdrWx');
    if (wxBox) wxBox.addEventListener('click', function () { refreshWx(true); });   // dokununca konumu yeniden dene
    refreshWx(true);
    setInterval(function () { renderDate(); refreshWx(false); }, 20000);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { renderDate(); refreshWx(false); }
    });
    window.addEventListener('online', function () { refreshWx(true); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

// ============================================
// SOHBET BAĞLAM MENÜSÜ (uzun basma: yeniden adlandır / sabitle / sil)
// ============================================
let contextMenuChatId = null;
let deleteChatIdPending = null;

function openChatMenu(id) {
  const chat = chats.find(c => c.id === id);
  if (!chat) return;
  contextMenuChatId = id;
  document.getElementById('chatMenuTitle').textContent = chat.title;
  document.getElementById('chatMenuPinLabel').textContent = chat.pinned ? 'Sabiti Kaldır' : 'Sabitle';
  document.getElementById('chatMenuPinIcon').innerHTML = chat.pinned ? ICON_PIN_FILLED : ICON_PIN;
  document.getElementById('chatMenuOverlay').style.display = 'flex';
}

function closeChatMenu() {
  document.getElementById('chatMenuOverlay').style.display = 'none';
}

function togglePinChat() {
  const chat = chats.find(c => c.id === contextMenuChatId);
  if (!chat) { closeChatMenu(); return; }
  chat.pinned = !chat.pinned;
  saveChatsToStorage();
  renderSidebar();
  closeChatMenu();
}

function openRenameChat() {
  const chat = chats.find(c => c.id === contextMenuChatId);
  if (!chat) return;
  closeChatMenu();
  const input = document.getElementById('renameInput');
  input.value = chat.title;
  document.getElementById('renameOverlay').style.display = 'flex';
  setTimeout(() => { input.focus(); input.select(); }, 60);
}

function closeRenameChat() {
  document.getElementById('renameOverlay').style.display = 'none';
}

function confirmRenameChat() {
  const chat = chats.find(c => c.id === contextMenuChatId);
  if (!chat) { closeRenameChat(); return; }
  const val = document.getElementById('renameInput').value.trim();
  if (val) chat.title = val.slice(0, 60);
  saveChatsToStorage();
  renderSidebar();
  closeRenameChat();
}

function openDeleteChatConfirm() {
  const chat = chats.find(c => c.id === contextMenuChatId);
  if (!chat) return;
  closeChatMenu();
  deleteChatIdPending = contextMenuChatId;
  document.getElementById('deleteChatConfirmDesc').textContent = `"${chat.title}" sohbeti kalıcı olarak silinecek. Bu işlem geri alınamaz.`;
  document.getElementById('deleteChatConfirmOverlay').style.display = 'flex';
}

function closeDeleteChatConfirm() {
  document.getElementById('deleteChatConfirmOverlay').style.display = 'none';
  deleteChatIdPending = null;
}

function confirmDeleteChatFromMenu() {
  const id = deleteChatIdPending;
  document.getElementById('deleteChatConfirmOverlay').style.display = 'none';
  deleteChatIdPending = null;
  if (id != null) {
    if (chats.length <= 1) createChat('Yeni Sohbet');
    deleteChat(id);
  }
}

document.addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && document.getElementById('renameOverlay').style.display === 'flex') {
    confirmRenameChat();
  }
});

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebarOverlay').classList.toggle('open');
}

function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebarOverlay').classList.remove('open');
}

// ============================================
// MODEL SEÇİCİ
// ============================================
const MODEL_LABELS = {
  'openai/gpt-oss-20b': 'GPT-OSS 20B',
  'qwen/qwen3.8-27b': 'Qwen3.8 27B',
  'openai/gpt-oss-120b': 'GPT-OSS 120B'
};
const VISION_MODEL_ID = 'qwen/qwen3.8-27b';

let selModel = null;      // null = Otomatik mod: backend, soruya gore kendi model secer
let autoMode = true;      // kullanici elle bir model secene kadar Otomatik modda kaliriz
let liveModel = null;     // en son cevap veren modelin kimligi (kutuda canli gosterilir)

function modelLabel(id){
  return MODEL_LABELS[id] || id;
}

// Model kutusunun yazisini gunceller.
//  - Baslangic (Otomatik mod, henuz cevap yok): "Otomatik"
//  - Otomatik modda cevap gelince: cevap veren modelin adi + soluk "Otomatik"
//  - Elle secim: secilen modelin adi (cevap baska modelden gelirse o modelin adi)
//  - Gorsel ekliyken: gorseli isleyecek modelin adi + soluk "Görsel"
function syncModelPill(){
  const pill = document.getElementById('modelPill');
  const nameEl = document.getElementById('modelPillName');
  const subEl = document.getElementById('modelPillSub');
  if (!pill || !nameEl || !subEl) return;

  const imgAttached = !!(uploadedFile && String(uploadedFile.type || '').indexOf('image/') === 0);
  let name = 'Otomatik';
  let sub = '';
  if (imgAttached) {
    name = modelLabel(VISION_MODEL_ID);
    sub = 'Görsel';
  } else if (liveModel) {
    name = modelLabel(liveModel);
    sub = autoMode ? 'Otomatik' : '';
  } else if (!autoMode && selModel) {
    name = modelLabel(selModel);
  }
  nameEl.textContent = name;
  subEl.textContent = sub;

  // Menudeki secili satir
  const current = autoMode ? 'auto' : selModel;
  document.querySelectorAll('.mb-model').forEach(function(el){
    el.classList.toggle('selected', el.getAttribute('data-model') === current);
  });

  // Ek bilgi sigmiyorsa (dar telefon ekrani) gizlenir; model adi her zaman okunur kalir.
  pill.classList.remove('no-sub');
  if (pill.scrollWidth > pill.clientWidth + 1) pill.classList.add('no-sub');
}

function selectModel(m){
  if (m === 'auto') {
    autoMode = true;
    selModel = null;
  } else {
    autoMode = false;
    selModel = m;
  }
  liveModel = null;
  syncModelPill();
  closeMbMenus();
}

// Sunucu bir cevap baslattiginda hangi modelin kullanildigini bildirir; kutu anlik olarak o adla degisir.
function onModelAnswered(m){
  liveModel = m || null;
  syncModelPill();
}

// (+) menusu ve model secici menusu
function closeMbMenus(){
  const pm = document.getElementById('plusMenu');
  const mm = document.getElementById('modelMenu');
  const pb = document.getElementById('plusBtn');
  const mp = document.getElementById('modelPill');
  if (pm) pm.classList.remove('open');
  if (mm) mm.classList.remove('open');
  if (pb) { pb.classList.remove('is-open'); pb.setAttribute('aria-expanded', 'false'); }
  if (mp) mp.setAttribute('aria-expanded', 'false');
}

function toggleMbMenu(which, ev){
  if (ev) ev.stopPropagation();
  const menu = document.getElementById(which + 'Menu');
  if (!menu) return;
  const wasOpen = menu.classList.contains('open');
  closeMbMenus();
  if (wasOpen) return;
  menu.classList.add('open');
  if (which === 'plus') {
    const pb = document.getElementById('plusBtn');
    pb.classList.add('is-open');
    pb.setAttribute('aria-expanded', 'true');
  } else {
    document.getElementById('modelPill').setAttribute('aria-expanded', 'true');
  }
}

document.addEventListener('click', function(e){
  if (!e.target.closest('.mb-menu')) closeMbMenus();
});
document.addEventListener('keydown', function(e){
  if (e.key === 'Escape') closeMbMenus();
});
window.addEventListener('resize', function(){ syncModelPill(); });

// ============================================
// INPUT
// ============================================
// Yazi kutusu: bosken tek satir, yazdikca yumusakca buyur.
// Olcum, gorunmez bir ayna textarea uzerinde yapilir; gercek kutunun yuksekligi hic
// 'auto'ya cekilmez, boylece CSS height gecisi (transition) bozulmaz ve gonderdikten
// sonra kutu da yumusakca tek satira geri doner.
(function(){
  const ta = document.getElementById('msgInput');
  const mirror = ta.cloneNode(false);
  mirror.removeAttribute('id');
  mirror.removeAttribute('placeholder');
  mirror.tabIndex = -1;
  mirror.setAttribute('aria-hidden', 'true');
  mirror.style.cssText = 'position:absolute;left:0;top:0;visibility:hidden;pointer-events:none;height:0;min-height:0;max-height:none;overflow:hidden;transition:none;';
  ta.parentNode.appendChild(mirror);

  function fit(){
    const cs = getComputedStyle(ta);
    const lh = parseFloat(cs.lineHeight) || 22;
    const minH = lh + parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    const maxH = Math.max(minH, window.innerHeight * 0.2);
    let h = minH;
    if (ta.value) {
      mirror.style.width = ta.clientWidth + 'px';
      mirror.value = ta.value;
      h = Math.max(minH, mirror.scrollHeight);
    }
    ta.style.overflowY = h > maxH ? 'auto' : 'hidden';
    ta.style.height = Math.min(h, maxH) + 'px';
    if (window.syncSendBtn) window.syncSendBtn();   // buton, yazi kutusuyla ayni anda guncellenir
  }

  ta.addEventListener('input', fit);

  // Yazarken kutu "canli" kalir: her tus vurusunda halka/kenarlik bir tik parlar,
  // yazma durunca ~140ms sonra yavasca (CSS gecisiyle) eski haline doner.
  const wrap = ta.closest('.input-wrapper');
  let typingTimer = null;
  ta.addEventListener('input', function(){
    if (!wrap) return;
    wrap.classList.add('is-typing');
    clearTimeout(typingTimer);
    typingTimer = setTimeout(function(){ wrap.classList.remove('is-typing'); }, 140);
  });
  window.addEventListener('resize', fit);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
  window.autoSizeMsgInput = fit;   // value programatik degistiginde (gonder, mikrofon, temizle) cagrilir
  fit();
})();

// ============================================
// DOSYA İŞLEMLERİ
// ============================================
let uploadedFile = null;

function triggerFile(){
  document.getElementById('fileInput').click();
}
function triggerFileWithAccept(kind){
  const fi = kind === 'pdf' ? document.getElementById('fileInputPdf') : document.getElementById('fileInputImage');
  if(!fi) return;
  fi.click();
}

function handleFile(e){
  const file = e.target.files[0];
  if(!file) return;
  const reader = new FileReader();
  reader.onload = ev => {
    uploadedFile = { name: file.name, type: file.type, data: ev.target.result.split(',')[1] };
    if (window.syncSendBtn) window.syncSendBtn();
    document.getElementById('fileBadgeName').textContent = file.name;
    document.getElementById('fileBadge').style.display = 'flex';
    if(file.type.startsWith('image/')){
      document.getElementById('filePreviewImg').src = ev.target.result;
      document.getElementById('filePreviewImg').style.display = 'block';
    }
    syncModelPill();
  };
  reader.readAsDataURL(file);
}

function removeFile(){
  uploadedFile = null;
  syncModelPill();
  if (window.syncSendBtn) window.syncSendBtn();
  document.getElementById('fileBadge').style.display='none';
  document.getElementById('filePreviewImg').style.display='none';
}

function focusMsgInput(){
  document.getElementById('msgInput').focus();
}

// ============================================
// KAMERA
// ============================================
let currentCameraStream = null;

async function openCamera(){
  if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    alert('Bu cihazda sayfa içi kamera erişimi desteklenmiyor.');
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    currentCameraStream = stream;
    const video = document.getElementById('cameraVideo');
    video.srcObject = stream;
    document.getElementById('cameraOverlay').style.display = 'flex';
  } catch(err) {
    alert('Kamera erişimi alınamadı: ' + err.message);
  }
}
function closeCamera(){
  document.getElementById('cameraOverlay').style.display = 'none';
  if(currentCameraStream){ currentCameraStream.getTracks().forEach(t => t.stop()); currentCameraStream = null; }
}
function captureCameraPhoto(){
  const video = document.getElementById('cameraVideo');
  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth; canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
  uploadedFile = { name: 'kamera_foto.jpg', type: 'image/jpeg', data: dataUrl.split(',')[1] };
  if (window.syncSendBtn) window.syncSendBtn();
  closeCamera();
  syncModelPill();
  document.getElementById('fileBadgeName').textContent = uploadedFile.name;
  document.getElementById('fileBadge').style.display = 'flex';
  document.getElementById('filePreviewImg').src = dataUrl;
  document.getElementById('filePreviewImg').style.display = 'block';
  document.getElementById('msgInput').focus();
}

// ============================================
// MİKROFON
// ============================================
// ============================================
// SESLİ KONUŞMA (Talk): dinler, gönderir, cevabı sesli okur, tekrar dinler
// ============================================
const VoiceMode = (function(){
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const PLACEHOLDERS = { listening: 'Dinliyorum…', thinking: 'Düşünüyorum…', speaking: 'Konuşuyorum…' };
  const NL = String.fromCharCode(10);
  let active = false;
  let starting = false;
  let state = 'idle';
  let runId = 0;
  let sessionId = 0;
  let ttsToken = 0;
  let rec = null;
  let micOk = false;
  let emptyRuns = 0;
  let startRetries = 0;
  let timer = null;
  let basePlaceholder = null;

  function byId(id){ return document.getElementById(id); }

  function setState(s){
    state = s;
    const wrap = byId('inputWrapper');
    const talk = byId('talkBtn');
    const ta = byId('msgInput');
    if (wrap) {
      wrap.classList.toggle('voice-on', active);
      wrap.classList.toggle('voice-listening', active && s === 'listening');
      wrap.classList.toggle('voice-thinking', active && s === 'thinking');
      wrap.classList.toggle('voice-speaking', active && s === 'speaking');
    }
    if (talk) {
      talk.classList.toggle('is-active', active);
      talk.setAttribute('aria-pressed', active ? 'true' : 'false');
      const t = active ? 'Sesli konuşmayı bitir' : 'Sesli konuşmayı başlat';
      talk.title = t;
      talk.setAttribute('aria-label', t);
    }
    if (ta) {
      if (basePlaceholder === null) basePlaceholder = ta.getAttribute('placeholder') || '';
      ta.setAttribute('placeholder', (active && PLACEHOLDERS[s]) ? PLACEHOLDERS[s] : basePlaceholder);
    }
    if (window.syncSendBtn) window.syncSendBtn();
  }

  // ---------- Konuşma metni hazırlama ----------
  function isJunkToken(tok){
    if (tok.indexOf('http://') === 0 || tok.indexOf('https://') === 0 || tok.indexOf('www.') === 0) return true;
    for (let i = 0; i < tok.length; i++) {
      if ('-:='.indexOf(tok[i]) === -1) return false;
    }
    return true;
  }

  function cleanForSpeech(md){
    let s = String(md || '');
    const a = s.indexOf('<think>');
    const b = s.indexOf('</think>');
    if (a !== -1 && b !== -1) s = s.substring(0, a) + s.substring(b + 8);
    else if (a !== -1) s = s.substring(0, a);

    let hadCode = false;
    s = s.split('```').filter(function(p, i){
      if (i % 2 === 1) { hadCode = true; return false; }
      return true;
    }).join(NL);

    let out = '';
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      const code = s.charCodeAt(i);
      if (ch === '[') continue;
      if (ch === ']') {
        if (s[i + 1] === '(') {
          const close = s.indexOf(')', i);
          if (close !== -1) i = close;
        }
        continue;
      }
      if ('*_`#>|~'.indexOf(ch) !== -1) continue;
      if (code >= 0xD800 && code <= 0xDFFF) continue;
      if (code >= 0x2600 && code <= 0x27BF) continue;
      if (code === 0xFE0F || code === 0x200D) continue;
      if (ch === NL) {
        out = out.trimEnd();
        if (out && '.!?:;,'.indexOf(out[out.length - 1]) === -1) out += '.';
        out += ' ';
        continue;
      }
      out += ch;
    }
    out = out.split(' ').filter(function(t){ return t && !isJunkToken(t); }).join(' ');
    if (hadCode) {
      if (out && '.!?'.indexOf(out[out.length - 1]) === -1) out += '.';
      out += ' Kod örneğini ekranda görebilirsin.';
    }
    return out.trim();
  }

  // Uzun cümleleri parçalara böler: tarayıcıların uzun tek seferlik okumada sesi kesme sorununu önler.
  function chunkText(text){
    const MAX = 180;
    const chunks = [];
    let cur = '';
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      cur += ch;
      const atEnd = '.!?;:'.indexOf(ch) !== -1 && (i + 1 >= text.length || text[i + 1] === ' ');
      if ((atEnd && cur.length >= 40) || cur.length >= MAX) {
        if (!atEnd) {
          const cut = Math.max(cur.lastIndexOf(', '), cur.lastIndexOf(' '));
          if (cut > 60) {
            chunks.push(cur.slice(0, cut + 1).trim());
            cur = cur.slice(cut + 1);
            continue;
          }
        }
        chunks.push(cur.trim());
        cur = '';
      }
    }
    if (cur.trim()) chunks.push(cur.trim());
    return chunks.filter(Boolean);
  }

  function pickVoice(){
    if (!ttsSupported()) return null;
    let list = [];
    try { list = window.speechSynthesis.getVoices() || []; } catch (e) {}
    const tr = list.filter(function(v){ return v.lang && v.lang.toLowerCase().indexOf('tr') === 0; });
    if (!tr.length) return null;
    const prefs = ['natural', 'neural', 'premium', 'enhanced', 'google', 'yelda', 'emel'];
    for (let p = 0; p < prefs.length; p++) {
      const hit = tr.find(function(v){ return (v.name || '').toLowerCase().indexOf(prefs[p]) !== -1; });
      if (hit) return hit;
    }
    return tr[0];
  }

  function speak(text, myRun, done){
    const clean = cleanForSpeech(text);
    if (!clean || (!ttsSupported() && !NeuralTTS.available())) { done(); return; }
    const all = chunkText(clean);
    const chunks = [];
    let total = 0;
    for (let k = 0; k < all.length; k++) {
      if (total > 3000) { chunks.push('Devamını ekranda okuyabilirsin.'); break; }
      chunks.push(all[k]);
      total += all[k].length;
    }
    const token = ++ttsToken;
    function runBrowser(){
    try { window.speechSynthesis.cancel(); } catch (e) {}
    let i = 0;
    function next(){
      if (token !== ttsToken || !active || myRun !== runId) return;
      if (i >= chunks.length) { done(); return; }
      const piece = chunks[i++];
      const u = new SpeechSynthesisUtterance(piece);
      u.lang = 'tr-TR';
      const v = pickVoice();
      if (v) u.voice = v;
      u.rate = 1.0;
      u.pitch = 1.0;
      let finished = false;
      let wd = null;
      const go = function(){
        if (finished) return;
        finished = true;
        clearTimeout(wd);
        next();
      };
      u.onend = go;
      u.onerror = go;
      wd = setTimeout(function(){
        if (finished) return;
        try { window.speechSynthesis.cancel(); } catch (e) {}
        go();
      }, 5000 + piece.length * 110);
      try { window.speechSynthesis.speak(u); } catch (e) { go(); }
    }
    setTimeout(next, 60);
    }
    if (NeuralTTS.available()) {
      const alive = function(){ return token === ttsToken && active && myRun === runId; };
      NeuralTTS.speakChunks(chunks, {
        onend: function(){ if (alive()) done(); },
        onfail: function(){ if (alive()) { if (ttsSupported()) runBrowser(); else done(); } }
      });
      return;
    }
    runBrowser();
  }

  // ---------- Dinleme ----------
  function listen(){
    if (!active) return;
    const myRun = runId;
    const mySession = ++sessionId;
    const ta = byId('msgInput');
    let finalText = '';
    let shown = '';
    let fatal = '';
    let r;
    try { r = new SR(); } catch (e) { endVoice(); alert('Sesli konuşma başlatılamadı.'); return; }
    rec = r;
    setState('listening');
    r.lang = 'tr-TR';
    r.interimResults = true;
    r.continuous = false;
    r.maxAlternatives = 1;
    const wd = setTimeout(function(){
      if (mySession === sessionId) { try { r.stop(); } catch (e) {} }
    }, 60000);
    r.onstart = function(){ if (mySession === sessionId && active) startRetries = 0; };
    r.onresult = function(e){
      if (mySession !== sessionId) return;
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) finalText += res[0].transcript;
        else interim += res[0].transcript;
      }
      shown = (finalText + interim).trim();
      if (ta) {
        ta.value = shown;
        if (window.autoSizeMsgInput) window.autoSizeMsgInput();
      }
    };
    r.onerror = function(e){
      if (mySession !== sessionId) return;
      const err = e && e.error;
      if (err === 'not-allowed' || err === 'service-not-allowed') fatal = 'Mikrofon izni reddedildi. Tarayıcı ayarlarından mikrofona izin ver.';
      else if (err === 'audio-capture') fatal = 'Mikrofon bulunamadı ya da kullanılamıyor.';
      else if (err === 'network') fatal = 'Ses tanıma servisine ulaşılamadı. İnternet bağlantını kontrol et.';
      else if (err === 'language-not-supported') fatal = 'Türkçe ses tanıma bu cihazda desteklenmiyor.';
    };
    r.onend = function(){
      clearTimeout(wd);
      if (mySession !== sessionId) return;
      rec = null;
      if (!active || myRun !== runId) return;
      if (fatal) { endVoice(); alert(fatal); return; }
      const text = (finalText || shown).trim();
      if (!text) {
        emptyRuns++;
        if (emptyRuns >= 3) { endVoice(); return; }
        timer = setTimeout(listen, 250);
        return;
      }
      emptyRuns = 0;
      submit(text, myRun);
    };
    try {
      r.start();
    } catch (e) {
      clearTimeout(wd);
      rec = null;
      if (startRetries < 2) { startRetries++; timer = setTimeout(listen, 400); }
      else { endVoice(); alert('Mikrofon başlatılamadı. Lütfen tekrar dene.'); }
    }
  }

  // ---------- Gönder, cevabı bekle, sesli oku ----------
  async function submit(text, myRun){
    const ta = byId('msgInput');
    setState('thinking');
    if (ta) {
      ta.value = text;
      if (window.autoSizeMsgInput) window.autoSizeMsgInput();
    }
    const chat = chats.find(function(c){ return c.id === currentChatId; });
    const n0 = chat ? chat.messages.length : 0;
    try { await sendMsg(); } catch (e) {}
    if (!active || myRun !== runId) return;
    let reply = '';
    if (chat && chat.messages.length > n0 + 1) {
      const last = chat.messages[chat.messages.length - 1];
      if (last && last.role === 'assistant') reply = last.content || '';
    }
    if (!String(reply).trim()) reply = 'Bir sorun oluştu. Tekrar dener misin?';
    setState('speaking');
    speak(reply, myRun, function(){
      if (!active || myRun !== runId) return;
      emptyRuns = 0;
      timer = setTimeout(listen, 350);
    });
  }

  // ---------- Başlat / bitir ----------
  async function start(){
    if (active || starting || streaming) return;
    if (!SR) { alert('Bu tarayıcı sesli konuşmayı desteklemiyor. Chrome veya Safari kullanabilirsin.'); return; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alert('Mikrofon erişimi bu bağlantı üzerinden engelli. Güvenli (https) bir bağlantı gerekir.');
      return;
    }
    closeMbMenus();
    if (window.Dictation) window.Dictation.cancel();
    stopSpeaking();
    NeuralTTS.unlock();
    // Kullanıcının dokunuşu sırasında sesli okumayı "uyandırıyoruz"; telefonlar sonradan başlayan okumayı bu sayede engellemiyor.
    if (ttsSupported()) {
      try {
        const u = new SpeechSynthesisUtterance(' ');
        u.volume = 0;
        window.speechSynthesis.speak(u);
      } catch (e) {}
    }
    starting = true;
    if (!micOk) {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach(function(t){ t.stop(); });
        micOk = true;
      } catch (err) {
        starting = false;
        alert('Mikrofon izni alınamadı: ' + (err && err.message ? err.message : err));
        return;
      }
    }
    starting = false;
    active = true;
    runId++;
    emptyRuns = 0;
    startRetries = 0;
    listen();
  }

  function endVoice(){
    active = false;
    starting = false;
    runId++;
    sessionId++;
    ttsToken++;
    NeuralTTS.stop();
    clearTimeout(timer);
    timer = null;
    if (rec) { try { rec.abort(); } catch (e) {} rec = null; }
    if (ttsSupported()) { try { window.speechSynthesis.cancel(); } catch (e) {} }
    setState('idle');
  }

  function toggle(){
    if (active) endVoice();
    else start();
  }

  // Yazmak isteyen kullanıcı kutuya dokununca sesli konuşma biter; ekran kapanınca/sekme değişince de biter.
  (function(){
    const ta = byId('msgInput');
    if (ta) ta.addEventListener('pointerdown', function(){ if (active) endVoice(); });
    document.addEventListener('visibilitychange', function(){ if (document.hidden && active) endVoice(); });
    window.addEventListener('pagehide', function(){ if (active) endVoice(); });
  })();

  return {
    toggle: toggle,
    end: endVoice,
    isActive: function(){ return active; },
    getState: function(){ return state; },
    _cleanForSpeech: cleanForSpeech,
    _chunkText: chunkText
  };
})();
window.VoiceMode = VoiceMode;

function toggleVoiceMode(){ VoiceMode.toggle(); }

// ============================================
// SESLE YAZMA (mikrofon butonu): konuşulanı yazı kutusuna yazar, otomatik göndermez
// ============================================
const Dictation = (function(){
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null;
  let listening = false;
  let stopping = false;
  let session = 0;
  let base = '';
  let finalText = '';
  let restarts = 0;
  let stopTimer = null;

  function byId(id){ return document.getElementById(id); }

  function setUI(on){
    const b = byId('micBtn');
    if (b) {
      b.classList.toggle('is-listening', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      const t = on ? 'Dinlemeyi durdur' : 'Sesle yaz';
      b.title = t;
      b.setAttribute('aria-label', t);
    }
  }

  function render(interim){
    const ta = byId('msgInput');
    if (!ta) return;
    const spoken = (finalText + (interim || '')).trim();
    const sep = (base && spoken && base.charAt(base.length - 1) !== String.fromCharCode(32)) ? ' ' : '';
    ta.value = base + sep + spoken;
    if (window.autoSizeMsgInput) window.autoSizeMsgInput();
  }

  function finish(){
    clearTimeout(stopTimer);
    stopTimer = null;
    listening = false;
    stopping = false;
    rec = null;
    setUI(false);
  }

  function cancel(){
    session++;
    if (rec) { try { rec.abort(); } catch (e) {} }
    finish();
  }

  function begin(){
    const my = ++session;
    let gotText = false;
    let fatal = '';
    let r;
    try { r = new SR(); } catch (e) { finish(); alert('Sesle yazma başlatılamadı.'); return; }
    rec = r;
    r.lang = 'tr-TR';
    r.interimResults = true;
    r.continuous = false;
    r.maxAlternatives = 1;
    r.onresult = function(e){
      if (my !== session) return;
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) { finalText += (finalText ? ' ' : '') + res[0].transcript.trim(); gotText = true; }
        else interim += res[0].transcript;
      }
      render(interim);
    };
    r.onerror = function(e){
      if (my !== session) return;
      const err = e && e.error;
      if (err === 'not-allowed' || err === 'service-not-allowed') fatal = 'Mikrofon izni reddedildi. Tarayıcı ayarlarından mikrofona izin ver.';
      else if (err === 'audio-capture') fatal = 'Mikrofon bulunamadı ya da kullanılamıyor.';
      else if (err === 'network') fatal = 'Ses tanıma servisine ulaşılamadı. İnternet bağlantını kontrol et.';
      else if (err === 'language-not-supported') fatal = 'Türkçe ses tanıma bu cihazda desteklenmiyor.';
    };
    r.onend = function(){
      if (my !== session) return;
      if (fatal) { finish(); alert(fatal); return; }
      // Konuşma bittiyse ve kullanıcı durdurmadıysa, yeni konuşma için dinlemeyi sürdür (sessizlikte durur).
      if (listening && !stopping && gotText && restarts < 20 && !(window.VoiceMode && window.VoiceMode.isActive())) {
        restarts++;
        base = (byId('msgInput') ? byId('msgInput').value : base);
        finalText = '';
        setTimeout(function(){ if (my === session && listening && !stopping) begin(); }, 150);
        return;
      }
      render('');
      finish();
    };
    try {
      r.start();
    } catch (e) {
      finish();
      alert('Mikrofon başlatılamadı. Lütfen tekrar dene.');
    }
  }

  function start(){
    if (listening) return;
    if (window.VoiceMode && window.VoiceMode.isActive()) return;
    if (!SR) { alert('Bu tarayıcı sesle yazmayı desteklemiyor. Chrome veya Safari kullanabilirsin.'); return; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alert('Mikrofon erişimi bu bağlantı üzerinden engelli. Güvenli (https) bir bağlantı gerekir.');
      return;
    }
    try { closeMbMenus(); } catch (e) {}
    try { stopSpeaking(); } catch (e) {}
    const ta = byId('msgInput');
    base = ta ? ta.value : '';
    finalText = '';
    restarts = 0;
    stopping = false;
    listening = true;
    setUI(true);
    begin();
  }

  function stop(){
    if (!listening) return;
    stopping = true;
    if (rec) { try { rec.stop(); } catch (e) { cancel(); return; } }
    // Güvenlik: onend gelmezse arayüz takılı kalmasın.
    clearTimeout(stopTimer);
    stopTimer = setTimeout(function(){ if (listening) cancel(); }, 2500);
  }

  function toggle(){ if (listening) stop(); else start(); }

  (function(){
    const ta = byId('msgInput');
    // Kullanıcı elle yazmaya başlarsa sesle yazma durur (elle yazılan metin ezilmesin).
    if (ta) ta.addEventListener('input', function(e){ if (listening && e.isTrusted) cancel(); });
    document.addEventListener('visibilitychange', function(){ if (document.hidden && listening) cancel(); });
    window.addEventListener('pagehide', function(){ if (listening) cancel(); });
  })();

  return { toggle: toggle, cancel: cancel, isListening: function(){ return listening; } };
})();
window.Dictation = Dictation;
function toggleDictation(){ Dictation.toggle(); }

// ============================================
// GEÇMİŞ TEMİZLEME
// ============================================
function askClearAllHistory() {
  if (streaming) {
    alert('Cevap yazılırken geçmiş temizlenemez. Lütfen cevabın bitmesini bekle.');
    return;
  }
  if (window.VoiceMode && window.VoiceMode.isActive()) window.VoiceMode.end();
  clearHistory();
}
function clearHistory() {
  document.getElementById('confirmOverlay').style.display = 'flex';
}
function hideClearConfirm() {
  document.getElementById('confirmOverlay').style.display = 'none';
}
async function confirmClearHistory() {
  hideClearConfirm();
  closeSettings();
  try {
    const response = await fetch('/history/clear', { method: 'POST' });
    const result = await response.json();
    if(result.ok) {
      chats = [];
      createChat('Yeni Sohbet');
      currentChatId = chats[0].id;
      renderSidebar();
      switchChat(currentChatId);
      removeFile();
      document.getElementById('msgInput').value = '';
      if (window.autoSizeMsgInput) window.autoSizeMsgInput();
    }
  } catch(err) {
    alert("Geçmiş temizlenirken bir hata oluştu.");
  }
}

// ============================================
// AYARLAR (sistem promptu / model bazli yaratıcılık seviyesi)
// ============================================
let settingsDefaultPrompt = '';
let settingsDefaultTemps = {};   // { model_id: default_temperature }
let settingsModelNames = {};     // { model_id: 'Görünen Ad' }

function renderModelTempSliders(currentTemps) {
  const container = document.getElementById('settingsModelTemps');
  container.innerHTML = '';
  Object.keys(settingsDefaultTemps).forEach(modelId => {
    const defaultVal = settingsDefaultTemps[modelId];
    const val = (currentTemps && currentTemps[modelId] !== undefined && currentTemps[modelId] !== null)
      ? currentTemps[modelId] : defaultVal;
    const label = settingsModelNames[modelId] || modelId;

    const item = document.createElement('div');
    item.className = 'settings-model-temp-item';
    item.dataset.model = modelId;
    item.innerHTML = `
      <div class="settings-model-temp-name"><span>${label}</span><span class="val">${parseFloat(val).toFixed(2)}</span></div>
      <div class="settings-slider-row">
        <span class="settings-slider-cap">Tutarlı</span>
        <input type="range" class="settings-slider model-temp-slider" min="0" max="1" step="0.05" value="${val}">
        <span class="settings-slider-cap">Yaratıcı</span>
      </div>
    `;
    container.appendChild(item);
  });
}

function updateModelTempLabel(sliderEl) {
  const item = sliderEl.closest('.settings-model-temp-item');
  const valSpan = item.querySelector('.settings-model-temp-name .val');
  valSpan.textContent = parseFloat(sliderEl.value).toFixed(2);
}

async function openSettings() {
  const overlay = document.getElementById('settingsOverlay');
  overlay.style.display = 'flex';
  document.getElementById('settingsSavedMsg').classList.remove('show');
  switchSettingsTab('general');
  try {
    const res = await fetch('/settings');
    const data = await res.json();
    settingsDefaultPrompt = data.default_system_prompt || '';
    settingsDefaultTemps = data.default_temperatures || {};
    settingsModelNames = data.model_names || {};
    document.getElementById('settingsSystemPrompt').value = data.system_prompt || settingsDefaultPrompt;
    renderModelTempSliders(data.temperatures || {});
  } catch (err) {
    alert('Ayarlar yüklenemedi.');
  }
  loadApiKeys();
  loadSearchKey();
  loadSerperKey();
  loadSearchEnabled();
}

function closeSettings() {
  document.getElementById('settingsOverlay').style.display = 'none';
}

function switchSettingsTab(tab) {
  document.querySelectorAll('.settings-tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  document.getElementById('settingsTabGeneral').classList.toggle('active', tab === 'general');
  document.getElementById('settingsTabApiKeys').classList.toggle('active', tab === 'apikeys');
}

function resetSettingsToDefault() {
  document.getElementById('settingsSystemPrompt').value = settingsDefaultPrompt;
  renderModelTempSliders({});
}

async function saveSettings() {
  const promptVal = document.getElementById('settingsSystemPrompt').value;
  const temperatures = {};
  document.querySelectorAll('#settingsModelTemps .settings-model-temp-item').forEach(item => {
    const modelId = item.dataset.model;
    const slider = item.querySelector('.model-temp-slider');
    temperatures[modelId] = parseFloat(slider.value);
  });
  try {
    const res = await fetch('/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ system_prompt: promptVal, temperatures: temperatures })
    });
    const result = await res.json();
    if (result.ok) {
      const msg = document.getElementById('settingsSavedMsg');
      msg.classList.add('show');
      setTimeout(() => { msg.classList.remove('show'); closeSettings(); }, 900);
    } else {
      alert('Ayarlar kaydedilemedi.');
    }
  } catch (err) {
    alert('Ayarlar kaydedilirken bir hata oluştu.');
  }
}

// ============================================
// API ANAHTARLARI (6 slot, her biri kendi başına güncellenip kaydedilir)
// ============================================
const ICON_EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const ICON_EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.94 10.94 0 0112 20c-7 0-11-8-11-8a21.62 21.62 0 015.06-6.06M9.9 4.24A10.94 10.94 0 0112 4c7 0 11 8 11 8a21.6 21.6 0 01-3.22 4.44M14.12 14.12a3 3 0 11-4.24-4.24"/><path d="M1 1l22 22"/></svg>';

async function loadApiKeys() {
  try {
    const res = await fetch('/settings/api-keys');
    const data = await res.json();
    renderApiKeys(data.keys || []);
  } catch (err) {
    document.getElementById('apiKeysList').innerHTML = '<div class="settings-hint">Anahtarlar yüklenemedi.</div>';
  }
}

function renderApiKeys(keys) {
  const container = document.getElementById('apiKeysList');
  container.innerHTML = '';
  keys.forEach((key, i) => {
    const isEmpty = !key;
    const item = document.createElement('div');
    item.className = 'api-key-item';
    item.dataset.slot = i;
    item.innerHTML = `
      <div class="api-key-item-label-row">
        <span class="api-key-item-label">Anahtar ${i + 1}</span>
        <span class="api-key-item-status ${isEmpty ? 'empty' : 'active'}">${isEmpty ? 'Boş' : 'Aktif'}</span>
      </div>
      <div class="api-key-input-row">
        <input type="password" class="api-key-input" value="${key.replace(/"/g, '&quot;')}" placeholder="${isEmpty ? 'Yeni anahtar ekle (gsk_...)' : 'gsk_...'}" spellcheck="false" autocomplete="off">
        <button type="button" class="api-key-toggle-btn" onclick="toggleApiKeyVisibility(this)" title="Göster/Gizle">${ICON_EYE}</button>
      </div>
      <div class="api-key-item-footer">
        <span class="api-key-saved-msg">Kaydedildi ✓</span>
        <button type="button" class="api-key-save-btn" onclick="saveApiKey(${i})">Güncelle ve Kaydet</button>
      </div>
    `;
    container.appendChild(item);
  });
}

function toggleApiKeyVisibility(btn) {
  const input = btn.parentElement.querySelector('.api-key-input');
  const showing = input.type === 'text';
  input.type = showing ? 'password' : 'text';
  btn.innerHTML = showing ? ICON_EYE : ICON_EYE_OFF;
}

async function saveApiKey(slot) {
  const item = document.querySelector(`.api-key-item[data-slot="${slot}"]`);
  const input = item.querySelector('.api-key-input');
  const value = input.value.trim();
  try {
    const res = await fetch(`/settings/api-keys/${slot}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: value })
    });
    const result = await res.json();
    if (result.ok) {
      const status = item.querySelector('.api-key-item-status');
      const isEmpty = !value;
      status.textContent = isEmpty ? 'Boş' : 'Aktif';
      status.className = 'api-key-item-status ' + (isEmpty ? 'empty' : 'active');
      input.placeholder = isEmpty ? 'Yeni anahtar ekle (gsk_...)' : 'gsk_...';
      const msg = item.querySelector('.api-key-saved-msg');
      msg.classList.add('show');
      setTimeout(() => msg.classList.remove('show'), 1400);
    } else {
      alert(result.error || 'Anahtar kaydedilemedi.');
    }
  } catch (err) {
    alert('Anahtar kaydedilirken bir hata oluştu.');
  }
}

// ============================================
// INTERNET ARAMASI AC/KAPA
// ============================================
async function loadSearchEnabled() {
  const toggle = document.getElementById('searchEnabledToggle');
  const hint = document.getElementById('searchEnabledHint');
  try {
    const res = await fetch('/settings/search-enabled');
    const data = await res.json();
    const enabled = data.enabled !== false;
    toggle.checked = enabled;
    hint.textContent = enabled ? 'Açık' : 'Kapalı';
  } catch (err) {
    // Sessizce basarisiz ol: durum yuklenemese bile Ayarlar penceresinin geri
    // kalani calismaya devam eder. Toggle varsayilan (isaretli) durumda kalir.
  }
}

async function saveSearchEnabled(checked) {
  const toggle = document.getElementById('searchEnabledToggle');
  const hint = document.getElementById('searchEnabledHint');
  try {
    const res = await fetch('/settings/search-enabled', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: checked })
    });
    const result = await res.json();
    if (result.ok) {
      const enabled = result.enabled !== false;
      toggle.checked = enabled;
      hint.textContent = enabled ? 'Açık' : 'Kapalı';
    } else {
      toggle.checked = !checked;
      alert(result.error || 'Ayar kaydedilemedi.');
    }
  } catch (err) {
    toggle.checked = !checked;
    alert('Ayar kaydedilirken bir hata oluştu.');
  }
}

// ============================================
// ARAMA API'Sİ (Tavily) - tekil anahtar, ayni desende
// ============================================
async function loadSearchKey() {
  try {
    const res = await fetch('/settings/search-key');
    const data = await res.json();
    const key = data.key || '';
    document.getElementById('searchKeyInput').value = key;
    const status = document.getElementById('searchKeyStatus');
    status.textContent = key ? 'Aktif' : 'Boş';
    status.className = 'api-key-item-status ' + (key ? 'active' : 'empty');
  } catch (err) {
    // Sessizce basarisiz ol: arama anahtari yuklenemese bile Ayarlar penceresinin
    // geri kalani (sistem promptu, model sicakliklari, Groq anahtarlari) calismaya devam eder.
  }
}

async function saveSearchKey() {
  const input = document.getElementById('searchKeyInput');
  const value = input.value.trim();
  try {
    const res = await fetch('/settings/search-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: value })
    });
    const result = await res.json();
    if (result.ok) {
      const status = document.getElementById('searchKeyStatus');
      const isEmpty = !value;
      status.textContent = isEmpty ? 'Boş' : 'Aktif';
      status.className = 'api-key-item-status ' + (isEmpty ? 'empty' : 'active');
      const msg = document.getElementById('searchKeySavedMsg');
      msg.classList.add('show');
      setTimeout(() => msg.classList.remove('show'), 1400);
    } else {
      alert(result.error || 'Anahtar kaydedilemedi.');
    }
  } catch (err) {
    alert('Anahtar kaydedilirken bir hata oluştu.');
  }
}

// ============================================
// ARAMA API'Sİ (Serper) - tekil anahtar, ayni desende
// ============================================
async function loadSerperKey() {
  try {
    const res = await fetch('/settings/serper-key');
    const data = await res.json();
    const key = data.key || '';
    document.getElementById('serperKeyInput').value = key;
    const status = document.getElementById('serperKeyStatus');
    status.textContent = key ? 'Aktif' : 'Boş';
    status.className = 'api-key-item-status ' + (key ? 'active' : 'empty');
  } catch (err) {
    // Sessizce basarisiz ol: serper anahtari yuklenemese bile Ayarlar penceresinin
    // geri kalani calismaya devam eder.
  }
}

async function saveSerperKey() {
  const input = document.getElementById('serperKeyInput');
  const value = input.value.trim();
  try {
    const res = await fetch('/settings/serper-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: value })
    });
    const result = await res.json();
    if (result.ok) {
      const status = document.getElementById('serperKeyStatus');
      const isEmpty = !value;
      status.textContent = isEmpty ? 'Boş' : 'Aktif';
      status.className = 'api-key-item-status ' + (isEmpty ? 'empty' : 'active');
      const msg = document.getElementById('serperKeySavedMsg');
      msg.classList.add('show');
      setTimeout(() => msg.classList.remove('show'), 1400);
    } else {
      alert(result.error || 'Anahtar kaydedilemedi.');
    }
  } catch (err) {
    alert('Anahtar kaydedilirken bir hata oluştu.');
  }
}

document.addEventListener('input', function (e) {
  if (e.target && e.target.classList && e.target.classList.contains('model-temp-slider')) {
    updateModelTempLabel(e.target);
  }
});

// ============================================
// SOHBET GÖNDERME
// ============================================
let streaming = false;

// Gonder butonu durumu: mesaj bos (ve ekli dosya yok) ya da yanit akarken devre disi/soluk;
// gonderilebilir hale gelince CSS gecisiyle yumusakca canlanir.
let _lastShowSend = null;
window.syncSendBtn = function(){
  const btn = document.getElementById('sendBtn');
  const inp = document.getElementById('msgInput');
  const talk = document.getElementById('talkBtn');
  const wrap = document.getElementById('inputWrapper');
  if (!btn || !inp) return;
  const hasInput = !!inp.value.trim() || !!uploadedFile;
  const voiceOn = !!(window.VoiceMode && window.VoiceMode.isActive());
  btn.disabled = streaming || !hasInput;
  if (talk) talk.disabled = streaming && !voiceOn;
  const homeBtn = document.getElementById('hdrHomeBtn');
  if (homeBtn) homeBtn.disabled = !!streaming;
  // Yazi (ya da ekli dosya) varken gonder oku, yokken Talk gorunur; sesli konusma suruyorken Talk kalir.
  const showSend = hasInput && !voiceOn;
  if (wrap) wrap.classList.toggle('has-input', showSend);
  if (showSend !== _lastShowSend) {
    _lastShowSend = showSend;
    if (typeof syncModelPill === 'function') syncModelPill();
  }
};
window.syncSendBtn();

// Gonderirken butonun etrafinda kisa bir isik halkasi (animasyon CSS'te).
window.fireSendPulse = function(){
  const b = document.getElementById('sendBtn');
  if (!b) return;
  b.classList.remove('is-firing');
  void b.offsetWidth;   // animasyonun her gonderimde bastan baslamasi icin reflow
  b.classList.add('is-firing');
};
(function(){
  const b = document.getElementById('sendBtn');
  if (b) b.addEventListener('animationend', function(e){
    if (e.animationName === 'send-pulse') b.classList.remove('is-firing');
  });
})();

async function sendMsg() {
  if (streaming) return;
  if (window.Dictation) window.Dictation.cancel();
  stopSpeaking();
  const inputEl = document.getElementById('msgInput');
  if (!inputEl) return;
  const text = inputEl.value.trim();
  if (!text && !uploadedFile) return;
  if (window.fireSendPulse) window.fireSendPulse();

  const chatArea = document.getElementById('chatArea');
  const welcome = document.getElementById('welcomeContainer');
  if (welcome) {
    welcome.remove();
    chatArea.classList.remove('is-empty');
  }

  let uG = document.createElement('div');
  uG.className = 'msg-group user-side';
  const uTime = new Date().toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' });
  uG.innerHTML = '<div class="bubble user-bubble">' + text + '<div class="msg-time">' + uTime + '</div></div>';
  chatArea.appendChild(uG);
  chatArea.scrollTop = chatArea.scrollHeight;

  const chat = chats.find(c => c.id === currentChatId);
  if (chat) {
    chat.messages.push({ role: 'user', content: text, timestamp: new Date().toISOString() });
    if (chat.title === 'Yeni Sohbet' && text.length > 0) {
      chat.title = text.slice(0, 25) + (text.length > 25 ? '...' : '');
      renderSidebar();
    }
    saveChatsToStorage();
  }

  const sentText = text;
  const sentFile = uploadedFile;
  inputEl.value = '';
  if (window.autoSizeMsgInput) window.autoSizeMsgInput();
  removeFile();

  const sendBtn = document.getElementById('sendBtn');
  const statusDot = document.getElementById('statusDot');
  streaming = true;
  if (sendBtn) sendBtn.disabled = true;
  if (window.syncSendBtn) window.syncSendBtn();
  if (statusDot) statusDot.textContent = 'Yazıyor...';

  const tG = document.createElement('div');
  tG.className = 'msg-group';
  tG.innerHTML = '<div class="bubble ai-bubble" style="color:var(--stone-dim)">Yanıt hazırlanıyor</div>';
  chatArea.appendChild(tG);
  chatArea.scrollTop = chatArea.scrollHeight;

  // --- Baglanti kopmasina karsi koruma ---
  // Her istege benzersiz kimlik veriyoruz. Ekran kilitlenir/baglanti koparsa sunucu cevabi
  // yine de tamamlar; biz de bu kimlikle tamamlanmis cevabi geri aliriz.
  const reqId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : (Date.now().toString(36) + Math.random().toString(36).slice(2));
  const ctl = new AbortController();
  let aiB = null;
  let full = '';
  let searchedThisResponse = false;
  let currentSources = [];
  let buffer = '';
  let gotFinal = false;
  let httpErr = 0;
  let recovering = false;

  function stripThinkText(s) {
    let t = s || '';
    const a = t.indexOf('<think>');
    const b = t.indexOf('</think>');
    if (a !== -1 && b !== -1) t = t.substring(0, a) + t.substring(b + 8);
    else if (a !== -1) t = t.substring(0, a);
    return t.trim();
  }

  function makeAiBubble(sources) {
    if (aiB) return;
    tG.remove();
    const aiG = document.createElement('div');
    aiG.className = 'msg-group';
    aiG.innerHTML = '<div class="msg-meta"></div>';
    aiB = document.createElement('div');
    aiB.className = 'bubble ai-bubble';
    aiG.appendChild(aiB);
    const footerEl = buildSourcesFooter(Array.isArray(sources) ? sources : []);
    if (footerEl) aiG.appendChild(footerEl);
    chatArea.appendChild(aiG);
  }

  function showFail(msg) {
    makeAiBubble([]);
    const warn = '<div style="color:#ff6b6b;font-size:11px;margin-top:8px">' + msg + '</div>';
    const partial = (full && full.trim()) ? marked.parse(stripThinkText(full)) : '';
    aiB.innerHTML = partial + warn;
    if (partial) enhanceContentBlocks(aiB);
  }

  async function recoverAnswer() {
    recovering = true;
    if (statusDot) statusDot.textContent = 'Yeniden bağlanıyor...';
    if (window.renderTimeout) { clearTimeout(window.renderTimeout); window.renderTimeout = null; }
    const sleep = ms => new Promise(res => setTimeout(res, ms));
    let job = null;
    let unknownCount = 0;
    // Sayac tabanli (saat tabanli degil): telefon kilitliyken zamanlayicilar durur,
    // geri donunce kaldigi yerden devam edebilsin diye.
    for (let i = 0; i < 150; i++) {
      try {
        const r = await fetch('/chat/job/' + encodeURIComponent(reqId), { cache: 'no-store' });
        job = await r.json();
        if (job.status === 'done' || job.status === 'error') break;
        if (job.status === 'unknown') { unknownCount++; if (unknownCount >= 4) break; }
        else { unknownCount = 0; }
      } catch (e) { job = null; }
      await sleep(2000);
    }
    recovering = false;
    if (job && job.status === 'done' && job.text) {
      makeAiBubble(job.sources);
      full = job.text;
      try { aiB.dataset.sources = JSON.stringify(Array.isArray(job.sources) ? job.sources : []); } catch (e) {}
      aiB.innerHTML = marked.parse(stripThinkText(full));
      enhanceContentBlocks(aiB);
      const aTime = document.createElement('div');
      aTime.className = 'msg-time';
      aTime.textContent = new Date().toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' });
      aiB.appendChild(aTime);
      if (chat) {
        chat.messages.push({ role: 'assistant', content: full, timestamp: new Date().toISOString(), searched: !!(job.sources && job.sources.length), sources: job.sources || [] });
        saveChatsToStorage();
      }
      try { if (job.model) onModelAnswered(job.model); } catch (e) {}
      return;
    }
    if (job && job.status === 'error') {
      if (job.text) full = job.text;
      showFail(job.error || 'Bir hata oluştu.');
    } else if (job && job.status === 'unknown') {
      showFail('Bağlantı kesildi. Mesaj sunucuya ulaşmamış ya da sunucu yeniden başlamış olabilir; lütfen tekrar gönder.');
    } else {
      showFail('Bağlantı kesildi ve cevap sunucudan alınamadı. Lütfen tekrar dene.');
    }
  }

  // Ekrana geri donuldugunda akis takili kalmis olabilir: sunucudaki isin durumuna bak,
  // cevap hazirsa eski baglantiyi kapatip hazir cevabi al.
  const onVisible = async () => {
    if (document.visibilityState !== 'visible' || gotFinal || recovering) return;
    try {
      const r = await fetch('/chat/job/' + encodeURIComponent(reqId), { cache: 'no-store' });
      const j = await r.json();
      if (!gotFinal && (j.status === 'done' || j.status === 'error')) ctl.abort();
    } catch (e) {}
  };
  document.addEventListener('visibilitychange', onVisible);

  try {
    const response = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: sentText, model: selModel, file: sentFile, chat_id: currentChatId, req_id: reqId }),
      signal: ctl.signal
    });
    if (!response.ok) { httpErr = response.status; throw new Error('HTTP ' + response.status); }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let lines = buffer.split(String.fromCharCode(10));
      buffer = lines.pop();

      for (let line of lines) {
        line = line.trim();
        if (!line || !line.startsWith('data: ')) continue;
        try {
          const ev = JSON.parse(line.slice(6));

          if (ev.type === 'model' && !aiB) {
            tG.remove();
            onModelAnswered(ev.model);  // mesaj kutusundaki model kutusu, cevap veren modelin adina canli olarak gecer
            searchedThisResponse = !!ev.searched;
            currentSources = Array.isArray(ev.sources) ? ev.sources : [];
            const aiG = document.createElement('div');
            aiG.className = 'msg-group';
            // Cevaplarin ustunde marka, model adi veya arama rozeti GOSTERILMEZ; satir yalnizca sesli okuma dugmesini tasir.
            aiG.innerHTML = '<div class="msg-meta"></div>';
            aiB = document.createElement('div');
            aiB.className = 'bubble ai-bubble';
            aiG.appendChild(aiB);
            const footerEl = buildSourcesFooter(currentSources);
            if (footerEl) aiG.appendChild(footerEl);
            chatArea.appendChild(aiG);
          } else if (ev.type === 'done') {
            gotFinal = true;
            if (window.renderTimeout) { clearTimeout(window.renderTimeout); window.renderTimeout = null; }
            if (aiB) {
              let displayMain = full;
              let tStart = displayMain.indexOf('<think>');
              let tEnd = displayMain.indexOf('</think>');
              if (tStart !== -1 && tEnd !== -1) displayMain = displayMain.substring(0, tStart) + displayMain.substring(tEnd + 8);
              else if (tStart !== -1 && tEnd === -1) displayMain = displayMain.substring(0, tStart);
              try { aiB.dataset.sources = JSON.stringify(Array.isArray(currentSources) ? currentSources : []); } catch (e) {}
              aiB.innerHTML = marked.parse(displayMain.trim());
              enhanceContentBlocks(aiB);
              const aTime = document.createElement('div');
              aTime.className = 'msg-time';
              aTime.textContent = new Date().toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' });
              aiB.appendChild(aTime);
            }
            if (chat) {
              chat.messages.push({ role: 'assistant', content: full, timestamp: new Date().toISOString(), searched: searchedThisResponse, sources: currentSources });
              saveChatsToStorage();
            }
          } else if (ev.type === 'error') {
            gotFinal = true;
            if (window.renderTimeout) { clearTimeout(window.renderTimeout); window.renderTimeout = null; }
            if (!aiB) {
              tG.remove();
              const aiG = document.createElement('div');
              aiG.className = 'msg-group';
              aiG.innerHTML = '<div class="msg-meta" style="color:var(--danger)"><span>Hata</span></div>';
              aiB = document.createElement('div');
              aiB.className = 'bubble ai-bubble';
              aiG.appendChild(aiB);
              chatArea.appendChild(aiG);
            }
            // Onemli: eger o ana kadar akmis bir metin varsa ASLA silmiyoruz, sadece altina
            // kucuk bir uyari ekliyoruz. Aksi halde kullanici streaming halindeki cevabin
            // birden kaybolup sadece hata yazisi kaldigini görüyor, bu da "donmus/yarida
            // kalmis" hissi yaratiyordu.
            if (full && full.trim()) {
              aiB.innerHTML = marked.parse(full.trim()) + '<div style="color:#ff6b6b;font-size:11px;margin-top:8px">' + (ev.text || 'Bağlantı kesildi.') + '</div>';
              enhanceContentBlocks(aiB);
            } else {
              aiB.innerHTML = '<span style="color:#ff6b6b">' + (ev.text || 'Bir hata olustu.') + '</span>';
            }
          } else if (ev.type === 'delta' && aiB) {
            full += ev.text;
            if (!window.renderTimeout) {
              window.renderTimeout = setTimeout(() => {
                let displayMain = full;
                let tStart = displayMain.indexOf('<think>');
                let tEnd = displayMain.indexOf('</think>');
                if (tStart !== -1 && tEnd !== -1) {
                  displayMain = displayMain.substring(0, tStart) + displayMain.substring(tEnd + 8);
                } else if (tStart !== -1 && tEnd === -1) {
                  displayMain = displayMain.substring(0, tStart);
                }
                aiB.innerHTML = marked.parse(displayMain.trim());
                enhanceContentBlocks(aiB);
                window.renderTimeout = null;
              }, 60);
            }
          }
        } catch (e) {}
      }
    }
  } catch (err) {}

  if (!gotFinal) {
    if (httpErr) showFail('Sunucu hatası (' + httpErr + '). Mesaj gönderilemedi.');
    else await recoverAnswer();
  }
  document.removeEventListener('visibilitychange', onVisible);

  streaming = false;
  if (window.syncSendBtn) window.syncSendBtn();
  if (statusDot) statusDot.textContent = 'Aktif';
}

// ============================================
// SAYFA YÜKLENİNCE
// ============================================
document.addEventListener('DOMContentLoaded', function() {
  renderSidebar();
  
  // Her zaman "Yeni Sohbet" aç (boş)
  let newChat = chats.find(c => c.title === 'Yeni Sohbet' && c.messages.length === 0);
  if (!newChat) {
    newChat = createChat('Yeni Sohbet');
  }
  currentChatId = newChat.id;
  switchChat(currentChatId);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(syncModelPill);
});
</script>
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    try { navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {}); } catch (e) {}
  });
}
</script>
</body>
</html>
"""

# ============================================
# ANA SAYFA
# ============================================

# ============================================
# DOGAL SES (Microsoft Edge neural TTS: ucretsiz, API anahtari gerektirmez)
# Kurulum: pip install edge-tts  (requirements.txt'ye de ekle)
# Yuklu degilse ya da hata olursa arayuz otomatik tarayici sesine doner.
# ============================================
TTS_VOICE = os.environ.get('TTS_VOICE', 'tr-TR-EmelNeural')
TTS_RATE = os.environ.get('TTS_RATE', '+8%')
TTS_PITCH = os.environ.get('TTS_PITCH', '+2Hz')
_TTS_CACHE = {}
_TTS_CACHE_LOCK = threading.Lock()
_TTS_CACHE_MAX = 80

def _tts_prepare(text):
    t = re.sub(r'https?://\S+', ' ', text or '')
    t = re.sub(r'[*_`#>~|]+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:1200]

async def _tts_synth(text):
    import edge_tts
    comm = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
    buf = bytearray()
    async for chunk in comm.stream():
        if chunk.get('type') == 'audio':
            buf.extend(chunk['data'])
    return bytes(buf)

def _tts_run(text):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(_tts_synth(text), timeout=25))
    finally:
        loop.close()

@app.route('/tts', methods=['POST'])
def tts_route():
    try:
        import edge_tts  # noqa: F401
    except Exception:
        return jsonify({'error': 'edge-tts yuklu degil'}), 503
    data = request.get_json(silent=True) or {}
    text = _tts_prepare(str(data.get('text') or ''))
    if not text:
        return jsonify({'error': 'bos metin'}), 400
    with _TTS_CACHE_LOCK:
        audio = _TTS_CACHE.get(text)
    if not audio:
        for _attempt in range(2):
            try:
                audio = _tts_run(text)
                if audio:
                    break
            except Exception as e:
                print(f'[tts] hata: {type(e).__name__}: {e}')
        if not audio:
            return jsonify({'error': 'ses uretilemedi'}), 502
        with _TTS_CACHE_LOCK:
            while len(_TTS_CACHE) >= _TTS_CACHE_MAX:
                _TTS_CACHE.pop(next(iter(_TTS_CACHE)), None)
            _TTS_CACHE[text] = audio
    return Response(audio, mimetype='audio/mpeg', headers={'Cache-Control': 'no-store'})

@app.route('/')
def index():
    return HTML

# ============================================
# UYGULAMAYI BAŞLAT
# ============================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
