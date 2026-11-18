"""Gorevler arayuzunu mevcut gomulu HTML'e (app.py icindeki HTML sabiti) en az mudahaleyle enjekte eder.

Mevcut HTML/CSS/JS bozulmaz: yeni CSS, panel, yan menu girdisi, ust menu girdisi ve betik, benzersiz capalara
EKLENIR. Capalardan biri bulunamazsa (HTML ileride degisirse) enjeksiyon sessizce atlanir ve uyari yazilir;
uygulamanin geri kalani aynen calisir.
"""
from .ui_css import CSS
from .ui_html import MENU_HTML, PANEL_HTML, SIDEBAR_HTML
from .ui_js import JS

MARKER = 'data-tasks-ui="1"'

ANCHORS = {
    'style_end': '</style>\n</head>',
    'main_open': '<div class="main-content">',
    'sidebar_list': '<div class="sidebar-list" id="sidebarList"></div>',
    'menu_settings': '<button class="hdr-item" id="hdrSettingsItem"',
    'body_end': '</body>',
}


def inject(html):
    """(yeni_html, uyarilar). Her capa tam olarak bir kez bulunmali, aksi halde ilgili parca atlanir."""
    if MARKER in html:
        return html, []
    warns = []
    out = html

    def once(key):
        n = out.count(ANCHORS[key])
        if n != 1:
            warns.append(f"capa '{key}' {n} kez bulundu (1 bekleniyordu)")
            return False
        return True

    # Panel ve betik birbirine bagimlidir: ikisi birlikte eklenir ya da hic eklenmez.
    if not (once('style_end') and once('main_open') and once('body_end')):
        return html, warns
    out = out.replace(ANCHORS['style_end'], '\n' + CSS + ANCHORS['style_end'], 1)
    out = out.replace(ANCHORS['main_open'], ANCHORS['main_open'] + '\n' + PANEL_HTML, 1)
    out = out.replace(ANCHORS['body_end'], '<script>\n' + JS + '\n</script>\n' + ANCHORS['body_end'], 1)
    if once('sidebar_list'):
        out = out.replace(ANCHORS['sidebar_list'], SIDEBAR_HTML + ANCHORS['sidebar_list'], 1)
    if once('menu_settings'):
        out = out.replace(ANCHORS['menu_settings'], MENU_HTML + ANCHORS['menu_settings'], 1)
    return out, warns
