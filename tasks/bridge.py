"""Bridge: gorev motoru ile app.py'deki mevcut yapay zeka altyapisi arasindaki TEK temas noktasi.

Mevcut kod yeniden yazilmaz; app.py'nin su parcalari oldugu gibi kullanilir:
  MODELS_CHAIN / selected_model (model zinciri ve fallback), get_available_keys / mark_key_cooldown (anahtar rotasyonu),
  _rl_* (Groq hiz siniri yonetimi), get_generation_params (model parametreleri), search_with_fallback (Serper + Tavily
  arastirmasi), _fetch_page_text (SSRF korumali sayfa okuma), memory_prompt_block (kalici hafiza), _tr_now_str.

Gorev motoru yalnizca bu arayuzu bilir; testlerde FakeBridge ile degistirilir (gercek API kredisi harcanmaz).
Anahtarlar hicbir zaman loglanmaz ya da hata mesajina yazilmaz.
"""
import re
import time

from . import config
from .util import redact


class LLMError(Exception):
    """kind: no_keys | auth | rate_limit | network | timeout | server | bad_response | too_large | invalid_request"""

    _MESSAGES = {
        'no_keys': 'Kayıtlı bir API anahtarı yok. Ayarlar, API Anahtarları bölümünden ekleyin.',
        'auth': 'API anahtarı geçersiz veya yetkisiz. Ayarlar bölümünden anahtarı kontrol edin.',
        'rate_limit': 'Model şu an istek sınırında (rate limit). Daha sonra otomatik yeniden denenecek.',
        'network': 'İnternet bağlantısı veya model sunucusuna erişim sorunu.',
        'timeout': 'Model zamanında yanıt vermedi (zaman aşımı).',
        'server': 'Model sunucusu geçici bir hata verdi.',
        'bad_response': 'Model anlaşılır bir yanıt üretmedi.',
        'too_large': 'İstek modelin giriş sınırına sığmadı.',
        'invalid_request': 'Model isteği reddetti (geçersiz istek).',
    }
    _RETRIABLE = {'rate_limit', 'network', 'timeout', 'server', 'bad_response', 'too_large'}

    def __init__(self, kind, detail=''):
        super().__init__(f'{kind}: {redact(detail)[:200]}')
        self.kind = kind
        self.detail = redact(detail)[:200]
        self.retriable = kind in self._RETRIABLE

    def user_message(self):
        return self._MESSAGES.get(self.kind, 'Model çağrısı başarısız oldu.')


class LLMResult:
    def __init__(self, text, model):
        self.text = text
        self.model = model


_THINK_RE = re.compile(r'(?is)<think>.*?</think>')


class AppBridge:
    """app.py modulunu (sys.modules[__name__]) sarar; nitelikler her cagrida o anki degerleriyle okunur."""

    def __init__(self, app_module):
        self.a = app_module

    # ------------------------------------------------------------ modeller
    def models(self):
        a = self.a
        names = getattr(a, 'MODEL_DISPLAY_NAMES', {}) or {}
        return [{'id': m, 'name': names.get(m, m)} for m in a.MODELS_CHAIN]

    def _chain(self, model):
        a = self.a
        base = list(a.MODELS_CHAIN)
        first = model or getattr(a, 'selected_model', None)
        if first:
            return [first] + [m for m in base if m != first]
        return base

    def now_text(self):
        return self.a._tr_now_str()

    def memory_note(self):
        try:
            return (self.a.memory_prompt_block() or '')[:1200]
        except Exception:
            return ''

    def input_char_budget(self, model=None):
        """Modelin dakikalik token sinirina (ucretsiz planda kisitli) gore guvenli giris karakter butcesi."""
        a = self.a
        try:
            m = model or self._chain(None)[0]
            if not a._rl_kisitli(m):
                return 24000
            limit = a._rl_limit(m)
            cpt = a._RL_CPT['v']
            return max(3000, int((limit - 2200 - a.GROQ_GUVENLIK_PAYI) * cpt))
        except Exception:
            return 9000

    # ------------------------------------------------------------ model cagrisi
    def llm(self, messages, model=None, max_tokens=1200, purpose='task', timeout=None, deadline=None):
        timeout = timeout or config.LLM_CALL_TIMEOUT
        last = None
        for m in self._chain(model):
            try:
                return self._call_model(m, messages, max_tokens, timeout, deadline, purpose)
            except LLMError as e:
                last = e
                if e.kind == 'too_large':
                    raise  # cagiran baglami kucultup yeniden dener
                continue
        raise last or LLMError('no_keys')

    def _call_model(self, model, messages, max_tokens, timeout, deadline, purpose):
        a = self.a
        gorev = 'sohbet' if purpose in ('queries', 'condition') else 'analiz'
        params = dict(a.get_generation_params(model, gorev, int(max_tokens)))
        payload = {'model': model, 'messages': messages, 'stream': False}
        payload.update(params)
        charged = False
        if a._rl_kisitli(model):
            need = a._rl_est(a._msgs_chars(messages)) + int(max_tokens) + a.GROQ_GUVENLIK_PAYI
            wait_s = a._rl_wait_for(model, need)
            if wait_s == float('inf'):
                raise LLMError('too_large', f'{model} icin gereken {need} token sinira sigmiyor')
            if wait_s > 45:
                raise LLMError('rate_limit', 'kapasite gec acilacak')
            if wait_s > 0:
                self._sleep(wait_s + 0.3, deadline)
            a._rl_charge(model, need)
            charged = True
        keys = list(a.get_available_keys(model))
        if not keys:
            if not a.GROQ_API_KEYS:
                raise LLMError('no_keys')
            raise LLMError('rate_limit', 'tum anahtarlar bekleme suresinde')
        httpx = a.httpx
        last = LLMError('server')
        retried_reasoning = False
        i = 0
        while i < len(keys):
            key = keys[i]
            i += 1
            t = timeout
            if deadline:
                t = max(5.0, min(timeout, deadline - time.time()))
            try:
                r = a._GROQ_CLIENT.post(a.URL, headers={'Authorization': f'Bearer {key}'}, json=payload, timeout=t)
            except httpx.TimeoutException:
                last = LLMError('timeout')
                continue
            except Exception as e:  # noqa: BLE001
                last = LLMError('network', type(e).__name__)
                continue
            try:
                a._rl_update(model, r.headers)
            except Exception:
                pass
            sc = r.status_code
            if sc == 200:
                try:
                    txt = (r.json()['choices'][0]['message'].get('content') or '')
                except Exception:
                    last = LLMError('bad_response')
                    continue
                txt = _THINK_RE.sub('', txt).strip()
                if not txt:
                    last = LLMError('bad_response', 'bos yanit')
                    continue
                return LLMResult(txt, model)
            body = ''
            try:
                body = r.text or ''
            except Exception:
                pass
            if sc == 429:
                cd = None
                ra = r.headers.get('retry-after')
                if ra:
                    try:
                        cd = float(ra)
                    except ValueError:
                        cd = None
                if not cd:
                    cd = a.parse_reset_string(r.headers.get('x-ratelimit-reset-requests')) or a.DEFAULT_COOLDOWN_SECONDS
                a.mark_key_cooldown(key, model, cd)
                last = LLMError('rate_limit', body[:120])
                continue
            if sc in (401, 403):
                a.mark_key_cooldown(key, model, 3600)
                last = LLMError('auth', f'HTTP {sc}')
                continue
            if sc in (400, 413):
                try:
                    a._rl_ogren_hata(model, body)
                except Exception:
                    pass
                if sc == 400 and 'reasoning' in body.lower() and 'reasoning_effort' in payload and not retried_reasoning:
                    retried_reasoning = True
                    payload.pop('reasoning_effort', None)
                    i -= 1  # ayni anahtarla reasoning'siz yeniden dene
                    continue
                if sc == 413 or a.is_context_length_error(sc, body) or 'too large' in body.lower():
                    raise LLMError('too_large', body[:120])
                last = LLMError('invalid_request', f'HTTP {sc}')
                continue
            last = LLMError('server', f'HTTP {sc}')
        raise last

    @staticmethod
    def _sleep(seconds, deadline):
        if deadline:
            seconds = min(seconds, max(0.0, deadline - time.time()))
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(left, 1.0))

    # ------------------------------------------------------------ arastirma
    def search(self, queries, depth, question, news=False):
        """Mevcut Serper + Tavily arastirma hattini kullanir. ASLA exception firlatmaz."""
        a = self.a
        try:
            qs = [q for q in queries if q][:a.SEARCH_MAX_QUERIES]
            plan = {
                'queries': qs,
                'zaman': 'hafta' if news else None,
                'haber': bool(news or a._HABER_RE.search(a._tr_lower(question or ''))),
                'detay': depth != 'hizli',
            }
            plan = a._apply_fresh_rules(plan, question or '')
            r = a.search_with_fallback(plan, question or (qs[0] if qs else ''))
            return {
                'context': r.get('context') or '',
                'sources': [{'title': s.get('title', ''), 'url': s.get('url', '')} for s in (r.get('sources') or [])],
                'errors': [redact(e) for e in (r.get('errors') or [])],
                'attempted': bool(r.get('attempted')),
            }
        except Exception as e:  # noqa: BLE001
            return {'context': '', 'sources': [], 'errors': [type(e).__name__], 'attempted': True}

    def fetch_page(self, url):
        """SSRF korumali (app._is_fetchable) sayfa okuma. Okunamazsa ''."""
        try:
            return self.a._fetch_page_text(url, [], max_chars=6000, timeout=8.0) or ''
        except Exception:
            return ''
