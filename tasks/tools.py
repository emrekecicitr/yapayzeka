"""Ajan araclari (SALT OKUNUR, yan etkisiz) ve model cikti ayristirma.

Guvenlik ilkeleri
- Arac kumesi sabit bir IZIN LISTESIDIR: search, read_page, calculate, recall, finish. Baska ad reddedilir.
- Hicbir arac dis sistemde degisiklik yapamaz (e-posta, dosya, odeme yok).
- read_page yalnizca KOKENI BILINEN adresleri okur: bu calistirmada arama sonuclarinda cikmis veya gorev
  aciklamasinda yazili adresler. Boylece web iceriginden gelen bir talimat ("su adresi oku") ajani rastgele bir
  adrese (SSRF veya veri sizdirma) goturemez. Ayrica mevcut SSRF korumasi (bridge.fetch_page) cekim aninda uygulanir.
- calculate, eval KULLANMAZ: AST izin listesi, boyut ve us sinirlari vardir; modelin yazdigi ifade kod calistiramaz.
- Arac sonuclari (web metni) modele 'yalnizca veri' olarak, temizlenip kisaltilarak verilir.
- recall yalnizca AYNI gorevin onceki sonuclarini dondurur (gorevler arasi sizinti yok).
"""
import ast
import json
import math
import re
import time

from . import config, util

ACTIONS = ('search', 'read_page', 'calculate', 'recall', 'finish')
_ALIASES = {'web_search': 'search', 'read': 'read_page', 'fetch': 'read_page', 'open_page': 'read_page',
            'readpage': 'read_page', 'calc': 'calculate', 'calculator': 'calculate', 'done': 'finish',
            'final': 'finish', 'answer': 'finish', 'final_answer': 'finish'}
_CTRL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')


class ToolError(Exception):
    """Kullaniciya/modele gosterilecek Turkce mesaj tasir."""


# ======================================================================
# guvenli hesap makinesi
# ======================================================================
_FUNCS = {'abs': abs, 'round': round, 'min': min, 'max': max, 'sqrt': math.sqrt, 'floor': math.floor,
          'ceil': math.ceil, 'log': math.log, 'log10': math.log10, 'exp': math.exp}
_CONSTS = {'pi': math.pi, 'e': math.e}
_MAX_INT_BITS = 20000
_CALC_CHARS_RE = re.compile(r'^[0-9A-Za-z\s+\-*/%().,^]*$')
_LONG_NUMBER_RE = re.compile(r'\d{51,}')
_NUM_LETTER_RE = re.compile(r'[0-9.]+[A-Za-z]+')
_EXP_OK_RE = re.compile(r'(?:\d+\.?\d*|\.\d+)[eE]\d*')


def _check_num(v):
    if type(v) is bool or type(v) not in (int, float):
        raise ToolError('Sonuç gerçek bir sayı değil.')
    if type(v) is float and (v != v or v in (float('inf'), float('-inf'))):
        raise ToolError('Sonuç tanımsız veya çok büyük.')
    if type(v) is int and v.bit_length() > _MAX_INT_BITS:
        raise ToolError('Sonuç çok büyük.')
    return v


def _binop(op, a, b):
    t = type(op)
    try:
        if t is ast.Add:
            return _check_num(a + b)
        if t is ast.Sub:
            return _check_num(a - b)
        if t is ast.Mult:
            return _check_num(a * b)
        if t in (ast.Div, ast.FloorDiv, ast.Mod):
            if b == 0:
                raise ToolError('Sıfıra bölme yapılamaz.')
            return _check_num(a / b if t is ast.Div else a // b if t is ast.FloorDiv else a % b)
        if t is ast.Pow:
            if abs(b) > 10000:
                raise ToolError('Üs çok büyük.')
            if type(a) is int and type(b) is int and b > 0:
                if b * max(1, abs(a).bit_length()) > _MAX_INT_BITS:
                    raise ToolError('Üs alma sonucu çok büyük.')
            return _check_num(a ** b)
    except ZeroDivisionError:
        raise ToolError('Sıfıra bölme yapılamaz.')
    except OverflowError:
        raise ToolError('Sonuç çok büyük.')
    raise ToolError('Desteklenmeyen işlem.')


_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)


def _ev(n, depth=0):
    if depth > 150:    # 200 karakterlik sinirda en derin gecerli ifade (1+1+...) yaklasik 100 duzey
        raise ToolError('İfade çok karmaşık.')
    if isinstance(n, ast.Expression):
        return _ev(n.body, depth + 1)
    if isinstance(n, ast.Constant):
        v = n.value
        if type(v) is bool or type(v) not in (int, float):
            raise ToolError('Yalnızca sayılar kullanılabilir.')
        return _check_num(v)
    if isinstance(n, ast.UnaryOp) and type(n.op) in (ast.UAdd, ast.USub):
        v = _ev(n.operand, depth + 1)
        return _check_num(-v if type(n.op) is ast.USub else +v)
    if isinstance(n, ast.BinOp) and type(n.op) in _BINOPS:
        return _binop(n.op, _ev(n.left, depth + 1), _ev(n.right, depth + 1))
    if isinstance(n, ast.Name):
        if n.id in _CONSTS:
            return _CONSTS[n.id]
        raise ToolError(f"Bilinmeyen ad: '{n.id[:20]}'. Yalnızca pi ve e kullanılabilir.")
    if isinstance(n, ast.Call):
        if not isinstance(n.func, ast.Name) or n.func.id not in _FUNCS or n.keywords:
            raise ToolError('Yalnızca abs, round, min, max, sqrt, floor, ceil, log, log10, exp işlevleri kullanılabilir.')
        if not 1 <= len(n.args) <= 3:
            raise ToolError('İşlev argüman sayısı geçersiz.')
        args = [_ev(a, depth + 1) for a in n.args]
        name = n.func.id
        if name == 'round':
            if len(args) == 2 and (type(args[1]) is not int or not -10 <= args[1] <= 15):
                raise ToolError('round için ondalık basamak sayısı -10 ile 15 arasında bir tam sayı olmalı.')
            if len(args) > 2:
                raise ToolError('round en çok 2 argüman alır.')
        elif name in ('abs', 'sqrt', 'floor', 'ceil', 'log10', 'exp') and len(args) != 1:
            raise ToolError(f'{name} tek argüman alır.')
        elif name == 'log' and len(args) > 2:
            raise ToolError('log en çok 2 argüman alır.')
        try:
            return _check_num(_FUNCS[name](*args))
        except (ValueError, OverflowError, TypeError, ZeroDivisionError):
            raise ToolError(f'{name} bu değerler için hesaplanamadı.')
    if isinstance(n, ast.Tuple):
        raise ToolError('Virgül yalnızca işlev argümanlarını ayırır; ondalık sayılar için nokta kullanın (örn. 3.5).')
    raise ToolError('Desteklenmeyen ifade. Yalnızca sayılar, + - * / // % ** ve parantez kullanılabilir.')


def _fmt_number(v):
    if type(v) is int:
        s = str(v)
        if len(s) <= 40:
            return s
        sign, digits = ('-', s[1:]) if s.startswith('-') else ('', s)   # float'a cevirmeden bilimsel gosterim
        return f'{sign}{digits[0]}.{digits[1:9]}e+{len(digits) - 1}'
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return format(v, '.12g')


def calc_value(expr):
    """Aritmetik ifadeyi GUVENLI degerlendirir, SAYISAL deger (int|float) dondurur. Hata: ToolError."""
    if not isinstance(expr, str):
        raise ToolError('İfade metin olmalı.')
    e = expr.strip().replace('\u2212', '-').replace('\u00d7', '*').replace('\u00f7', '/').replace('^', '**')
    if not e:
        raise ToolError('İfade boş.')
    if len(e) > config.AGENT_CALC_EXPR_CHARS:
        raise ToolError(f'İfade en fazla {config.AGENT_CALC_EXPR_CHARS} karakter olabilir.')
    if not _CALC_CHARS_RE.match(e):
        raise ToolError('İfade geçersiz karakter içeriyor (ondalık için nokta kullanın).')
    if _LONG_NUMBER_RE.search(e):
        raise ToolError('Sayı çok uzun.')
    for m in _NUM_LETTER_RE.finditer(e):          # '2pi', '3x', '0x10', '1else': yalnizca ussel gosterim (1e5) gecerli
        if not _EXP_OK_RE.fullmatch(m.group(0)):
            raise ToolError('Sayı ile harf bitişik yazılamaz; çarpma için * kullanın (örn. 2*pi).')
    try:
        tree = ast.parse(e, mode='eval')
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise ToolError('İfade anlaşılamadı. Örnek: (120 * 1.2) / 3')
    try:
        return _ev(tree)
    except RecursionError:
        raise ToolError('İfade çok karmaşık.')


def safe_calc(expr):
    """Aritmetik ifadeyi GUVENLI degerlendirir. Donus: 'ifade = sonuc'. Hata: ToolError."""
    v = calc_value(expr)
    return f'{expr.strip()[:120]} = {_fmt_number(v)}'


def format_number(v):
    return _fmt_number(v)


# ======================================================================
# model ciktisini ayristirma ve dogrulama
# ======================================================================
def first_json_object(text):
    """Metindeki ILK gecerli JSON nesnesini bulur (kod citi, onceki/sonraki aciklama veya birden fazla nesne olabilir)."""
    if not text:
        return None
    s = str(text)[:20000]
    s = re.sub(r'(?is)<think>.*?</think>', '', s)
    dec = json.JSONDecoder()
    tried = 0
    i = s.find('{')
    while i != -1 and tried < 25:
        try:
            obj, _ = dec.raw_decode(s[i:])
            if isinstance(obj, dict):
                return obj
        except (ValueError, RecursionError):
            pass
        tried += 1
        i = s.find('{', i + 1)
    return None


def _clean(s, max_len):
    s = _CTRL.sub('', str(s)).strip()
    s = re.sub(r'[ \t]+', ' ', s)
    return s[:max_len]


_TOP_LEVEL_ARG_KEYS = ('query', 'url', 'expression', 'expr', 'findings', 'answer', 'summary', 'result', 'confidence')


def validate_args(action, args):
    """Eylem argumanlarini dogrular ve normallestirir. Hata: ToolError (modele geri bildirilir)."""
    if action == 'search':
        q = args.get('query')
        if not isinstance(q, str) or not q.strip():
            raise ToolError('search için "query" (metin) gerekli.')
        return {'query': _clean(re.sub(r'\s+', ' ', q), 200)}
    if action == 'read_page':
        u = args.get('url')
        if not isinstance(u, str) or not u.strip():
            raise ToolError('read_page için "url" (metin) gerekli.')
        clean = util.check_url(u.strip())
        if not clean:
            raise ToolError('Geçerli bir http/https adresi değil (yerel ve özel adresler kabul edilmez).')
        return {'url': clean}
    if action == 'calculate':
        e = args.get('expression', args.get('expr'))
        if not isinstance(e, str) or not e.strip():
            raise ToolError('calculate için "expression" (metin) gerekli.')
        if len(e.strip()) > config.AGENT_CALC_EXPR_CHARS:
            raise ToolError(f'İfade en fazla {config.AGENT_CALC_EXPR_CHARS} karakter olabilir.')
        return {'expression': _clean(e, config.AGENT_CALC_EXPR_CHARS)}
    if action == 'recall':
        return {}
    if action == 'finish':
        f = args.get('findings', args.get('answer', args.get('summary', args.get('result'))))
        if not isinstance(f, str) or not f.strip():
            raise ToolError('finish için "findings" (bulguların metni) gerekli.')
        conf = str(args.get('confidence') or 'orta').strip().lower()
        conf = {'düşük': 'dusuk', 'yüksek': 'yuksek', 'low': 'dusuk', 'medium': 'orta', 'high': 'yuksek'}.get(conf, conf)
        if conf not in ('dusuk', 'orta', 'yuksek'):
            conf = 'orta'
        return {'findings': util.strip_dashes(_clean(f, 3500)).strip(), 'confidence': conf}
    raise ToolError('Bilinmeyen eylem.')


def parse_action(text):
    """Donus: ({'thought','action','args'}, None) veya (None, hata_mesaji)."""
    obj = first_json_object(text)
    if obj is None:
        return None, 'Yanıt geçerli bir JSON nesnesi değil.'
    action = obj.get('action')
    if not isinstance(action, str) or not action.strip():
        return None, '"action" alanı eksik.'
    a = re.sub(r'[\s\-]+', '_', action.strip().lower())
    a = _ALIASES.get(a, a)
    if a not in ACTIONS:
        return None, f'Bilinmeyen eylem. Geçerli eylemler: {", ".join(ACTIONS)}.'
    args = obj.get('args')
    if args is None:
        args = {k: obj[k] for k in _TOP_LEVEL_ARG_KEYS if k in obj}
    if not isinstance(args, dict):
        return None, '"args" bir nesne olmalı.'
    try:
        args = validate_args(a, args)
    except ToolError as e:
        return None, str(e)
    thought = obj.get('thought')
    thought = util.strip_dashes(_clean(thought, 600)) if isinstance(thought, str) else ''
    return {'thought': thought, 'action': a, 'args': args}, None


# ======================================================================
# arac baglami ve arac uygulamalari
# ======================================================================
def norm_url(u):
    u = (u or '').strip().split('#', 1)[0]
    return u[:-1] if u.endswith('/') else u


def description_urls(text):
    out = []
    for m in _URL_RE.finditer(text or ''):
        u = util.check_url(m.group(0).rstrip('.,;:'))
        if u and u not in out:
            out.append(u)
        if len(out) >= 10:
            break
    return out


class Outcome:
    """Bir arac cagrisinin sonucu. status: ok | error | rejected"""
    __slots__ = ('status', 'text', 'flagged', 'new_sources')

    def __init__(self, status, text, flagged=0, new_sources=None):
        self.status = status
        self.text = text
        self.flagged = flagged
        self.new_sources = new_sources or []


class ToolContext:
    def __init__(self, task, run, runs, bridge, budgets, internet, depth, cache_get, cache_put, clock=time.time):
        self.task = task
        self.run = run
        self.runs = runs
        self.bridge = bridge
        self.budgets = budgets
        self.internet = internet
        self.depth = depth
        self.cache_get = cache_get
        self.cache_put = cache_put
        self.clock = clock
        self.sources = []              # [{'n','title','url'}] sirali ve benzersiz; modelin [n] atiflari buna gore
        self._src_keys = set()
        # normallestirilmis adres -> kanonik adres. Okuma HER ZAMAN kanonik adresle yapilir (modelin yazdigi varyantla degil).
        self.allowed_urls = {norm_url(u): u for u in description_urls((task.get('title') or '') + ' ' + (task.get('description') or ''))}
        self.counts = {'search': 0, 'read_page': 0, 'calculate': 0, 'recall': 0}
        self.seen_calls = set()
        self.flagged_total = 0
        self.evidence_ok = 0           # basarili search/read_page sayisi
        self.evidence_fail = 0         # basarisiz search/read_page sayisi
        self.other_ok = 0              # basarili calculate/recall (gercek onceki sonuc donduren)

    # ---- tekrar kurulum (kaldigi yerden devam): kayitli turlardan durumu geri olustur
    def replay(self, turns):
        for t in turns:
            a = t['action']
            args = t.get('args') or {}
            if t['status'] != 'rejected' and a in self.counts:
                self.counts[a] += 1
            if t['status'] != 'rejected':
                self.seen_calls.add(self._call_key(a, args))
            for s in args.get('_sources') or []:
                self._add_source(s.get('title'), s.get('url'))
            self.flagged_total += int(t.get('flagged') or 0)
            if a in ('search', 'read_page'):
                if t['status'] == 'ok':
                    self.evidence_ok += 1
                elif t['status'] == 'error':
                    self.evidence_fail += 1
            elif a in ('calculate', 'recall') and t['status'] == 'ok' and not (t.get('observation') or '').startswith('Bu görevin önceki başarılı'):
                self.other_ok += 1

    @staticmethod
    def _call_key(action, args):
        clean = {k: v for k, v in (args or {}).items() if not str(k).startswith('_')}
        if action == 'search':
            clean = {'query': ' '.join(str(clean.get('query', '')).lower().split())}
        elif action == 'read_page':
            clean = {'url': norm_url(str(clean.get('url', '')))}
        return action + ':' + json.dumps(clean, sort_keys=True, ensure_ascii=False)

    def _add_source(self, title, url):
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            return None
        key = norm_url(url)
        if key in self._src_keys:
            for s in self.sources:
                if norm_url(s['url']) == key:
                    return s
            return None
        if len(self.sources) >= config.AGENT_MAX_SOURCES:
            return None
        s = {'n': len(self.sources) + 1, 'title': util.truncate(util.strip_dashes(title or url), 160), 'url': url}
        self.sources.append(s)
        self._src_keys.add(key)
        self.allowed_urls.setdefault(key, url)
        return s

    def remaining(self):
        return {k: max(0, self.budgets.get(k, 0) - self.counts.get(k, 0)) for k in self.counts}

    # ---- tek giris noktasi
    def run_tool(self, action, args):
        key = self._call_key(action, args)
        if key in self.seen_calls:
            return Outcome('rejected', 'Bu çağrıyı zaten yaptın, sonucu yukarıda. Farklı bir şey dene ya da finish çağır.')
        fn = {'search': self._search, 'read_page': self._read_page, 'calculate': self._calculate,
              'recall': self._recall}[action]
        out = fn(args)
        if out.status != 'rejected':
            self.seen_calls.add(key)
        return out

    def _budget_check(self, action):
        if self.counts[action] >= self.budgets[action]:
            return Outcome('rejected', f'{action} bütçesi doldu. Elindeki verilerle finish çağır.')
        return None

    def _search(self, args):
        if not self.internet:
            return Outcome('rejected', 'Bu görevde internet araştırması kapalı. Yalnızca calculate, recall ve finish kullanılabilir.')
        rej = self._budget_check('search')
        if rej:
            return rej
        q = args['query']
        depth = self.depth
        ck = 'agent|' + ' '.join(q.lower().split()) + '|' + depth
        data = self.cache_get(ck)
        if data is None:
            res = self.bridge.search([q], depth, q)
            data = {'context': res.get('context') or '', 'sources': res.get('sources') or [], 'errors': res.get('errors') or []}
            if data['context']:
                self.cache_put(ck, {'context': data['context'], 'sources': data['sources']})
        self.counts['search'] += 1
        if not data.get('context') and not data.get('sources'):
            self.evidence_fail += 1
            err = '; '.join(str(e) for e in (data.get('errors') or []))[:150]
            return Outcome('error', 'Arama sonuç döndürmedi' + (f' ({util.redact(err)})' if err else '') + '. Sorguyu değiştirip dene ya da finish çağır.')
        self.evidence_ok += 1
        listed = []
        for s in data.get('sources') or []:
            reg = self._add_source(s.get('title'), s.get('url'))
            if reg and reg not in listed:
                listed.append(reg)
        cleaned, flagged = util.sanitize_untrusted(data.get('context') or '', config.AGENT_OBS_CHARS)
        text = cleaned
        if listed:
            text += '\n\nKAYNAK LİSTESİ (alıntı numaraları):\n' + '\n'.join(f"[{s['n']}] {s['title']} ({s['url']})" for s in listed[:8])
        self.flagged_total += flagged
        return Outcome('ok', text, flagged, [{'title': s['title'], 'url': s['url']} for s in listed])

    def _read_page(self, args):
        if not self.internet:
            return Outcome('rejected', 'Bu görevde internet araştırması kapalı, sayfa okunamaz.')
        url = args['url']
        if norm_url(url) not in self.allowed_urls:
            return Outcome('rejected', 'Bu adres izin verilen kaynaklar arasında değil. Yalnızca arama sonuçlarında çıkan veya '
                                       'görev açıklamasında yazılı adresler okunabilir. Önce search kullan.')
        rej = self._budget_check('read_page')
        if rej:
            return rej
        url = self.allowed_urls[norm_url(url)]
        text = self.bridge.fetch_page(url)
        self.counts['read_page'] += 1
        if not text or not text.strip():
            self.evidence_fail += 1
            return Outcome('error', 'Sayfa okunamadı (erişilemedi, boş ya da güvenli olmayan adres). Başka bir kaynak dene.')
        self.evidence_ok += 1
        cleaned, flagged = util.sanitize_untrusted(text, config.AGENT_READ_CHARS)
        self.flagged_total += flagged
        reg = next((s for s in self.sources if norm_url(s['url']) == norm_url(url)), None)
        head = f"[{reg['n']}] {reg['title']} ({url})\n" if reg else f'({url})\n'
        return Outcome('ok', head + cleaned, flagged)

    def _calculate(self, args):
        rej = self._budget_check('calculate')
        if rej:
            return rej
        self.counts['calculate'] += 1
        try:
            res = safe_calc(args['expression'])
            self.other_ok += 1
            return Outcome('ok', res)
        except ToolError as e:
            return Outcome('error', f'Hesaplanamadı: {e}')

    def _recall(self, args):
        rej = self._budget_check('recall')
        if rej:
            return rej
        self.counts['recall'] += 1
        rows = self.runs.previous_results(self.task['id'], self.run['id'], limit=2)
        if not rows:
            return Outcome('ok', 'Bu görevin önceki başarılı çalıştırma sonucu yok (ilk çalıştırma).')
        parts = []
        flagged = 0
        for r in rows:
            when = time.strftime('%d.%m.%Y %H:%M', time.gmtime(r['finished_at'] or 0)) + ' UTC' if r.get('finished_at') else 'tarih yok'
            cleaned, f = util.sanitize_untrusted(r['result'] or '', 1400)
            flagged += f
            parts.append(f"Çalıştırma #{r['run_no']} ({when}):\n{cleaned}")
        self.flagged_total += flagged
        self.other_ok += 1
        return Outcome('ok', '\n\n'.join(parts), flagged)
