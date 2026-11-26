"""Calisma klasoru icinde GUVENLI dosya islemleri (eylem yetenegi icin).

Savunulan saldirilar
- Yol gecisi: '../', mutlak yol, surucu harfi/ADS (':'), ters egik cizgi, bos ve '.' bilesenleri.
- Unicode: NFKC ile degisen (tam genislikte '．．', '／' vb.) adlar, kontrol/bicimlendirme karakterleri (bidi, sifir genislik) reddedilir.
- Windows: ayrilmis adlar (CON, NUL, COM1...), sonda nokta/bosluk, cok uzun ad ve cok derin yol.
- Sembolik baglar: POSIX'te dizin zinciri dir_fd + O_NOFOLLOW ile ACILIR (kontrol ile kullanim arasi yaris yok);
  dir_fd olmayan platformlarda lstat ile yurunur ve son yol realpath ile yeniden dogrulanir.
- Uzerine yazma: varsayilan ONLEMLI. Mevcut dosya yalnizca overwrite=True ile degistirilir; sembolik bag hedefi her zaman reddedilir.
- Atomik yazma: ayni dizinde gecici dosya (O_EXCL) + fsync + link (uzerine yazmadan) ya da replace (overwrite).
Hata mesajlari iç ayrinti/yol icermez.
"""
import errno
import hashlib
import os
import re
import stat
import unicodedata
import uuid


class ActionError(Exception):
    """Kullaniciya/modele gosterilebilir, ic ayrinti icermeyen Turkce hata."""


_RESERVED = {'con', 'prn', 'aux', 'nul', 'clock$'} | {f'com{i}' for i in range(0, 10)} | {f'lpt{i}' for i in range(0, 10)}
_NAME_OK = re.compile(r'^[\w .\-]+$')
MAX_PATH_CHARS = 200
MAX_COMPONENT = 80
MAX_DEPTH = 4

_O_DIR = getattr(os, 'O_DIRECTORY', 0)
_O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_HAS_DIRFD = bool(_O_DIR and _O_NOFOLLOW and os.open in getattr(os, 'supports_dir_fd', set())
                  and os.mkdir in getattr(os, 'supports_dir_fd', set()))


def secure_write_supported():
    """Guvenli yazma (dir_fd + O_NOFOLLOW) bu platformda var mi? Yoksa (Windows) dosya eylemleri YAZMAZ: kontrol ile kullanim
    arasinda sembolik bag yarisi olmayan bir yol yoktur, bu yuzden daha zayif bir yedek yol KULLANILMAZ (kapali-hata)."""
    return _HAS_DIRFD


def split_relpath(rel, exts):
    """Goreli yolu sozdizimsel olarak dogrular. Donus: bilesenler listesi (normallestirilmis). Hata: ActionError."""
    if not isinstance(rel, str) or not rel.strip():
        raise ActionError('Dosya yolu (path) gerekli.')
    if len(rel) > MAX_PATH_CHARS:
        raise ActionError(f'Dosya yolu en fazla {MAX_PATH_CHARS} karakter olabilir.')
    if unicodedata.normalize('NFKC', rel) != rel:
        raise ActionError('Dosya yolu geçersiz Unicode karakterler içeriyor (normalleştirilmiş biçimde yazın).')
    for ch in rel:
        cat = unicodedata.category(ch)
        if cat[0] == 'C' or cat in ('Zl', 'Zp') or (cat == 'Zs' and ch != ' '):
            raise ActionError('Dosya yolu denetim, bicimlendirme veya gorunmez karakter iceremez.')
    if rel.startswith('/') or '\\' in rel or ':' in rel or rel.startswith('~'):
        raise ActionError('Yalnızca çalışma klasörü içinde göreli yol kullanılabilir (mutlak yol, "\\", ":" ve "~" yasak).')
    parts = rel.split('/')
    if len(parts) > MAX_DEPTH:
        raise ActionError(f'Yol en fazla {MAX_DEPTH} düzey derinlikte olabilir.')
    for p in parts:
        if p in ('', '.', '..'):
            raise ActionError('Yolda boş, "." veya ".." bileşeni olamaz.')
        if p.startswith('.'):
            raise ActionError('Gizli (nokta ile başlayan) ad kullanılamaz.')
        if p != p.strip() or p.endswith('.'):
            raise ActionError('Ad boşluk ya da nokta ile bitemez.')
        if len(p) > MAX_COMPONENT:
            raise ActionError(f'Her ad en fazla {MAX_COMPONENT} karakter olabilir.')
        if not _NAME_OK.match(p):
            raise ActionError('Ad yalnızca harf, rakam, boşluk, "_", "-" ve "." içerebilir.')
        if p.split('.')[0].strip().lower() in _RESERVED:
            raise ActionError('Bu ad işletim sisteminde ayrılmıştır.')
    last = parts[-1].lower()
    if not any(last.endswith(e) for e in exts) or last in exts:
        raise ActionError('Dosya uzantısı izin verilenlerden biri olmalı: ' + ', '.join(exts) + '.')
    return parts


def workspace_root(configured, create=False):
    """Calisma klasorunun gercek (realpath) yolu. create=True ise yoksa 0700 ile olusturur."""
    root = os.path.realpath(os.path.expanduser(configured))
    if create and not os.path.isdir(root):
        try:
            os.makedirs(root, mode=0o700, exist_ok=True)
        except OSError:
            raise ActionError('Çalışma klasörü oluşturulamadı.')
    if not os.path.isdir(root):
        raise ActionError('Çalışma klasörü bulunamadı.')
    return root


def _open_dir_chain(root, parts, create):
    """POSIX: kok -> alt dizinler, her adim O_NOFOLLOW ile. Donus: son dizin fd'si."""
    dfd = os.open(root, os.O_RDONLY | _O_DIR)
    try:
        for p in parts:
            if create:
                try:
                    os.mkdir(p, 0o700, dir_fd=dfd)
                except FileExistsError:
                    pass
            try:
                nfd = os.open(p, os.O_RDONLY | _O_DIR | _O_NOFOLLOW, dir_fd=dfd)
            except FileNotFoundError:
                raise
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR, getattr(errno, 'EMLINK', -1)):
                    raise ActionError('Yol bir sembolik bağ ya da dizin olmayan bir öğe içeriyor; reddedildi.')
                raise
            os.close(dfd)
            dfd = nfd
        return dfd
    except BaseException:
        os.close(dfd)
        raise


def _bytes_hash(b):
    return hashlib.sha256(b).hexdigest()


def write_file(root, parts, data, overwrite=False):
    """data: bytes. Dosyayi calisma klasoru icine atomik yazar. Donus: yazilan bayt sayisi. Hata: ActionError."""
    if not isinstance(data, (bytes, bytearray)):
        raise ActionError('İçerik bayt olmalı.')
    try:
        if not _HAS_DIRFD:
            raise ActionError('Bu platformda güvenli dosya yazma (dir_fd ve O_NOFOLLOW) desteklenmiyor; dosya yazılmadı.')
        return _write_posix(root, parts, bytes(data), overwrite)
    except ActionError:
        raise
    except FileExistsError:
        raise ActionError('Hedef dosya zaten var (üzerine yazma izni verilmedi).')
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise ActionError('Disk dolu: dosya yazılamadı.')
        if e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise ActionError('Yazma izni yok: dosya yazılamadı.')
        if e.errno == errno.ENAMETOOLONG:
            raise ActionError('Dosya adı bu dosya sistemi için çok uzun.')
        raise ActionError('Dosya yazılamadı (giriş/çıkış hatası).')


def _write_posix(root, parts, data, overwrite):
    dfd = _open_dir_chain(root, parts[:-1], create=True)
    tmp = f'.tmp-{uuid.uuid4().hex}'
    tmp_made = False
    try:
        name = parts[-1]
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                raise ActionError('Hedef bir sembolik bağ; reddedildi.')
            if not stat.S_ISREG(st.st_mode):
                raise ActionError('Hedef normal bir dosya değil; reddedildi.')
            if not overwrite:
                raise ActionError('Hedef dosya zaten var (üzerine yazma izni verilmedi).')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600, dir_fd=dfd)
        tmp_made = True
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if overwrite:
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            tmp_made = False
        else:
            try:
                os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
            except FileExistsError:
                raise ActionError('Hedef dosya zaten var (üzerine yazma izni verilmedi).')
            except OSError as e:
                if e.errno in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK):
                    raise ActionError('Bu dosya sistemi atomik, üzerine yazmayan kayıt desteklemiyor.')
                raise
        try:
            os.fsync(dfd)
        except OSError:
            pass
        return len(data)
    finally:
        if tmp_made:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
        os.close(dfd)


def file_hash(root, parts):
    """Hedef dosya varsa sha256'si, yoksa None (sembolik bag ya da normal olmayan oge: ActionError). Cokme sonrasi dogrulama icin."""
    try:
        if _HAS_DIRFD:
            dfd = _open_dir_chain(root, parts[:-1], create=False)
            try:
                fd = os.open(parts[-1], os.O_RDONLY | _O_NOFOLLOW, dir_fd=dfd)
            finally:
                os.close(dfd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ActionError('Hedef normal bir dosya değil.')
                with os.fdopen(fd, 'rb', closefd=True) as f:
                    fd = None
                    return _bytes_hash(f.read(2 * 1024 * 1024))
            finally:
                if fd is not None:
                    os.close(fd)
        cur = root
        for p in parts[:-1]:
            cur = os.path.join(cur, p)
            if os.path.islink(cur):
                raise ActionError('Sembolik bağ.')
        target = os.path.join(cur, parts[-1])
        if os.path.islink(target):
            raise ActionError('Sembolik bağ.')
        with open(target, 'rb') as f:
            return _bytes_hash(f.read(2 * 1024 * 1024))
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ActionError('Sembolik bağ.')
        raise ActionError('Dosya okunamadı.')


bytes_hash = _bytes_hash
