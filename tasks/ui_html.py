PANEL_HTML = r'''
<section id="tkPanel" aria-label="Görevler" data-tasks-ui="1">
  <div class="tk-top">
    <button class="tk-ibtn" type="button" id="tkBack" aria-label="Sohbete dön" title="Sohbete dön"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></button>
    <h2>Görevler<small id="tkSub">Otonom görev merkezi</small></h2>
    <button class="tk-ibtn" type="button" id="tkBell" aria-label="Bildirimler" title="Bildirimler" aria-expanded="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 01-3.4 0"/></svg><span class="tk-badge" id="tkBellBadge" hidden>0</span></button>
    <button class="tk-btn primary" type="button" id="tkNew"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg><span class="tk-lbl-txt">Yeni görev</span></button>
  </div>
  <div class="tk-np" id="tkNotifPanel" role="dialog" aria-label="Bildirimler"></div>
  <div class="tk-body">
    <div class="tk-wrap">
      <div id="tkBannerBox"></div>
      <div class="tk-stats" id="tkStats"></div>
      <div class="tk-next" id="tkNext"></div>
      <div class="tk-filters" id="tkFilters">
        <button type="button" class="tk-fchip on" data-filter="all">Tümü</button>
        <button type="button" class="tk-fchip" data-filter="active">Aktif</button>
        <button type="button" class="tk-fchip" data-filter="completed">Tamamlanan</button>
        <button type="button" class="tk-fchip" data-filter="failed">Başarısız</button>
        <button type="button" class="tk-fchip" data-filter="paused">Pasif</button>
      </div>
      <div class="tk-list" id="tkList" aria-live="polite"></div>
    </div>
  </div>
  <div class="tk-ov" id="tkFormOv"><div class="tk-modal" role="dialog" aria-modal="true" aria-labelledby="tkFormTitle">
    <div class="tk-mh"><h3 id="tkFormTitle">Yeni görev</h3><button type="button" class="tk-ibtn" data-close="tkFormOv" aria-label="Kapat">&times;</button></div>
    <div class="tk-mb" id="tkFormBody"></div>
    <div class="tk-mf"><span id="tkFormErr" class="tk-ferr" style="flex:1;align-self:center;color:#E8998F;font-size:13px"></span><button type="button" class="tk-btn" data-close="tkFormOv">Vazgeç</button><button type="button" class="tk-btn primary" id="tkFormSave">Kaydet</button></div>
  </div></div>
  <div class="tk-ov" id="tkDetailOv"><div class="tk-modal wide" role="dialog" aria-modal="true" aria-labelledby="tkDetailTitle">
    <div class="tk-mh"><h3 id="tkDetailTitle">Görev</h3><button type="button" class="tk-ibtn" data-close="tkDetailOv" aria-label="Kapat">&times;</button></div>
    <div class="tk-mb" id="tkDetailBody"></div>
  </div></div>
  <div class="tk-ov" id="tkConfirmOv"><div class="tk-modal small" role="alertdialog" aria-modal="true" aria-labelledby="tkConfirmMsg">
    <div class="tk-mb"><p id="tkConfirmMsg" style="margin:4px 0 0;font-size:15.5px;line-height:1.5"></p></div>
    <div class="tk-mf"><button type="button" class="tk-btn" id="tkConfirmNo">Vazgeç</button><button type="button" class="tk-btn danger" id="tkConfirmYes">Sil</button></div>
  </div></div>
  <div class="tk-toast" id="tkToast" role="status" aria-live="polite"></div>
</section>
'''

SIDEBAR_HTML = r'''<div class="tk-side-wrap"><button type="button" class="tk-side-btn" id="tkSidebarBtn" onclick="window.TasksUI&&TasksUI.open()"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h9"/></svg><span>Görevler</span><span class="tk-badge" id="tkSideBadge" hidden>0</span></button></div>
    '''

MENU_HTML = r'''<button class="hdr-item" id="hdrTasksItem" type="button" role="menuitem" onclick="try{closeHdrMenu();}catch(e){}window.TasksUI&&TasksUI.open()"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h9"/></svg><span>Görevler</span></button>
              '''
