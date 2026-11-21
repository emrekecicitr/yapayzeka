CSS = r'''
/* ===== Görevler paneli (mevcut tasarım değişkenlerini kullanır) ===== */
.tk-side-wrap{padding:6px 12px 2px;flex-shrink:0}
.tk-side-btn{display:flex;align-items:center;gap:10px;width:100%;padding:10px 12px;border:1px solid var(--hairline-soft);background:transparent;color:var(--ivory);border-radius:var(--radius-md);font-family:var(--font-ui);font-size:14.5px;font-weight:600;cursor:pointer;text-align:left;transition:background-color .12s ease,border-color .12s ease}
.tk-side-btn:hover{background:var(--brass-dim);border-color:var(--hairline)}
.tk-side-btn svg,.tk-hdr-ic svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}
.tk-badge{margin-left:auto;min-width:20px;padding:1px 7px;border-radius:999px;background:var(--brass);color:#2B2520;font-size:12px;font-weight:700;text-align:center;line-height:18px}
.tk-badge[hidden]{display:none}
#tkPanel{position:absolute;inset:0;z-index:60;display:none;flex-direction:column;background:var(--obsidian);color:var(--ivory);font-family:var(--font-ui)}
#tkPanel.open{display:flex}
#tkPanel{color-scheme:dark}
.tk-top{display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid var(--hairline-soft);flex-shrink:0}
.tk-top h2{margin:0;font-size:20px;font-weight:700;letter-spacing:.01em;flex:1;min-width:0}
.tk-top h2 small{display:block;font-size:12px;font-weight:500;color:var(--stone);margin-top:2px}
.tk-ibtn{position:relative;display:inline-flex;align-items:center;justify-content:center;width:40px;height:40px;border-radius:var(--radius-md);border:1px solid var(--hairline-soft);background:transparent;color:var(--ivory);cursor:pointer;flex-shrink:0}
.tk-ibtn:hover{background:var(--brass-dim)}
.tk-ibtn svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.tk-ibtn .tk-badge{position:absolute;top:-6px;right:-6px;margin:0;min-width:18px;line-height:16px;font-size:11px;padding:0 5px}
.tk-btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:9px 14px;border-radius:var(--radius-md);border:1px solid var(--hairline);background:transparent;color:var(--ivory);font-family:var(--font-ui);font-size:14px;font-weight:600;cursor:pointer;min-height:38px;transition:background-color .12s ease,opacity .12s ease}
.tk-btn:hover{background:var(--brass-dim)}
.tk-btn:disabled{opacity:.45;cursor:not-allowed}
.tk-btn.primary{background:var(--brass);border-color:var(--brass);color:#2B2520}
.tk-btn.primary:hover{background:var(--brass-bright)}
.tk-btn.danger{color:#E58C80;border-color:rgba(181,86,75,.55)}
.tk-btn.danger:hover{background:rgba(181,86,75,.14)}
.tk-btn.sm{padding:6px 10px;font-size:13px;min-height:32px;border-radius:10px}
.tk-body{flex:1;overflow-y:auto;padding:16px 18px 28px;-webkit-overflow-scrolling:touch}
.tk-wrap{max-width:980px;margin:0 auto}
.tk-stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:12px}
.tk-stat{background:var(--obsidian-2);border:1px solid var(--hairline-soft);border-radius:var(--radius-md);padding:12px 14px}
.tk-stat b{display:block;font-size:24px;font-weight:700;line-height:1.1}
.tk-stat span{font-size:12.5px;color:var(--stone)}
.tk-stat.run b{color:var(--brass-bright)}.tk-stat.ok b{color:#6FC2AF}.tk-stat.bad b{color:#E58C80}
.tk-banner{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px;padding:10px 14px;border-radius:var(--radius-md);border:1px solid rgba(230,201,137,.5);background:rgba(201,169,106,.12);font-size:14px}
.tk-banner button{margin-left:auto}
.tk-next{font-size:13px;color:var(--stone);margin:0 2px 12px}
.tk-filters{display:flex;gap:6px;overflow-x:auto;padding-bottom:6px;margin-bottom:10px;scrollbar-width:none}
.tk-filters::-webkit-scrollbar{display:none}
.tk-fchip{flex-shrink:0;padding:6px 13px;border-radius:999px;border:1px solid var(--hairline-soft);background:transparent;color:var(--meta-bright);font-family:var(--font-ui);font-size:13.5px;cursor:pointer}
.tk-fchip.on{background:var(--brass-dim);border-color:var(--brass);color:var(--brass-bright);font-weight:600}
.tk-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px}
.tk-card{background:var(--obsidian-2);border:1px solid var(--hairline-soft);border-radius:var(--radius-md);padding:14px 15px;display:flex;flex-direction:column;gap:9px;min-width:0}
.tk-card.is-running{border-color:rgba(201,169,106,.65)}
.tk-card-top{display:flex;align-items:flex-start;gap:10px}
.tk-title{margin:0;font-size:16.5px;font-weight:700;line-height:1.3;flex:1;min-width:0;overflow-wrap:anywhere;cursor:pointer}
.tk-desc{margin:0;font-size:14px;color:var(--ink-dim);line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.tk-chip{display:inline-flex;align-items:center;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid var(--hairline);color:var(--meta-bright);white-space:nowrap;line-height:18px}
.tk-chip.st-running{color:var(--brass-bright);border-color:var(--brass)}
.tk-chip.st-completed{color:#6FC2AF;border-color:rgba(62,142,126,.7)}
.tk-chip.st-failed{color:#E58C80;border-color:rgba(181,86,75,.7)}
.tk-chip.st-awaiting_approval{color:#2B2520;background:var(--brass-bright);border-color:var(--brass-bright)}
.tk-chip.st-paused,.tk-chip.st-cancelled{color:var(--stone)}
.tk-meta{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;font-size:13px;color:var(--stone)}
.tk-times{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;font-size:13px;color:var(--time-gray)}
.tk-times b{display:block;font-weight:600;color:var(--stone);font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}
.tk-progress{height:6px;border-radius:999px;background:var(--hairline-soft);overflow:hidden}
.tk-progress>div{height:100%;background:linear-gradient(90deg,var(--brass),var(--brass-bright));border-radius:999px;transition:width .4s ease}
.tk-step{font-size:13px;color:var(--brass-bright)}
.tk-err{font-size:13px;color:#E8998F;background:rgba(181,86,75,.12);border:1px solid rgba(181,86,75,.4);border-radius:10px;padding:7px 10px;overflow-wrap:anywhere}
.tk-res{font-size:13px;color:var(--ink-dim);border-left:2px solid var(--brass);padding:2px 0 2px 10px;overflow-wrap:anywhere}
.tk-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
.tk-empty{text-align:center;color:var(--stone);padding:48px 16px;font-size:15px;line-height:1.6}
.tk-empty b{display:block;color:var(--ivory);font-size:18px;margin-bottom:6px}
/* modal */
.tk-ov{position:fixed;inset:0;z-index:1100;background:rgba(0,0,0,.62);display:none;align-items:center;justify-content:center;padding:18px;backdrop-filter:blur(3px)}
.tk-ov.open{display:flex}
.tk-modal{width:100%;max-width:620px;max-height:92vh;overflow:hidden;display:flex;flex-direction:column;background:var(--obsidian-2);border:1px solid var(--hairline);border-radius:var(--radius-xl);color:var(--ivory);font-family:var(--font-ui);box-shadow:0 24px 70px rgba(0,0,0,.55)}
.tk-modal.wide{max-width:820px}
.tk-modal.small{max-width:420px}
.tk-mh{display:flex;align-items:center;gap:10px;padding:16px 18px 12px;border-bottom:1px solid var(--hairline-soft)}
.tk-mh h3{margin:0;font-size:18px;flex:1;min-width:0;overflow-wrap:anywhere}
.tk-mb{padding:14px 18px;overflow-y:auto;flex:1;-webkit-overflow-scrolling:touch}
.tk-mf{display:flex;gap:8px;justify-content:flex-end;padding:12px 18px 16px;border-top:1px solid var(--hairline-soft);flex-wrap:wrap}
.tk-f{margin-bottom:13px}
.tk-f>label,.tk-lbl{display:block;font-size:13px;font-weight:600;color:var(--meta-bright);margin-bottom:5px}
.tk-f input[type=text],.tk-f input[type=number],.tk-f input[type=time],.tk-f input[type=datetime-local],.tk-f input[type=url],.tk-f select,.tk-f textarea{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:10px;border:1px solid var(--hairline);background:var(--obsidian);color:var(--ivory);font-family:var(--font-ui);font-size:15px;outline:none}
.tk-f textarea{min-height:92px;resize:vertical;line-height:1.45}
.tk-f input:focus,.tk-f select:focus,.tk-f textarea:focus{border-color:var(--brass)}
.tk-f .tk-hint{font-size:12.5px;color:var(--stone);margin-top:4px;line-height:1.4}
.tk-f .tk-ferr{font-size:12.5px;color:#E8998F;margin-top:4px}
.tk-f.bad input,.tk-f.bad select,.tk-f.bad textarea{border-color:var(--danger)}
.tk-row{display:flex;gap:10px;flex-wrap:wrap}.tk-row>*{flex:1;min-width:130px}
.tk-seg{display:flex;flex-wrap:wrap;gap:6px}
.tk-seg button{flex:1;min-width:104px;padding:9px 8px;border-radius:10px;border:1px solid var(--hairline);background:transparent;color:var(--meta-bright);font-family:var(--font-ui);font-size:13.5px;font-weight:600;cursor:pointer}
.tk-seg button.on{background:var(--brass-dim);border-color:var(--brass);color:var(--brass-bright)}
.tk-days{display:flex;gap:6px;flex-wrap:wrap}
.tk-days button{width:44px;height:38px;border-radius:10px;border:1px solid var(--hairline);background:transparent;color:var(--meta-bright);font-family:var(--font-ui);font-size:13px;font-weight:600;cursor:pointer}
.tk-days button.on{background:var(--brass);border-color:var(--brass);color:#2B2520}
.tk-sw,.tk-f>label.tk-sw{display:flex;align-items:center;gap:10px;cursor:pointer;font-size:14.5px;font-weight:500;color:var(--ivory);margin:0}
.tk-sw input{width:20px;height:20px;accent-color:var(--brass);flex-shrink:0}
.tk-adv{border:1px solid var(--hairline-soft);border-radius:var(--radius-md);padding:0 12px;margin-top:6px}
.tk-adv>summary{cursor:pointer;padding:11px 0;font-size:14px;font-weight:600;color:var(--meta-bright)}
.tk-sec{margin:0 0 16px}
.tk-sec>h4{margin:0 0 8px;font-size:12.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--stone)}
.tk-kv{display:grid;grid-template-columns:150px 1fr;gap:6px 12px;font-size:14px}
.tk-kv dt{color:var(--stone)}.tk-kv dd{margin:0;overflow-wrap:anywhere}
.tk-steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.tk-steps>li{border:1px solid var(--hairline-soft);border-radius:10px;padding:8px 10px;font-size:14px;background:var(--obsidian)}
.tk-steps .sh{display:flex;gap:10px;align-items:center}
.tk-steps .si{width:20px;text-align:center;flex-shrink:0;font-weight:700}
.tk-steps .s-completed .si{color:#6FC2AF}.tk-steps .s-failed .si{color:#E58C80}.tk-steps .s-running .si{color:var(--brass-bright)}.tk-steps .s-pending .si,.tk-steps .s-skipped .si{color:var(--stone)}
.tk-steps details{margin-top:6px;font-size:13px;color:var(--ink-dim)}
.tk-steps summary{cursor:pointer;color:var(--stone);font-size:12.5px}
.tk-steps .se{color:#E8998F;font-size:13px;margin-top:4px}
.tk-spin{display:inline-block;width:12px;height:12px;border:2px solid var(--hairline);border-top-color:var(--brass-bright);border-radius:50%;animation:tkspin .9s linear infinite;vertical-align:-1px}
@keyframes tkspin{to{transform:rotate(360deg)}}
.tk-md{font-size:15px;line-height:1.6;color:var(--ink-dim);overflow-wrap:anywhere}
.tk-md h3,.tk-md h4,.tk-md h5,.tk-md h6{color:var(--ivory);margin:14px 0 6px;line-height:1.3}
.tk-md h3{font-size:18px}.tk-md h4{font-size:16.5px}.tk-md h5,.tk-md h6{font-size:15px}
.tk-md p{margin:0 0 10px}.tk-md ul,.tk-md ol{margin:0 0 10px;padding-left:22px}
.tk-md code{background:var(--code-bg);color:var(--code-rose);padding:1px 5px;border-radius:5px;font-size:13.5px}
.tk-md pre{background:var(--code-bg);padding:10px 12px;border-radius:10px;overflow-x:auto}.tk-md pre code{padding:0}
.tk-md a{color:var(--brass-bright);text-decoration:underline}
.tk-md .tk-tw{overflow-x:auto;margin:0 0 10px}
.tk-md table{border-collapse:collapse;min-width:100%;font-size:14px}
.tk-md th,.tk-md td{border:1px solid var(--hairline);padding:6px 9px;text-align:left;vertical-align:top}
.tk-md th{background:var(--obsidian-3);color:var(--ivory)}
.tk-md hr{border:none;border-top:1px solid var(--hairline-soft);margin:12px 0}
.tk-hist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.tk-hist li{display:flex;align-items:center;gap:8px;flex-wrap:wrap;border:1px solid var(--hairline-soft);border-radius:10px;padding:8px 10px;font-size:13.5px}
.tk-hist li.sel{border-color:var(--brass)}
.tk-hist .hm{flex:1;min-width:150px;color:var(--ink-dim)}
.tk-src{margin:0;padding-left:20px;font-size:14px}.tk-src li{margin:3px 0;overflow-wrap:anywhere}
.tk-src a{color:var(--brass-bright)}
/* bildirim paneli */
.tk-np{position:absolute;right:14px;top:60px;width:min(380px,calc(100% - 28px));max-height:70%;overflow:auto;z-index:70;display:none;background:var(--obsidian-2);border:1px solid var(--hairline);border-radius:var(--radius-md);box-shadow:0 18px 50px rgba(0,0,0,.5)}
.tk-np.open{display:block}
.tk-np-h{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--hairline-soft);font-weight:700;font-size:14.5px}
.tk-np-h span{flex:1}
.tk-ni{display:block;width:100%;text-align:left;padding:10px 12px;border:none;border-bottom:1px solid var(--hairline-soft);background:transparent;color:var(--ivory);font-family:var(--font-ui);cursor:pointer}
.tk-ni:hover{background:var(--brass-dim)}
.tk-ni.unread{background:rgba(201,169,106,.08)}
.tk-ni b{display:block;font-size:14px;margin-bottom:2px}.tk-ni span{display:block;font-size:13px;color:var(--stone);overflow-wrap:anywhere}.tk-ni i{display:block;font-style:normal;font-size:11.5px;color:var(--time-gray);margin-top:3px}
.tk-toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:1300;background:var(--obsidian-3);border:1px solid var(--hairline);color:var(--ivory);padding:10px 16px;border-radius:12px;font-family:var(--font-ui);font-size:14px;max-width:92vw;box-shadow:0 10px 30px rgba(0,0,0,.45);display:none}
.tk-toast.open{display:block}.tk-toast.err{border-color:var(--danger);color:#F1B2A9}
.tk-chip.tk-agent{color:var(--brass-bright);border-color:var(--brass)}
.tk-turns{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:5px}
.tk-turns li{border:1px solid var(--hairline-soft);border-radius:8px;padding:6px 9px;font-size:13px;background:var(--obsidian-2)}
.tk-turns .th{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tk-turns .tn{width:20px;height:20px;border-radius:50%;background:var(--brass-dim);color:var(--brass-bright);font-size:11.5px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0}
.tk-turns .ta{color:var(--stone);overflow-wrap:anywhere;min-width:0;flex:1}
.tk-turns .tt{color:var(--ink-dim);font-size:12.5px;margin-top:3px;overflow-wrap:anywhere}
.tk-turns .tobs{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12.5px;color:var(--ink-dim);margin-top:5px;max-height:240px;overflow:auto}
.tk-turns li.t-rejected,.tk-turns li.t-error{border-color:rgba(181,86,75,.4)}
.tk-rv{margin-top:8px;border:1px solid var(--hairline-soft);border-radius:8px;padding:8px 10px;font-size:13px;background:var(--obsidian-2)}
.tk-rv.rv-ok{border-color:rgba(62,142,126,.6)}.tk-rv.rv-warn{border-color:rgba(230,201,137,.6)}.tk-rv.rv-bad{border-color:rgba(181,86,75,.5)}
.tk-rv .rvh{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tk-rv .rvn{color:var(--ink-dim);margin-top:5px;overflow-wrap:anywhere}
.tk-rv .rvs{margin-top:8px;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--stone)}
.tk-rv ul{margin:4px 0 0;padding-left:18px}.tk-rv li{margin:3px 0;overflow-wrap:anywhere;color:var(--ink-dim)}
@media (max-width:900px){.tk-list{grid-template-columns:1fr}}
@media (max-width:640px){
  .tk-top{padding:12px 12px}.tk-body{padding:12px 12px 24px}
  .tk-stats{grid-template-columns:repeat(3,1fr);gap:8px}.tk-stat{padding:10px 11px}.tk-stat b{font-size:21px}
  .tk-ov{padding:0;align-items:flex-end}
  .tk-modal,.tk-modal.wide,.tk-modal.small{max-width:100%;max-height:94vh;border-radius:20px 20px 0 0;border-bottom:none}
  .tk-modal.small{border-radius:20px}
  .tk-kv{grid-template-columns:1fr}.tk-kv dt{margin-top:6px}
  .tk-times{grid-template-columns:1fr 1fr}
  .tk-actions .tk-btn{flex:1 1 auto}
  .tk-top .tk-btn .tk-lbl-txt{display:none}
  .tk-f input[type=text],.tk-f input[type=number],.tk-f input[type=time],.tk-f input[type=datetime-local],.tk-f input[type=url],.tk-f select,.tk-f textarea{font-size:16px}
}
'''
