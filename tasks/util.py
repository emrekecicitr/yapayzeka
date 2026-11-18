"""Gorev sistemi ortak yardimcilari (saf fonksiyonlar)."""
import ipaddress
import json
import re
from urllib.parse import urlparse

_SECRET_RE = re.compile(
    r'(gsk_[A-Za-z0-9]{6,}|tvly-[A-Za-z0-9_\-]{6,}|sk-[A-Za-z0-9_\-]{12,}|'
    r'(?i:bearer)\s+[A-Za-z0-9._\-]{12,}|[A-Fa-f0-9]{40,})')


def redact(text):
    """Hata mesajlarina/loglara sizabilecek API anahtari benzeri dizileri maskeler."""
    return _SECRET_RE.sub('[gizli]', str(text or ''))


# ---- guvenilmeyen icerik (web sayfasi, arama sonucu) ----------------------
_INJECTION_RE = re.compile(
    r'(?i)('
    r'ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)|'
    r'disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)|'
    r'forget\s+(?:all\s+)?(?:previous|prior|your)\s+(?:instructions?|rules?)|'
    r'(?:reveal|show|print|leak|send)\s+.{0,40}(?:system\s+prompt|api[\s_-]?key|password|secret)|'
    r'you\s+are\s+now\s+(?:a|an|in)\b|new\s+instructions?\s*:|system\s*(?:prompt|message)\s*:|'
    r'öncek[iî]\s+(?:tüm\s+)?(?:talimat|komut|kural)\w*\s+(?:yok\s+say|unut|geçersiz)|'
    r'yukarıdaki\s+(?:tüm\s+)?(?:talimat|komut)\w*|'
    r'(?:talimat|komut)\w*\s+(?:yok\s+say|görmezden\s+gel)|'
    r'artık\s+sen\b|sistem\s+(?:mesaj|talimat|istem)\w*\s*:|'
    r'api\s*anahtar\w*\s+(?:ver|göster|yaz|paylaş)'
    r')')
_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def sanitize_untrusted(text, max_chars=8000):
    """Web/arama kaynakli metni modele VERI olarak vermeden once temizler.
    - kontrol karakterlerini ve bizim ayirac dizilerimizi (<<< >>>) etkisizlestirir,
    - talimat benzeri satirlari 'filtrelendi' ile degistirir (en iyi cabadir; asil koruma, metnin
      acikca 'yalnizca veri' diye isaretlenip modele sistem kurallariyla birlikte verilmesidir).
    Donus: (temiz_metin, filtrelenen_satir_sayisi)"""
    t = _CTRL_RE.sub('', str(text or ''))
    t = t.replace('<<<', '‹‹‹').replace('>>>', '›››')
    out, flagged = [], 0
    for line in t.split('\n'):
        if _INJECTION_RE.search(line):
            out.append('[filtrelendi: talimat benzeri içerik]')
            flagged += 1
        else:
            out.append(line)
    t = '\n'.join(out)
    if len(t) > max_chars:
        t = t[:max_chars].rstrip() + '\n[kısaltıldı]'
    return t, flagged


# ---- cikti temizligi -------------------------------------------------------
def strip_dashes(text):
    """Uzun (U+2014) ve kisa (U+2013) tireleri kaldirir: sayi araliginda '-', digerlerinde virgul."""
    t = str(text or '')
    t = re.sub(r'(?<=\d)\s*[\u2013\u2014]\s*(?=\d)', '-', t)
    t = re.sub(r'\s*[\u2013\u2014]\s*', ', ', t)
    return t


def extract_json(text):
    """Model ciktisindan ilk JSON nesnesini ayiklar; yoksa None."""
    if not text:
        return None
    s = str(text).strip()
    s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s, flags=re.I)
    a, b = s.find('{'), s.rfind('}')
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(s[a:b + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def truncate(text, n):
    t = str(text or '')
    return t if len(t) <= n else t[:n].rstrip() + '…'


def check_url(url):
    """Kosul izleme icin URL on dogrulamasi. Gecerliyse normalize URL, degilse None.
    (DNS tabanli SSRF kontrolu cekim aninda uygulama icindeki _is_fetchable ile ayrica yapilir.)"""
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u or len(u) > 500:
        return None
    try:
        p = urlparse(u)
    except ValueError:
        return None
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        return None
    host = p.hostname.lower()
    if host == 'localhost' or host.endswith(('.local', '.internal', '.localhost')):
        return None
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return None
    except ValueError:
        pass
    return u
