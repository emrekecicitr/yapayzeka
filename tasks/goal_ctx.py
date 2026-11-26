"""Hedef (orkestrasyon) baglaminin istemlere nasil girdigi. Yalnizca ince yardimcilar: executor/agent bunu ice aktarir, bu modul
agir hicbir seyi ice aktarmaz (dongusel bagimlilik olmasin).

Onceki alt gorev sonuclari goreve 'goal_context' durumu olarak verilir ve tetikleyici baglamla AYNI kanaldan, GUVENILMEYEN VERI olarak
(sinirlandirilmis, filtrelenmis, 'yalnizca veri' etiketiyle) modele girer; eylem onerilerini de kirletir (confirm_tainted gerekir).
Kosul (tetikleyici) baglami davranisi DEGISMEZ: yalnizca bu isaretle baslayan metin farkli etiket ve sinir alir.
"""
from . import config

GOAL_MARK = '[[HEDEF-BAGLAMI]]\n'


def split(text):
    """(hedef_baglami_mi, govde). Isaret govdeden cikarilir."""
    t = text or ''
    if t.startswith(GOAL_MARK):
        return True, t[len(GOAL_MARK):]
    return False, t


def cap(is_goal):
    return config.GOALS_CONTEXT_CHARS + 800 if is_goal else 1500
