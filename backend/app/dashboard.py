"""Local, read-only dashboard: Overview, Matchups and Waiver Advisor over the Python engine."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import lru_cache
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .phase1 import ROOT, load_inputs, rank_league
from .simulator import simulate
from .deterministic import forecast
from .waiver import classify_plan, evaluate_singles
from .yahoo_transactions import ACTUAL_NAME, resolve_evidence
from .streaming import optimize_week, optimize_horizon, SearchConfig, StrategyPolicy
from .advisor import AdvisorConfig, advise
from .presentation import present
from .trades import TradeConfig, trade_lab, verification as trade_verification
from .presentation import trade_briefs


HTML = r'''<!doctype html>
<html lang="en-CA"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Big Baller League · Dashboard</title><style>
:root{color-scheme:dark;--bg:#0c1117;--surface:#131a22;--surface-2:#18212b;--surface-3:#212c38;--line:#232e3a;--line-2:#324152;--ink:#edf1f5;--ink-2:#c4cdd7;--muted:#93a1b0;--faint:#738292;--accent:#5aa9ff;--accent-soft:rgba(90,169,255,.12);--rival:#c39aff;--good:#55c493;--bad:#ea7b72;--warn:#e6b85c;--r:14px;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--ink);background:var(--bg)}
*{box-sizing:border-box}body{margin:0;background:var(--bg)}body.loading main{opacity:.55;transition:opacity .2s}
h1,h2,h3{margin:0;font-weight:650;letter-spacing:-.01em;color:var(--ink)}h2{font-size:1.08rem}p{color:var(--muted);line-height:1.5}button{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
select{background:var(--surface-2);color:var(--ink);border:1px solid var(--line-2);border-radius:9px;padding:7px 10px;font:inherit;max-width:100%}
input[type=range]{accent-color:var(--accent);width:100%}
.app-head{position:sticky;top:0;z-index:20;background:rgba(12,17,23,.9);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.bar{max-width:1320px;margin:auto;padding:10px 24px;display:flex;align-items:center;gap:28px}
.brand{display:flex;align-items:center;gap:10px;min-width:0}.logo{width:32px;height:32px;border-radius:9px;background:linear-gradient(135deg,#2c6cb4,#5aa9ff);display:grid;place-items:center;font-weight:750;font-size:.78rem;color:#fff;flex:none}.league{font-weight:650;white-space:nowrap}.sub{font-size:.75rem;color:var(--faint);white-space:nowrap}
.tabs{display:flex;gap:4px;overflow-x:auto}.tabs a{padding:8px 14px;border-radius:9px;text-decoration:none;color:var(--muted);font-weight:550;white-space:nowrap}.tabs a:hover{color:var(--ink);background:var(--surface-2)}.tabs a[aria-current=page]{color:var(--ink);background:var(--surface-3)}
.head-right{margin-left:auto;display:flex;gap:8px;align-items:center}
.hpill,.chip-btn{border:1px solid var(--line-2);background:var(--surface);border-radius:999px;padding:5px 12px;font-size:.8rem;color:var(--ink-2);cursor:pointer;white-space:nowrap;display:inline-flex;gap:7px;align-items:center}.hpill::before{content:'';width:7px;height:7px;border-radius:50%;background:var(--faint)}.hpill.good::before{background:var(--good)}.hpill.warn::before{background:var(--warn)}.hpill.bad::before{background:var(--bad)}
.icon-btn{width:34px;height:34px;border-radius:9px;border:1px solid var(--line-2);background:var(--surface);display:grid;place-items:center;cursor:pointer;color:var(--ink-2);flex:none}.icon-btn:hover,.chip-btn:hover,.hpill:hover{background:var(--surface-2)}
main{max-width:1320px;margin:auto;padding:24px}.view{display:flex;flex-direction:column;gap:20px}.view[hidden]{display:none}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:20px 22px;min-width:0}
.card-head{display:flex;justify-content:space-between;align-items:flex-end;gap:12px 20px;flex-wrap:wrap;margin-bottom:14px}.card-head p{margin:2px 0 0;font-size:.85rem}
.banner{border-radius:10px;padding:10px 14px;font-size:.88rem;border:1px solid}.banner[hidden]{display:none}.banner.warn{background:#251f12;border-color:#5e4a1d;color:#f1d79b}.banner.bad{background:#2a1515;border-color:#6b2a2a;color:#f6b7b0}#error{max-width:1272px;margin:16px auto 0}
.you{font-size:.66rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);background:var(--accent-soft);border-radius:999px;padding:2px 8px;margin-left:8px;vertical-align:2px}
.hero{background:linear-gradient(160deg,#172331,#131a22 65%)}.hero h1{font-size:1.75rem}.hero p{margin:4px 0 18px;font-size:.88rem}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.kpi{background:var(--surface-2);border:1px solid var(--line);border-radius:12px;padding:14px 16px;text-align:left;min-width:0}.kpi .k{font-size:.78rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.kpi .v{font-size:1.95rem;font-weight:700;line-height:1.15;margin-top:4px;font-variant-numeric:tabular-nums}.kpi .s{font-size:.78rem;color:var(--faint);margin-top:4px}
button.kpi{cursor:pointer;width:100%}button.kpi:hover{border-color:var(--line-2);background:var(--surface-3)}.kpi.up .v{color:var(--good)}.kpi.down .v{color:var(--bad)}.kpi .go{color:var(--accent)}
.path{display:grid;grid-template-columns:repeat(18,minmax(0,1fr));gap:6px}
.pw{border:1px solid var(--line);border-radius:9px;padding:7px 3px;text-align:center;cursor:pointer;background:var(--surface-2);min-width:0}.pw .n{font-size:.68rem;color:var(--faint)}.pw .sc{font-weight:650;font-variant-numeric:tabular-nums;font-size:.9rem}.pw .op{font-size:.64rem;color:var(--faint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pw.win{background:rgba(85,196,147,.13);border-color:rgba(85,196,147,.32)}.pw.loss{background:rgba(234,123,114,.11);border-color:rgba(234,123,114,.3)}.pw.final{border-width:2px}.pw.now{outline:2px solid var(--accent);outline-offset:1px}.pw:hover{filter:brightness(1.18)}
.keyrow{display:flex;gap:16px;font-size:.75rem;color:var(--faint);margin-top:10px;flex-wrap:wrap}.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
table{border-collapse:collapse;width:100%}
.tbl th{font-size:.72rem;font-weight:600;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none}.tbl th:hover{color:var(--ink-2)}.tbl th[aria-sort]{color:var(--ink)}.tbl th[aria-sort=ascending]::after{content:' ↑'}.tbl th[aria-sort=descending]::after{content:' ↓'}
.tbl td{padding:11px 12px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums;white-space:nowrap}.tbl .num{text-align:right}.tbl tr.me td{background:var(--accent-soft)}.tbl tr.me td:first-child{box-shadow:inset 3px 0 var(--accent)}.tbl tr.focus td{background:var(--surface-2)}
.seed{font-weight:700;color:var(--ink-2)}.team-btn{background:none;border:0;padding:0;cursor:pointer;text-align:left;font-weight:560}.team-btn:hover{color:var(--accent)}.tbl .actual{color:var(--muted)}
.wbar{position:relative;height:6px;border-radius:3px;background:var(--surface-3);width:90px;display:inline-block;vertical-align:middle;margin-left:12px}.wbar i{position:absolute;top:0;bottom:0;border-radius:3px}.wbar::after{content:'';position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--line-2)}
.cutlabel td{padding:3px 12px;font-size:.68rem;color:var(--faint);text-transform:uppercase;letter-spacing:.07em;border-bottom:1px dashed var(--line-2);background:none!important;box-shadow:none!important}
.str-grid{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:20px;align-items:start}.heat-wrap{overflow-x:auto}
.heat{border-collapse:separate;border-spacing:4px;min-width:660px}.heat th{font-size:.76rem;color:var(--muted);font-weight:650;padding:2px 4px 6px;text-align:center;cursor:pointer;white-space:nowrap}.heat th small{display:block;font-size:.62rem;color:var(--faint);font-weight:500}.heat th.sorted{color:var(--ink)}.heat th.sorted::after{content:' ↓'}
.heat th.tcol,.heat td.tname{text-align:left;position:sticky;left:0;background:var(--surface);z-index:1}.heat td.tname{max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.88rem;padding-right:10px}.heat td.tname.me{color:var(--accent);font-weight:650}.heat td.tname.focus{color:var(--ink);font-weight:650}
.hc{width:100%;min-width:54px;height:38px;border:0;border-radius:8px;cursor:pointer;font-weight:650;font-variant-numeric:tabular-nums;font-size:.92rem;color:var(--ink)}.hc:hover{box-shadow:inset 0 0 0 1px rgba(255,255,255,.4)}tr.me .hc{box-shadow:inset 0 0 0 1px rgba(90,169,255,.6)}.hc.sel,tr.me .hc.sel{box-shadow:inset 0 0 0 2px var(--ink)}
.legend{display:flex;align-items:center;gap:8px;font-size:.74rem;color:var(--faint)}.legend .grad{width:130px;height:8px;border-radius:4px;background:linear-gradient(90deg,rgba(226,104,96,.85),rgba(140,155,170,.15),rgba(63,178,128,.85))}
.profile{background:var(--surface-2);border:1px solid var(--line);border-radius:12px;padding:16px}.profile select{width:100%;margin:6px 0 10px}.plegend{font-size:.74rem;color:var(--faint);margin-bottom:6px}.plegend i{display:inline-block;width:2px;height:10px;background:var(--accent);margin-right:6px;vertical-align:-1px}
.prow{display:grid;grid-template-columns:44px 1fr 30px 30px;gap:10px;align-items:center;padding:6px;border-radius:8px;cursor:pointer;width:100%;border:0;background:none;text-align:left}.prow:hover,.prow.sel{background:var(--surface-3)}.prow .lab{font-size:.78rem;font-weight:650;color:var(--ink-2)}.prow .sv{text-align:right;font-weight:650;font-variant-numeric:tabular-nums;font-size:.88rem}.prow .rk{text-align:right;font-size:.74rem;color:var(--faint)}
.ptrack{position:relative;height:8px;background:var(--surface);border-radius:4px}.ptrack i{position:absolute;left:0;top:0;bottom:0;border-radius:4px}.ptrack b{position:absolute;top:-3px;bottom:-3px;width:2px;margin-left:-1px;background:var(--accent);border-radius:1px}
.pdetail{margin-top:12px;padding-top:12px;border-top:1px solid var(--line);font-size:.84rem;display:flex;flex-direction:column;gap:5px}.pdetail .ph{font-weight:650;color:var(--ink)}.kv{display:flex;justify-content:space-between;gap:12px;color:var(--muted)}.kv b{color:var(--ink-2);font-weight:550;text-align:right;font-variant-numeric:tabular-nums}
.h2h{margin-top:12px;padding-top:12px;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:2px}.h2h button{display:flex;justify-content:space-between;background:none;border:0;padding:5px 6px;border-radius:7px;cursor:pointer;font-size:.85rem;color:var(--ink-2)}.h2h button:hover{background:var(--surface-3)}
.mx-top{display:flex;align-items:center;gap:10px 16px;flex-wrap:wrap}.stepper{display:flex;align-items:center;gap:6px}.icon-btn:disabled{opacity:.4;cursor:default}.games{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin-top:14px}
.game{border:1px solid var(--line);background:var(--surface-2);border-radius:10px;padding:9px 11px;text-align:left;cursor:pointer;display:flex;flex-direction:column;gap:1px;font-size:.8rem;min-width:0}.game:hover{border-color:var(--line-2)}.gl{display:flex;justify-content:space-between;gap:8px}.gl span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.gl b{font-variant-numeric:tabular-nums}.gs{font-size:.68rem;color:var(--faint);margin-top:2px}.game.sel{border-color:var(--accent);background:var(--accent-soft)}.game .mn{color:var(--accent);font-weight:600}
.vs{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:center;gap:20px;padding:4px 0 18px}.side{min-width:0}.side.b{text-align:right}.tn{font-size:1.3rem;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.side.a .tn{color:var(--accent)}.side.b .tn{color:var(--rival)}.side .tr{font-size:.78rem;color:var(--faint)}
.score{font-size:2.7rem;font-weight:750;font-variant-numeric:tabular-nums;letter-spacing:-.02em;text-align:center;line-height:1.1}.st{font-size:.7rem;color:var(--faint);text-align:center;text-transform:uppercase;letter-spacing:.08em}
.groups{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:10px}.grp{background:var(--surface-2);border-radius:10px;padding:10px 12px;font-size:.86rem;min-width:0}.grp .gk{font-size:.7rem;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.grp .gv{color:var(--ink);font-weight:600;margin-top:2px}.grp.close .gk{color:var(--warn)}
.crow{display:grid;grid-template-columns:70px minmax(90px,1fr) minmax(180px,2.2fr) minmax(90px,1fr) 64px;grid-template-areas:"cl va m vb bd";gap:14px;align-items:center;padding:10px 4px;border-top:1px solid var(--line)}
.cl{grid-area:cl;font-weight:650;font-size:.88rem}.cl small{display:block;font-size:.66rem;color:var(--faint);font-weight:500}.va{grid-area:va}.vb{grid-area:vb;text-align:right}.mcol{grid-area:m}.band{grid-area:bd}
.va,.vb{font-variant-numeric:tabular-nums;font-size:1rem;color:var(--ink-2)}.va.won{color:var(--accent);font-weight:700}.vb.won{color:var(--rival);font-weight:700}.va.lost,.vb.lost{color:var(--muted)}.va .sub,.vb .sub{display:block;font-size:.7rem;color:var(--faint);font-weight:400}
.mbar{position:relative;height:10px}.mbar::before{content:'';position:absolute;left:0;right:0;top:4px;height:2px;background:var(--surface-3)}.mbar::after{content:'';position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--line-2)}.mbar i{position:absolute;top:1px;height:8px;border-radius:4px}.mlab{font-size:.72rem;color:var(--faint);text-align:center;margin-top:3px;font-variant-numeric:tabular-nums}
.band{font-size:.7rem;border-radius:999px;padding:3px 0;text-align:center;border:1px solid var(--line);color:var(--faint)}.band.close{color:var(--warn);border-color:#5e4a1d;background:#231d10}.band.edge{color:var(--ink-2);border-color:var(--line-2)}.crow.close{background:linear-gradient(90deg,transparent,rgba(230,184,92,.05),transparent)}
.scrim{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:39}.drawer{position:fixed;top:0;right:0;bottom:0;width:min(440px,100vw);background:var(--surface);border-left:1px solid var(--line);z-index:40;overflow-y:auto;padding:20px 22px 40px;box-shadow:-20px 0 40px rgba(0,0,0,.4)}.drawer[hidden],.scrim[hidden]{display:none}
.dhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}.drawer section{margin-bottom:24px}.drawer .eyebrow{margin-bottom:8px}
.seg{display:flex;background:var(--surface-2);border:1px solid var(--line-2);border-radius:10px;padding:3px;gap:3px}.seg button{flex:1;border:0;background:none;padding:7px 6px;border-radius:7px;cursor:pointer;color:var(--muted);font-size:.85rem}.seg button[aria-pressed=true]{background:var(--surface-3);color:var(--ink)}
.field{display:flex;flex-direction:column;gap:6px;margin:14px 0 6px}.field label{font-size:.82rem;color:var(--muted)}.field[hidden]{display:none}
.hl{display:flex;justify-content:space-between;gap:14px;padding:9px 0;border-bottom:1px solid var(--line);font-size:.85rem}.hl .hk{color:var(--muted);flex:none}.hl .hv{color:var(--ink-2);text-align:right}.hl .hv.warn{color:var(--warn)}.hl .hv.bad{color:var(--bad)}
.drawer details{border-top:1px solid var(--line);padding-top:12px;margin-top:12px}.drawer details>summary{cursor:pointer;color:var(--ink-2);font-weight:550}.drawer details p{font-size:.83rem;margin:8px 0}
.tip{position:fixed;z-index:60;pointer-events:none;background:#090d12;border:1px solid var(--line-2);border-radius:8px;padding:8px 10px;font-size:.8rem;color:var(--ink-2);max-width:280px;box-shadow:0 8px 24px rgba(0,0,0,.45);white-space:pre-line}.tip[hidden]{display:none}
.scroll{overflow:auto}.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;margin:18px 0}.controls{display:flex;gap:14px;align-items:center;flex-wrap:wrap}.note{font-size:.85rem;color:var(--warn)}.bad{color:var(--bad)}details summary{cursor:pointer}
.mini{font-size:.85rem}.mini th,.mini td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}.mini th{color:var(--faint);font-weight:500;font-size:.8rem}.mini td{color:var(--ink-2)}.dtable th,.dtable td{padding:8px 10px}
#advisorDiagnostics h3{margin:18px 0 8px;font-size:1rem}
.advisor{--card:var(--surface)}.tile-value.sm{font-size:1.15rem;line-height:1.3}.trade-search{padding:18px 20px 16px;margin-top:6px}.ts-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}.ts-title{font-size:1.1rem;font-weight:600;color:var(--ink);margin:4px 0}.ts-sub{font-size:.86rem;color:var(--muted);margin:0;max-width:62ch}.ts-actions{display:flex;align-items:center;gap:12px;margin-left:auto}.ts-dirty{font-size:.82rem;color:var(--warn)}.ts-dirty.tone-warn{color:var(--bad)}.ts-grid{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:2px 32px}@media (max-width:760px){.ts-grid{grid-template-columns:1fr}}#view-trades .adv-head:has(.headline:empty){display:none}.ts-group.wide{grid-column:1/-1}.ts-label{display:flex;align-items:center;gap:10px;font-size:.74rem;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--faint);margin:16px 0 8px}.ts-hint{font-weight:400;letter-spacing:0;text-transform:none;color:var(--faint)}.ts-links{display:inline-flex;gap:10px;margin-left:auto}.linkbtn{background:none;border:0;padding:0;color:var(--accent);font:inherit;font-size:.78rem;letter-spacing:0;text-transform:none;cursor:pointer}.linkbtn:hover{text-decoration:underline}.toggles label{position:relative;display:inline-flex;align-items:center;gap:7px;cursor:pointer;user-select:none;transition:border-color .15s,background .15s,color .15s}.toggles label:hover{border-color:var(--line-2);color:var(--ink-2)}.toggles input{position:absolute;opacity:0;width:1px;height:1px;pointer-events:none}.toggles label::before{content:"";width:12px;height:12px;flex:none;border-radius:4px;border:1px solid var(--line-2);background:var(--surface-2)}.toggles label:has(input:checked){border-color:var(--accent);background:var(--accent-soft);color:var(--ink)}.toggles label:has(input:checked)::before{background:var(--accent);border-color:var(--accent);box-shadow:inset 0 0 0 2px var(--surface)}.toggles label:has(input:focus-visible){outline:2px solid var(--accent);outline-offset:2px}.toggles.protect label:has(input:checked){border-color:var(--warn);background:rgba(230,184,92,.12)}.toggles.protect label:has(input:checked)::before{background:var(--warn);border-color:var(--warn)}.ts-sub-chip{color:var(--faint);font-size:.78rem}.ts-sub-chip:empty{display:none}.btn.primary{background:var(--accent);border-color:var(--accent);color:#07121e;font-weight:600;min-width:132px;display:inline-flex;align-items:center;justify-content:center;gap:8px}.btn.primary:hover{background:var(--accent);filter:brightness(1.08)}.btn.primary:disabled{opacity:.55;cursor:not-allowed}.btn.primary.pulse{box-shadow:0 0 0 4px var(--accent-soft)}.spin{width:14px;height:14px;border:2px solid rgba(7,18,30,.3);border-top-color:#07121e;border-radius:50%;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.ts-status{margin-top:16px;padding-top:12px;border-top:1px solid var(--line)}.ts-bar{position:relative;height:4px;border-radius:4px;background:var(--surface-3);overflow:hidden}.ts-bar span{position:absolute;top:0;bottom:0;width:35%;border-radius:4px;background:var(--accent);animation:tsbar 1.3s ease-in-out infinite}@keyframes tsbar{from{left:-35%}to{left:100%}}.ts-status p{font-size:.85rem;color:var(--muted);margin:8px 0 0}#tradeResults.stale{opacity:.4;pointer-events:none;transition:opacity .2s}.skel{height:132px;margin:18px 0;border:1px solid var(--line);border-radius:14px;background:linear-gradient(90deg,var(--surface) 0%,var(--surface-2) 50%,var(--surface) 100%);background-size:200% 100%;animation:skel 1.4s ease-in-out infinite}@keyframes skel{to{background-position:-200% 0}}@media (prefers-reduced-motion:reduce){.spin,.ts-bar span,.skel{animation:none}}#tradeProtect{margin-top:4px}#tradeProtect label{cursor:pointer}
.adv-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}.adv-actions{display:flex;flex-direction:column;align-items:flex-end;gap:6px}
.headline{font-size:1.35rem;font-weight:650;color:var(--ink);margin:2px 0 8px}.chips{display:flex;gap:8px;flex-wrap:wrap}.mchip{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:.85rem;color:var(--muted)}.mchip b{color:var(--ink);font-weight:600}
.datanote{font-size:.85rem;color:var(--muted);margin:10px 0 0}.fine{font-size:.82rem;color:var(--faint);margin:8px 0 0;line-height:1.45}
.btn{background:var(--surface-3);color:var(--ink);border:1px solid var(--line-2);border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer}.btn:hover{background:var(--surface-3)}.btn:disabled{opacity:.6;cursor:wait}
.tone-good{color:var(--good)}.tone-bad{color:var(--bad)}.tone-warn{color:var(--warn)}
.section-label{font-size:.9rem;font-weight:600;color:var(--ink-2);margin:18px 0 8px}
.compare{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:4px 18px 14px;margin:18px 0}.link{cursor:pointer;color:var(--ink);border-bottom:1px dotted #5b7286}.link:hover{color:#fff}.holdrow td{color:var(--muted)}
.dtable{font-size:.88rem;white-space:normal}.dtable th{background:none;cursor:default;color:var(--faint);font-weight:500;text-align:left;border-bottom:1px solid var(--line)}.dtable td{text-align:left;border-bottom:1px solid var(--surface-3);color:#d6e0e8}.dtable .num,.dtable th.num{text-align:right;white-space:nowrap}.narrow{max-width:760px}.plan-brief{display:block;font-size:.78rem;color:var(--faint);margin-top:2px}.dtable tr:hover{background:none}.dtable tr.flip td{background:#13261b}
.scard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 22px;margin:18px 0;scroll-margin-top:16px}.scard.hold{border-style:dashed}
.scard-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.eyebrow{font-size:.75rem;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}.scard h3{font-size:1.45rem;margin:4px 0 0;color:var(--ink)}
.pill{border-radius:999px;padding:4px 12px;font-size:.8rem;white-space:nowrap;border:1px solid var(--line-2);color:var(--muted)}.pill.tone-warn{border-color:#6b5420;background:#2a2312}.pill.tone-good{border-color:#245c3b;background:#13261b}.pill.tone-bad{border-color:#6b2a2a;background:#2a1414}
.consider{font-size:.9rem;color:var(--ink-2);margin:10px 0 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0 4px}.tile{background:var(--bg);border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:3px}.tile-label{font-size:.8rem;color:var(--faint)}.tile-value{font-size:1.75rem;font-weight:650;color:var(--ink);line-height:1.2}.tile-value.tone-good{color:var(--good)}.tile-value.tone-bad{color:var(--bad)}.tile-value.tone-warn{color:var(--warn)}.tile-sub{font-size:.8rem;color:var(--muted);line-height:1.4;white-space:pre-line}
.tl-row{display:grid;grid-template-columns:96px 1fr;gap:14px;align-items:start;padding:5px 0}.tl-date{background:var(--bg);border-radius:8px;padding:7px 0;text-align:center;font-size:.85rem;color:var(--ink-2)}.tl-moves{display:flex;flex-direction:column;gap:10px}
.tl-line{display:flex;gap:8px;align-items:center;flex-wrap:wrap;line-height:1.6}.sym{width:17px;height:17px;border-radius:50%;display:inline-grid;place-items:center;font-size:.78rem;font-weight:700;border:1.5px solid currentColor;flex:none}.add .sym{color:var(--good)}.drop .sym{color:var(--bad)}
.pname{color:var(--ink);font-weight:500}.drop .pname{color:var(--ink-2)}.pmeta{font-size:.8rem;color:var(--faint)}.chip{font-size:.72rem;padding:1px 8px;border-radius:999px;background:var(--surface-3);color:#aebfcd}.chip-stream{background:#2a2312;color:var(--warn)}
.cond{font-size:.82rem;color:var(--muted);margin:10px 0 0}
.risks{border-top:1px solid var(--line);margin-top:16px;padding-top:12px;display:flex;flex-direction:column;gap:8px}.risk{display:flex;gap:10px;align-items:flex-start;font-size:.9rem;line-height:1.45;color:var(--ink-2)}.risk strong{color:var(--ink);font-weight:600}.risk-icon{width:18px;height:18px;border-radius:50%;display:inline-grid;place-items:center;font-size:.75rem;font-weight:700;border:1.5px solid currentColor;flex:none;margin-top:2px}
details.why,details.audit{margin-top:14px}details.why>summary{list-style:none;text-align:center;border:1px solid var(--line-2);border-radius:999px;padding:9px;color:var(--ink-2)}details.why>summary:hover{background:var(--surface-2)}details.why>summary::-webkit-details-marker,details.audit>summary::-webkit-details-marker{display:none}details.why>summary::after{content:' ▾'}details.why[open]>summary::after{content:' ▴'}
details.audit>summary{list-style:none;font-size:.85rem;color:var(--faint);padding:4px 0}details.audit>summary::before{content:'▸ '}details.audit[open]>summary::before{content:'▾ '}details.audit h4{margin:12px 0 4px;color:var(--ink-2)}details.audit p{margin:4px 0;font-size:.85rem}
.why-body section{margin-top:18px}.why-body h4{margin:0 0 6px;color:var(--ink);font-size:1rem}.why-body .wk{margin:8px 0 2px;color:var(--ink-2);font-size:.9rem}ul.plain{margin:4px 0;padding-left:18px;color:var(--ink-2);line-height:1.6;font-size:.92rem}
.advanced{margin-top:22px}.advanced summary{color:var(--muted)}
@media(max-width:640px){.scard{padding:16px}.tl-row{grid-template-columns:1fr;gap:6px}.tl-date{justify-self:start;padding:4px 10px}.adv-actions{align-items:flex-start}.scard h3{font-size:1.25rem}}
.scard{scroll-margin-top:84px}.advisor .adv-head{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:18px 22px}
@media(max-width:1080px){.str-grid{grid-template-columns:1fr}.path{grid-template-columns:repeat(9,minmax(0,1fr))}.games{grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:760px){.bar{flex-wrap:wrap;gap:8px 12px;padding:10px 16px}.tabs{order:3;width:100%}#sourceChip,.sub{display:none}main{padding:14px}.card{padding:16px}.hero h1{font-size:1.4rem}.kpi .v{font-size:1.5rem}.path{grid-template-columns:repeat(6,minmax(0,1fr))}.groups{grid-template-columns:1fr}.wbar{display:none}.tbl th,.tbl td{padding:9px 8px}.tbl td:nth-child(2){max-width:150px;overflow:hidden;text-overflow:ellipsis}
.crow{grid-template-columns:52px 1fr 1fr;grid-template-areas:"cl va vb" "bd m m";gap:6px 12px}.vs{gap:10px}.tn{font-size:1rem}.score{font-size:2rem}.heat td.tname{max-width:110px}}
.trade-filters>summary{cursor:pointer;color:var(--ink-2);font-size:.88rem;margin-top:14px;width:max-content;max-width:100%}.xchg{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);gap:10px;margin:14px 0 4px}.xside{background:var(--bg);border-radius:10px;padding:12px 14px}.xside>.tile-label{display:block;margin-bottom:6px}.xp{display:flex;justify-content:space-between;gap:10px;align-items:baseline;padding:3px 0;flex-wrap:wrap}.xp b{font-weight:600;color:var(--ink)}.xp small{color:var(--faint);font-size:.78rem;font-variant-numeric:tabular-nums}.xarrow{align-self:center;color:var(--faint);font-size:1.1rem}.tw-why{color:var(--ink-2);margin:12px 0 4px;max-width:85ch}.tnote{border-left:3px solid var(--warn);background:#221d10;padding:8px 12px;border-radius:0 8px 8px 0;font-size:.88rem;color:var(--ink-2);margin:10px 0}.tmoves{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:10px 0 0}.tmoves .tile-label{margin-right:4px}.tmoves .mchip{font-size:.82rem}.tmoves .mchip.cond{border-color:#6b5420}.tmeta{font-size:.8rem;color:var(--faint);margin:10px 0 0}.mkbar{display:grid;grid-template-columns:96px minmax(0,1fr) 40px;gap:10px;align-items:center;font-size:.85rem;margin:6px 0;max-width:520px}.mtrack{display:block;height:8px;border-radius:4px;background:var(--surface-3);overflow:hidden}.mkbar i{display:block;height:100%;background:var(--accent)}.mkbar.send i{background:var(--warn)}@media(max-width:640px){.xchg{grid-template-columns:1fr}.xarrow{display:none}.tmetrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body>
<header class="app-head"><div class="bar">
<div class="brand"><span class="logo" aria-hidden="true">BBL</span><div><div class="league" id="leagueName">Big Baller League</div><div class="sub">2026–27 · Yahoo H2H · 9 categories</div></div></div>
<nav class="tabs" aria-label="Sections"><a href="#overview">Overview</a><a href="#matchups">Matchups</a><a href="#waivers">Waiver Advisor</a><a href="#trades">Trade Lab</a></nav>
<div class="head-right"><button id="sourceChip" class="chip-btn" title="Projection source">Blend</button><button id="healthPill" class="hpill" title="Yahoo data health">Yahoo</button><button id="settingsBtn" class="icon-btn" aria-label="Settings and data health"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button></div>
</div></header>
<div id="error" class="banner bad" role="alert" hidden></div>
<main>
<section id="view-overview" class="view" aria-label="League overview">
<div class="card hero"><div class="eyebrow">Your team</div><h1 id="heroTeam">Loading…</h1><p id="heroNote"></p><div class="kpis" id="kpis"></div></div>
<div class="card"><div class="card-head"><div><h2>Your season path</h2><p>Category score for each of your 18 regular-season matchups. Select a week to open it.</p></div></div><div id="path" class="path"></div>
<div class="keyrow"><span><i class="sw" style="background:rgba(85,196,147,.4)"></i>Projected win</span><span><i class="sw" style="background:rgba(234,123,114,.4)"></i>Projected loss</span><span><i class="sw" style="outline:2px solid var(--accent)"></i>Current week</span><span>Thick border = final Yahoo result</span></div></div>
<div class="card"><div class="card-head"><div><h2>League standings</h2><p id="standNote"></p></div></div><div class="scroll"><table id="standings" class="tbl"><thead></thead><tbody></tbody></table></div></div>
<div class="card"><div class="card-head"><div><h2>Category strengths</h2><p id="scope"></p></div><div class="controls"><select id="period" aria-label="Comparison period"></select><span class="legend">Weakest <span class="grad"></span> Strongest</span></div></div>
<div class="str-grid"><div class="heat-wrap"><table id="heatmap" class="heat"><thead></thead><tbody></tbody></table><p class="fine">Scores are league-relative: best team 100, worst 0, 50 when all are tied. They rank teams; they are not probabilities. Select a cell for the projected total and league range.</p></div><aside id="profile" class="profile" aria-live="polite"></aside></div></div>
</section>
<section id="view-matchups" class="view" aria-label="Matchup explorer" hidden>
<div class="card"><div class="mx-top"><h2>Matchups</h2><div class="stepper"><button id="prevWeek" class="icon-btn" aria-label="Previous week">‹</button><select id="week" aria-label="Week"></select><button id="nextWeek" class="icon-btn" aria-label="Next week">›</button></div><span id="weekDates" class="fine" style="margin:0"></span></div><div id="weekGames" class="games"></div></div>
<div class="card" id="matchupCard"></div>
</section>
<section id="view-waivers" class="view" aria-label="Waiver Advisor" hidden>
<div id="waiverWarn" class="banner warn" hidden></div>
<section class="advisor"><div class="adv-head"><div><p id="advisorHeadline" class="headline"></p><div id="advisorContext"></div></div><div class="adv-actions"><button id="yahooRefresh" class="btn">Refresh Yahoo</button><span id="yahooBrief" class="fine"></span></div></div><div id="advisorResults"></div><details class="panel advanced"><summary>Advanced settings and search diagnostics</summary><div class="controls" style="margin-top:12px"><label>Planning horizon <select id="advisorHorizon"><option value="auto" selected>Automatic</option><option value="1">1 scoring period</option><option value="2">2 scoring periods</option><option value="3">3 scoring periods</option></select></label><button id="advisorRun" class="btn">Re-run</button></div><p id="yahooStatus" class="note"></p><div id="advisorDiagnostics"></div></details><p class="fine">Read-only: nothing is sent to Yahoo. Projections are deterministic; source checks are sensitivity tests, not probabilities.</p></section>
</section>
<section id="view-trades" class="view" aria-label="Trade Lab" hidden>
<section class="advisor">
<div class="panel trade-search" aria-label="Trade search"><div class="ts-head"><div><div class="eyebrow">Trade search</div><h2 class="ts-title">Trades that help you more than the market thinks</h2><p class="ts-sub">Every viable package with the chosen teams is replayed against your schedule. All teams and types take about a minute.</p></div><div class="ts-actions"><span id="tradeDirty" class="ts-dirty" aria-live="polite"></span><button id="tradeApply" class="btn primary" type="button">Search</button></div></div>
<details id="tradeFilters" class="trade-filters"><summary>Filters · <span id="tradeScope">all teams, all trade types</span></summary><div class="ts-grid"><div class="ts-group"><div class="ts-label">Trade partners<span class="ts-links"><button type="button" class="linkbtn" data-set="tradePartner:1">All</button><button type="button" class="linkbtn" data-set="tradePartner:0">None</button></span></div><div id="tradePartner" class="chips toggles"><span class="fine">Loading teams…</span></div></div>
<div class="ts-group"><div class="ts-label">Trade types</div><div id="tradeTypes" class="chips toggles"><label class="mchip"><input type="checkbox" value="1-1" checked>1-for-1<span class="ts-sub-chip"></span></label><label class="mchip"><input type="checkbox" value="2-1" checked>2-for-1<span class="ts-sub-chip">you send two</span></label><label class="mchip"><input type="checkbox" value="1-2" checked>1-for-2<span class="ts-sub-chip">you get two</span></label><label class="mchip"><input type="checkbox" value="2-2" checked>2-for-2<span class="ts-sub-chip"></span></label></div></div>
<div class="ts-group wide"><div class="ts-label">Untouchable players<span class="ts-hint">never offered or dropped</span><span class="ts-links"><button type="button" class="linkbtn" data-set="tradeProtect:0">Clear</button></span></div><div id="tradeProtect" class="chips toggles protect"><span class="fine">Loading your roster…</span></div></div></div></details>
<div id="tradeStatus" class="ts-status" role="status" hidden><div class="ts-bar"><span></span></div><p id="tradeStatusText"></p></div></div>
<div class="adv-head"><div><p id="tradeHeadline" class="headline"></p><div id="tradeContext"></div></div></div><div id="tradeResults" aria-busy="false"></div>
<details class="panel advanced"><summary>Search diagnostics and assumptions</summary><div id="tradeDiagnostics"></div></details>
<p class="fine">Read-only: no trade is proposed or sent to Yahoo. Category-win changes are deterministic projections, not acceptance or win probabilities. Market value combines Yahoo preseason ADP and FantasyPros consensus rankings; model value is a projection estimate. Neither predicts what a manager will accept.</p></section>
</section>
</main>
<div id="scrim" class="scrim" hidden></div>
<aside id="settings" class="drawer" aria-label="Settings and data health" hidden>
<div class="dhead"><h2>Settings and data</h2><button id="settingsClose" class="icon-btn" aria-label="Close settings">✕</button></div>
<section><div class="eyebrow">Projection source</div><div class="seg" id="sourceSeg"><button data-v="blend">Blend</button><button data-v="hashtag">Hashtag</button><button data-v="leaguewinner">LeagueWinner</button></div>
<select id="source" hidden><option value="blend">Blended</option><option value="hashtag">Hashtag only</option><option value="leaguewinner">LeagueWinner only</option></select>
<div class="field" id="weightBox"><label for="weight">Hashtag share of the blend: <b id="weightLabel">50%</b></label><input id="weight" type="range" min="0" max="100" value="50"></div>
<p class="fine">Applies to standings, strengths, matchups and the Waiver Advisor.</p></section>
<section><div class="eyebrow">Data health</div><div id="health"></div></section>
<details><summary>How the numbers work</summary><div id="method"></div></details>
<details><summary>Technical details</summary><div id="tech"></div></details>
</aside>
<div id="tip" class="tip" role="tooltip" hidden></div>
<script>
const cats=[['fg_pct','FG%'],['ft_pct','FT%'],['three_ptm','3PTM'],['pts','PTS'],['reb','REB'],['ast','AST'],['stl','STL'],['blk','BLK'],['turnovers','TO']],catName=Object.fromEntries(cats),VIEWS=['overview','matchups','waivers','trades'];
const e=id=>document.getElementById(id);let data=null,yahoo=null,view='overview',sortKey='projected_seed',asc=true,focus=null,focusCat=null,heatSort=null,selWeek=null,selKey=null,advisorStale=true,tradesStale=true;
function cell(row,value,tag='td'){const x=document.createElement(tag);x.textContent=value;row.appendChild(x);return x}
function head(table,labels){const row=document.createElement('tr');for(const [,label] of labels)cell(row,label,'th');table.tHead.replaceChildren(row)}
function record(v){return v.join('–')}
const pct1=v=>(100*v).toFixed(1)+'%',me=()=>data.my_team_id,team=id=>data.teams.find(t=>t.team_id===id),tname=id=>team(id)?.team_name??'Team '+id;
const fmt=(key,v,d=0)=>v==null?'—':key.endsWith('_pct')?(100*v).toFixed(1)+'%':v.toLocaleString('en-CA',{minimumFractionDigits:d,maximumFractionDigits:d});
const weight=()=>{const s=e('source').value;return s==='hashtag'?1:s==='leaguewinner'?0:Number(e('weight').value)/100};
const sourceText=()=>{const s=e('source').value;return s==='hashtag'?'Hashtag only':s==='leaguewinner'?'LeagueWinner only':`Blend · ${Math.round(weight()*100)}% Hashtag`};
const dfmt=s=>new Date(s+'T12:00:00').toLocaleDateString('en-CA',{month:'short',day:'numeric'});
const when=s=>{const d=new Date(s);return isNaN(d)?s:d.toLocaleString('en-CA',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})};
const ago=m=>m<60?Math.round(m)+' min':m<1440?Math.floor(m/60)+' h '+Math.round(m%60)+' min':Math.floor(m/1440)+' d';
const weekOf=n=>data.weeks.find(w=>w.week===n),keyOf=m=>m.week+'-'+m.teams.join('-');
function currentWeek(){const t=new Date().toLocaleDateString('en-CA');return(data.weeks.find(w=>w.end_date>=t)||data.weeks.at(-1)).week}
function myGame(week){return data.weekly_matchups.find(m=>m.week===week&&m.teams.includes(me()))}
function orient(m,id){const flip=m.teams[1]===id,o=x=>x==='W'?'L':x==='L'?'W':x;return{a:flip?m.teams[1]:m.teams[0],b:flip?m.teams[0]:m.teams[1],score:flip?[m.score[1],m.score[0],m.score[2]]:m.score,outcomes:m.outcomes&&Object.fromEntries(Object.entries(m.outcomes).map(([k,v])=>[k,flip?o(v):v]))}}
const score=s=>s[2]?record(s):`${s[0]}–${s[1]}`;
// Margin bands describe projected gaps only; they are display labels, not probabilities.
const LIMITS={fg_pct:[.005,.015],ft_pct:[.01,.03]};
const gapOf=(k,a,b)=>k.endsWith('_pct')?Math.abs(a-b):Math.abs(a-b)/Math.max(1e-9,(a+b)/2);
function band(k,a,b){const[c,l]=LIMITS[k]||[.05,.12],g=gapOf(k,a,b);return g<=c?'close':g<=l?'edge':'clear'}
const margin=(k,a,b)=>k.endsWith('_pct')?(100*Math.abs(a-b)).toFixed(1)+' pp':`${Math.abs(a-b).toFixed(1)} (${(100*gapOf(k,a,b)).toFixed(1)}%)`;
function heatBg(s){if(s==null)return'transparent';const d=(s-50)/50,a=Math.abs(d);return a<.04?'rgba(140,155,170,.13)':`rgba(${d>0?'63,178,128':'226,104,96'},${(.12+.68*a).toFixed(3)})`}
const barColor=s=>s==null?'transparent':s>=60?'var(--good)':s<=40?'var(--bad)':'#8d9aa8';
function kv(k,v){const x=el('div','kv');x.append(el('span',null,k),el('b',null,v));return x}

function show(){const v=(location.hash||'#overview').slice(1);view=VIEWS.includes(v)?v:'overview';for(const x of VIEWS){e('view-'+x).hidden=x!==view;const a=document.querySelector(`.tabs a[href="#${x}"]`);x===view?a.setAttribute('aria-current','page'):a.removeAttribute('aria-current')}if(!data)return;if(view==='matchups'){e('week').value=selWeek;renderWeek()}if(view==='waivers'&&advisorStale){advisorStale=false;loadAdvisor()}if(view==='trades'&&tradesStale){tradesStale=false;loadTrades()}}
function openMatch(m){selWeek=m.week;selKey=keyOf(m);if(location.hash==='#matchups')show();else location.hash='#matchups';scrollTo(0,0)}
function setFocus(id){focus=id===me()?null:id;renderStandings();renderHeat();renderProfile()}

function kpi(k,v,s,btn){const x=el(btn?'button':'div','kpi');x.append(el('div','k',k),el('div','v',v),el('div','s',s));return x}
function renderOverview(){const t=team(me()),cw=currentWeek(),g=myGame(cw),done=data.actual_completed_weeks;e('heroTeam').textContent=t.team_name;e('heroNote').textContent=`${sourceText()} · ${done?`Weeks 1–${done} final from Yahoo; Weeks ${done+1}–18 projected`:'No completed weeks yet; all 18 weeks are projected'}`;
const items=[kpi('Projected seed','#'+t.projected_seed,`of ${data.teams.length} teams · top 6 make the playoffs`),kpi('Season category W–L–T',record(t.projected_record),done?`Actual so far ${record(t.actual_record)}`:'Projected over 162 category decisions'),kpi('Category win %',pct1(t.projected_win_pct),'Share of decisions won, ties count half. Not a probability.')];
if(g){const o=orient(g,me()),w=weekOf(cw);let sub='Final Yahoo result';if(g.status!=='actual'){const n=cats.filter(([k])=>o.outcomes[k]==='T'||band(k,g.totals[o.a].categories[k],g.totals[o.b].categories[k])==='close').length;sub=`${n} close categor${n===1?'y':'ies'} · ${dfmt(w.start_date)}–${dfmt(w.end_date)}`}const x=kpi(`Week ${cw} vs ${tname(o.b)}`,score(o.score),sub,true);x.classList.add(o.score[0]>o.score[1]?'up':o.score[0]<o.score[1]?'down':'flat');x.querySelector('.s').append(el('span','go',' · Open matchup →'));x.onclick=()=>openMatch(g);items.push(x)}
e('kpis').replaceChildren(...items);renderPath(cw)}
function renderPath(cw){const box=e('path');box.replaceChildren();for(const w of data.weeks){const g=myGame(w.week),b=el('button','pw');b.append(el('div','n','Wk '+w.week));if(!g){b.disabled=true;b.append(el('div','sc','—'));box.append(b);continue}const o=orient(g,me()),s=o.score;b.classList.add(s[0]>s[1]?'win':s[0]<s[1]?'loss':'tie');if(g.status==='actual')b.classList.add('final');if(w.week===cw)b.classList.add('now');b.append(el('div','sc',score(s)),el('div','op',tname(o.b)));b.dataset.tip=`Week ${w.week} · ${dfmt(w.start_date)}–${dfmt(w.end_date)}${w.is_extended?' (14 days)':''}\nvs ${tname(o.b)}\n${g.status==='actual'?'Final':'Projected'} ${record(s)}`;b.setAttribute('aria-label',b.dataset.tip.replaceAll('\n',', '));b.onclick=()=>openMatch(g);box.append(b)}}

function renderStandings(){const tb=e('standings'),done=data.actual_completed_weeks,cols=[['projected_seed','#'],['team_name','Team'],['projected_record','Season W–L–T'],['projected_win_pct','Category win %'],['actual_record','Actual to date']],hr=document.createElement('tr');
for(const [k,l] of cols){const th=el('th',k==='projected_seed'||k==='team_name'?null:'num',l);th.tabIndex=0;if(sortKey===k)th.setAttribute('aria-sort',asc?'ascending':'descending');const go=()=>{if(sortKey===k)asc=!asc;else{sortKey=k;asc=k==='projected_seed'||k==='team_name'}renderStandings()};th.onclick=go;th.onkeydown=ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();go()}};if(k==='projected_record')th.title='Actual completed weeks plus projected remaining weeks';if(k==='actual_record')th.title='Final Yahoo results only';hr.append(th)}tb.tHead.replaceChildren(hr);
const rows=[...data.teams].sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(Array.isArray(x)){x=x[0]+.5*x[2];y=y[0]+.5*y[2]}const c=typeof x==='string'?x.localeCompare(y):x-y;return(asc?1:-1)*c||a.projected_seed-b.projected_seed});
const dev=Math.max(.01,...data.teams.map(t=>Math.abs(t.projected_win_pct-.5))),body=tb.tBodies[0],seeded=sortKey==='projected_seed'&&asc;body.replaceChildren();
for(const t of rows){const tr=body.insertRow();if(t.team_id===me())tr.className='me';else if(t.team_id===focus)tr.className='focus';tr.insertCell().append(el('span','seed',t.projected_seed));const nc=tr.insertCell(),b=el('button','team-btn',t.team_name);b.title='Show this team in the category profile';b.onclick=()=>{setFocus(t.team_id);e('profile').scrollIntoView({behavior:'smooth',block:'nearest'})};nc.append(b);if(t.team_id===me())nc.append(el('span','you','You'));
const rc=tr.insertCell();rc.className='num';rc.textContent=record(t.projected_record);const wc=tr.insertCell(),bar=el('span','wbar'),i=document.createElement('i'),d=(t.projected_win_pct-.5)/dev*50;wc.className='num';i.style.left=(d>=0?50:50+d)+'%';i.style.width=Math.abs(d)+'%';i.style.background=d>=0?'var(--good)':'var(--bad)';bar.append(i);wc.append(document.createTextNode(pct1(t.projected_win_pct)),bar);
const ac=tr.insertCell();ac.className='num actual';ac.textContent=done?record(t.actual_record):'—';
if(seeded&&t.projected_seed===6&&rows.length>6){const c=body.insertRow();c.className='cutlabel';const td=c.insertCell();td.colSpan=5;td.textContent='Playoff line · top 6'}}
e('standNote').textContent=done?`Season W–L–T is ${done} final Yahoo week${done>1?'s':''} plus projected Weeks ${done+1}–18. Actual to date is Yahoo only. Select a team to profile it.`:'No weeks are final yet, so each season record is a projection over 162 category decisions. Select a team to profile it.'}

function fillPeriods(){const p=e('period'),prior=p.value;p.replaceChildren(new Option(data.strength_scope[0].toUpperCase()+data.strength_scope.slice(1),'regular'),...data.weeks.filter(w=>data.periods[w.week]).map(w=>new Option(`Week ${w.week} · ${dfmt(w.start_date)}–${dfmt(w.end_date)}`,w.week)));p.value=data.periods[prior]?prior:'regular'}
const periodData=()=>data.periods[e('period').value]||data.periods.regular,dp=()=>e('period').value==='regular'?0:1;
function renderHeat(){const P=periodData(),t=e('heatmap'),hr=document.createElement('tr'),th0=el('th','tcol'+(heatSort?'':' sorted'),'Team');th0.title='Sort by projected seed';th0.onclick=()=>{heatSort=null;renderHeat()};hr.append(th0);
for(const [k,l] of cats){const th=el('th',heatSort===k?'sorted':null,l);th.append(el('small',null,k==='turnovers'?'lower wins':k.endsWith('_pct')?'makes ÷ att.':'total'));th.title=`Sort by ${l} strength`;th.onclick=()=>{heatSort=heatSort===k?null:k;renderHeat()};hr.append(th)}t.tHead.replaceChildren(hr);
const sc=(id,k)=>P.strengths[id][k].score??-1,rows=[...data.teams].sort((a,b)=>(heatSort?sc(b.team_id,heatSort)-sc(a.team_id,heatSort):0)||a.projected_seed-b.projected_seed),body=t.tBodies[0];body.replaceChildren();
for(const tm of rows){const id=tm.team_id,tr=body.insertRow();if(id===me())tr.className='me';const nc=tr.insertCell();nc.className='tname'+(id===me()?' me':id===focus?' focus':'');nc.textContent=tm.team_name;nc.title=tm.team_name;
for(const [k,l] of cats){const s=P.strengths[id][k],b=el('button','hc',s.score==null?'—':String(Math.round(s.score)));b.style.background=heatBg(s.score);if(id===(focus||me())&&k===focusCat)b.classList.add('sel');b.dataset.tip=`${tm.team_name} · ${l}\nStrength ${s.score==null?'—':s.score.toFixed(1)} · rank ${s.rank??'—'} of ${data.teams.length}\nProjected ${fmt(k,s.value,dp())}${k==='turnovers'?' (lower is better)':''}`;b.setAttribute('aria-label',b.dataset.tip.replaceAll('\n',', '));b.onclick=()=>{focus=id===me()?null:id;focusCat=k;renderStandings();renderHeat();renderProfile()};tr.insertCell().append(b)}}
e('scope').textContent=data.actual_completed_weeks?`Totals cover remaining Weeks ${data.actual_completed_weeks+1}–18; completed-week stat totals are not captured.`:'Projected totals for the selected period, ranked across all ten teams.'}
function renderProfile(){const id=focus||me(),P=periodData(),S=P.strengths[id],M=P.strengths[me()],box=e('profile'),sel=document.createElement('select');for(const t of [...data.teams].sort((a,b)=>a.projected_seed-b.projected_seed))sel.add(new Option(`${t.projected_seed}. ${t.team_name}${t.team_id===me()?' (you)':''}`,t.team_id));sel.value=id;sel.setAttribute('aria-label','Team to profile');sel.onchange=()=>setFocus(sel.value);box.replaceChildren(el('div','eyebrow','Team profile'),sel);
if(id!==me()){const lg=el('div','plegend');lg.append(el('i'),document.createTextNode('Your team’s score'));box.append(lg)}
for(const [k,l] of cats){const s=S[k],r=el('button','prow'+(k===focusCat?' sel':'')),tr=el('div','ptrack'),i=document.createElement('i');i.style.width=(s.score==null?0:Math.max(s.score,1.5))+'%';i.style.background=barColor(s.score);tr.append(i);if(id!==me()&&M[k].score!=null){const m=document.createElement('b');m.style.left=M[k].score+'%';tr.append(m)}r.append(el('span','lab',l),tr,el('span','sv',s.score==null?'—':String(Math.round(s.score))),el('span','rk',s.rank?'#'+s.rank:'—'));r.setAttribute('aria-label',`${l}: strength ${s.score==null?'unavailable':Math.round(s.score)}, rank ${s.rank??'—'}`);r.onclick=()=>{focusCat=focusCat===k?null:k;renderHeat();renderProfile()};box.append(r)}
const d=el('div','pdetail');if(focusCat){const k=focusCat,s=S[k],ranked=data.teams.map(t=>({t,s:P.strengths[t.team_id][k]})).filter(x=>x.s.rank!=null).sort((a,b)=>a.s.rank-b.s.rank);d.append(el('div','ph',`${catName[k]} · rank ${s.rank??'—'} of ${data.teams.length}`),kv('Projected total',fmt(k,s.value,dp())+(k==='turnovers'?' (lower is better)':'')));
if(k.endsWith('_pct')){const v=P.totals[id].shooting_volumes,[m,a]=k==='fg_pct'?['fgm','fga']:['ftm','fta'];d.append(kv(k==='fg_pct'?'FGM / FGA':'FTM / FTA',`${fmt(m,v[m],1)} / ${fmt(a,v[a],1)}`))}
if(ranked.length)d.append(kv('League best',`${ranked[0].t.team_name} · ${fmt(k,ranked[0].s.value,dp())}`),kv('League worst',`${ranked.at(-1).t.team_name} · ${fmt(k,ranked.at(-1).s.value,dp())}`));d.append(kv('Strength score',s.score==null?'—':s.score.toFixed(1)+' of 100'));if(id!==me())d.append(kv('Your team',`${fmt(k,M[k].value,dp())} · rank ${M[k].rank??'—'}`))}else d.append(el('span','fine','Select a category for its projected total, rank and league range.'));box.append(d);
if(id!==me()){const meets=data.weekly_matchups.filter(m=>m.teams.includes(me())&&m.teams.includes(id));if(meets.length){const h=el('div','h2h');h.append(el('div','eyebrow','Your matchups with this team'));for(const m of meets){const o=orient(m,me()),b=el('button');b.append(el('span',null,`Week ${m.week} · ${m.status==='actual'?'final':'projected'}`),el('b',o.score[0]>o.score[1]?'tone-good':o.score[0]<o.score[1]?'tone-bad':null,record(o.score)));b.onclick=()=>openMatch(m);h.append(b)}box.append(h)}}}

function fillWeeks(){const s=e('week');s.replaceChildren(...data.weeks.map(w=>new Option(`Week ${w.week}${w.is_extended?' · 14 days':''}`,w.week)));if(selWeek==null||!weekOf(selWeek))selWeek=currentWeek();s.value=selWeek}
function renderWeek(){selWeek=Number(e('week').value);const w=weekOf(selWeek);e('weekDates').textContent=`${dfmt(w.start_date)} – ${dfmt(w.end_date)} · ${w.days} days${selWeek===currentWeek()?' · current week':''}`;e('prevWeek').disabled=selWeek<=data.weeks[0].week;e('nextWeek').disabled=selWeek>=data.weeks.at(-1).week;
const games=data.weekly_matchups.filter(m=>m.week===selWeek).sort((a,b)=>b.teams.includes(me())-a.teams.includes(me()));if(!games.some(m=>keyOf(m)===selKey))selKey=games.length?keyOf(games[0]):null;const box=e('weekGames');box.replaceChildren();
for(const m of games){const mine=m.teams.includes(me()),o=orient(m,mine?me():m.teams[0]),b=el('button','game'+(keyOf(m)===selKey?' sel':'')),l1=el('div','gl'),l2=el('div','gl');b.setAttribute('aria-pressed',String(keyOf(m)===selKey));l1.append(el('span',mine?'mn':null,tname(o.a)+(mine?' (you)':'')),el('b',null,String(o.score[0])));l2.append(el('span',null,tname(o.b)),el('b',null,String(o.score[1])));b.append(l1,l2,el('div','gs',(m.status==='actual'?'Final':'Projected')+(o.score[2]?` · ${o.score[2]} tied`:'')));b.onclick=()=>{selKey=keyOf(m);renderWeek()};box.append(b)}renderMatchup()}
function grp(label,list,cls){const g=el('div','grp'+(cls?' '+cls:''));g.append(el('div','gk',label),el('div','gv',list.length?list.join(' · '):'None'));return g}
function side(id,cls){const s=el('div','side '+cls),t=team(id),n=el('div','tn',t.team_name);n.title=t.team_name;s.append(n,el('div','tr',`${id===me()?'You · ':''}Projected seed ${t.projected_seed} · ${record(t.projected_record)}`));return s}
function renderMatchup(){const m=data.weekly_matchups.find(x=>keyOf(x)===selKey),card=e('matchupCard');card.replaceChildren();if(!m){card.append(el('p',null,'No matchup data for this week.'));return}const o=orient(m,m.teams.includes(me())?me():m.teams[0]),vs=el('div','vs'),mid=el('div');mid.append(el('div','score',record(o.score)),el('div','st',m.status==='actual'?'Final · Yahoo':'Projected category score'));vs.append(side(o.a,'a'),mid,side(o.b,'b'));card.append(vs);
if(m.status==='actual'){card.append(el('p','fine','Final category score from Yahoo. Category stat totals for completed weeks are not captured, so the breakdown is unavailable.'));return}
const T=id=>m.totals[id],A=T(o.a),B=T(o.b),G={a:[],close:[],b:[]},rows=el('div');
for(const [k,l] of cats){const a=A.categories[k],b=B.categories[k],out=o.outcomes[k],bd=out==='T'?'close':band(k,a,b);(bd==='close'?G.close:out==='W'?G.a:G.b).push(l);
const r=el('div','crow '+bd),lab=el('div','cl',l),va=el('div','va '+(out==='W'?'won':out==='L'?'lost':'')),vb=el('div','vb '+(out==='L'?'won':out==='W'?'lost':''));if(k==='turnovers')lab.append(el('small',null,'lower wins'));va.append(fmt(k,a,1));vb.append(fmt(k,b,1));va.title=String(a);vb.title=String(b);
if(k.endsWith('_pct')){const[x,y]=k==='fg_pct'?['fgm','fga']:['ftm','fta'];va.append(el('span','sub',`${fmt(x,A.shooting_volumes[x],1)} / ${fmt(y,A.shooting_volumes[y],1)}`));vb.append(el('span','sub',`${fmt(x,B.shooting_volumes[x],1)} / ${fmt(y,B.shooting_volumes[y],1)}`))}
const mc=el('div','mcol'),bar=el('div','mbar'),i=document.createElement('i'),lean=(LIMITS[k]||[.05,.12])[1],w=out==='T'?0:Math.max(2,Math.min(1,gapOf(k,a,b)/(lean*2.5))*50);i.style.width=w+'%';i.style.background=out==='W'?'var(--accent)':'var(--rival)';i.style[out==='W'?'right':'left']='50%';bar.append(i);mc.append(bar,el('div','mlab',out==='T'?'Tie':out==='W'?'◂ '+margin(k,a,b):margin(k,a,b)+' ▸'));
r.append(lab,va,mc,vb,el('span','band '+bd,bd==='close'?'Close':bd==='edge'?'Edge':'Clear'));rows.append(r)}
const gs=el('div','groups'),short=id=>id===me()?'You':tname(id);gs.append(grp(`${short(o.a)} ahead`,G.a),grp('Close · contestable',G.close,'close'),grp(`${short(o.b)} ahead`,G.b));card.append(gs,rows,el('p','fine',`Projected with ${sourceText()}. FG% and FT% are total makes ÷ total attempts; lower turnovers win. Margin bands: Close is within 0.5 pp for FG%, 1 pp for FT% and 5% of the average total elsewhere; Edge is within 1.5 pp, 3 pp and 12%. Bands describe projected margins, not win probabilities.${m.cup_gap_provisional?' Undated NBA Cup or replacement games are omitted, so these totals are understated.':''}`))}

const GAPS={lineup_lock_flags_not_shown_schedule_tipoffs_used:'Yahoo lock markers were not visible; scheduled tip-offs are used as locks.'};
function health(y){yahoo=y||null;const p=e('healthPill'),w=e('waiverWarn');if(!y){p.className='hpill';p.textContent='Yahoo unknown';return}const ok=y.status==='verified',fresh=y.freshness==='fresh';p.className='hpill '+(!ok?'bad':fresh?'good':'warn');p.textContent=y.age_minutes!=null?`Yahoo · ${ago(y.age_minutes)} ago`:'Yahoo · '+y.status;const msg=ok?'':`Yahoo snapshot is ${y.status}; suggestions fall back to saved evidence and stay provisional.`;w.textContent=msg;w.hidden=!msg;renderHealth()}
function renderHealth(){if(!data)return;const q=data.quality,s=q.schedule,dq=q.data_quality,y=yahoo||{},box=e('health'),row=(k,v,c)=>{const x=el('div','hl');x.append(el('span','hk',k),el('span','hv'+(c?' '+c:''),v));box.append(x)};box.replaceChildren();
row('Yahoo snapshot',y.status?`${y.status==='verified'?'Verified':y.status}${y.captured_at_utc?' · '+when(y.captured_at_utc):''}${y.age_minutes!=null?` · ${ago(y.age_minutes)} old`:''}`:'Unknown',y.status!=='verified'?'bad':y.freshness!=='fresh'?'warn':'');
row('Final results',data.actual_completed_weeks?`Weeks 1–${data.actual_completed_weeks} from Yahoo`:'No completed weeks yet');row('Current-week stats',y.actual_state==='captured'?'Captured with the snapshot':y.actual_state?String(y.actual_state).replaceAll('_',' '):'Not captured',y.actual_state&&y.actual_state!=='captured'?'warn':'');
row('NBA schedule',`${s.confirmed_unique_games.toLocaleString('en-CA')} dated games; ${s.unscheduled_games_league_wide} undated Cup games omitted`,s.unscheduled_games_league_wide?'warn':'');row('Player matching',`${dq.linked_source_rows} rows linked; ${dq.unmatched_rostered} rostered unmatched`,dq.unmatched_rostered?'warn':'');row('Fantasy schedule',`${data.matchups.unique_pairings} verified matchups over 18 weeks`);
for(const g of [...(y.reasons||[]),...(y.missing_evidence||[])])row('Note',GAPS[g]||g.replaceAll('_',' '),'warn')}
function renderMethod(){const q=data.quality,s=q.schedule,p=q.provenance,P=t=>el('p',null,t);e('method').replaceChildren(...['Category strength rescales each projected total across the league: best team 100, worst 0, and 50 when every team is tied. Turnovers are reversed because fewer wins. These are rankings, not probabilities.','Each week has nine category decisions, so a team has 162 over the 18-week regular season. A 6–3 week adds six wins and three losses. Category win % is wins plus half of ties, divided by decisions.','Projections blend each source’s per-game makes, attempts and counting stats before percentages are calculated. FG% and FT% are total makes ÷ total attempts.','Every scheduled day uses the best legal lineup from the Yahoo roster. Bench and IL players do not count, and expected games are scaled by projected games played ÷ 82.','Projected seeds break ties with the most recent weekly win percentage, moving backward, then team ID.','Matchup margin bands: Close is within 0.5 pp for FG%, 1 pp for FT% and 5% of the average total elsewhere; Edge is within 1.5 pp, 3 pp and 12%; anything wider is Clear.'].map(P));
e('tech').replaceChildren(...[`Forecast as of ${when(data.as_of_utc)}; Yahoo roster captured ${when(p.yahoo_roster_as_of_utc)}.`,`Model: ${q.projection_model}. Hashtag weight ${Math.round(data.hashtag_weight*100)}%.`,s.source_status,s.lineup_policy,s.lock_policy,s.expected_games_formula,`Matchup file checksum ${String(data.matchups.source_sha256).slice(0,12)}…`].filter(Boolean).map(P))}
function drawer(open){e('settings').hidden=e('scrim').hidden=!open;if(open)e('settingsClose').focus()}
function syncSeg(){for(const b of document.querySelectorAll('#sourceSeg button'))b.setAttribute('aria-pressed',String(b.dataset.v===e('source').value));e('weightBox').hidden=e('source').value!=='blend';e('weightLabel').textContent=Math.round(Number(e('weight').value))+'%';e('sourceChip').textContent=sourceText()}

async function load(){syncSeg();document.body.classList.add('loading');e('error').hidden=true;try{const r=await fetch('/api/deterministic?hashtag_weight='+weight()),v=await r.json();if(!r.ok)throw Error(v.error||'Forecast unavailable');v.my_team_id=v.my_team_id??v.teams[0].team_id;data=v;if(v.league_name)e('leagueName').textContent=v.league_name;fillPeriods();fillWeeks();renderOverview();renderStandings();renderHeat();renderProfile();renderMethod();renderHealth();advisorStale=true;tradesStale=true;show();loadYahoo()}catch(err){e('error').textContent=err.message;e('error').hidden=false}finally{document.body.classList.remove('loading')}}
function yahooBrief(y){return y?[`Yahoo snapshot ${y.status}`,y.age_minutes!=null?`${y.age_minutes} min old`:''].filter(Boolean).join(' · '):''}
function yahooLine(y){if(!y)return 'Yahoo snapshot unknown';return [`Yahoo snapshot ${y.status}`,y.captured_at_utc?`captured ${y.captured_at_utc}`:'',y.age_minutes!=null?`${y.age_minutes} min old (${y.freshness})`:'',y.status!=='verified'&&y.reasons?.length?'reasons: '+y.reasons.join(', '):'',y.missing_evidence?.length?'missing: '+y.missing_evidence.join(', '):'',y.fallback&&y.status!=='verified'?'using '+y.fallback:'',y.actual_state?'actual state: '+y.actual_state:''].filter(Boolean).join(' · ')}
function el(tag,cls,text){const x=document.createElement(tag);if(cls)x.className=cls;if(text!=null)x.textContent=text;return x}
function signed(v,d=1){return Math.abs(v)<5*10**-(d+1)?(0).toFixed(d):(v>0?'+':'−')+Math.abs(v).toFixed(d)}
function words(x){return x.replaceAll('_',' ')}
const tone=t=>'tone-'+(t||'neutral');
const toneOf=v=>v>0?'good':v<0?'bad':'neutral';
const n=v=>v==null?'—':signed(v,Number.isInteger(v)?0:1);
function plural(k,w){return `${k} ${w}${k===1?'':'s'}`}
function colored(text,t){return el('span',tone(t),text)}
function badge(text,t){return el('span','pill '+tone(t),text)}
function tile(label,value,t,sub){const x=el('div','tile');x.append(el('span','tile-label',label),el('span','tile-value '+tone(t),value));if(sub)x.append(el('span','tile-sub',sub));return x}
function dtable(labels,rows,numeric=[]){const t=el('table','dtable'),h=t.createTHead().insertRow();labels.forEach((l,i)=>h.append(el('th',numeric.includes(i)?'num':null,l)));const b=t.createTBody();for(const r of rows){const tr=b.insertRow(),cells=Array.isArray(r)?r:r.cells;if(r.cls)tr.className=r.cls;cells.forEach((v,i)=>{const td=tr.insertCell();if(numeric.includes(i))td.className='num';if(v instanceof Node)td.append(v);else td.textContent=v??'—'})}const w=el('div','scroll');w.append(t);return w}
function timeline(d){const box=el('div','timeline');for(const g of d.timeline){const row=el('div','tl-row'),moves=el('div','tl-moves');row.append(el('span','tl-date',g.when));for(const m of g.moves){const mv=el('div'),a=el('div','tl-line add'),r=el('div','tl-line drop');a.append(el('span','sym','+'),el('span','pname',m.add),el('span','pmeta',m.add_meta),...m.tags.map(t=>el('span','chip'+(t==='Stream'?' chip-stream':''),t)));r.append(el('span','sym','−'),el('span','pname',m.drop),...(m.drop_tag?[el('span','chip',m.drop_tag)]:[]));mv.append(a,r);moves.append(mv)}row.append(moves);box.append(row)}return box}
function riskList(list){const box=el('div','risks');for(const r of list){const x=el('div','risk '+tone(r.tone)),b=el('div');b.append(el('strong',null,r.label+'.'),document.createTextNode(' '+r.text));x.append(el('span','risk-icon',r.tone==='bad'?'×':'!'),b);box.append(x)}return box}
const pctKeys=['fg_pct','ft_pct'];
function edge(k,v){return pctKeys.includes(k)?signed(100*v,1)+' pp':signed(v,1)}
function categoryResults(s){const out=el('div');for(const w of s.weeks){const keys=Object.keys(w.categories),moved=keys.some(k=>Math.abs(w.categories[k].margin_after-w.categories[k].margin_before)>1e-9),head=`Week ${w.week} vs ${w.opponent_name}: ${record(w.before)} → ${record(w.after)}`;if(!moved){out.append(el('p','wk',head+' · no change'));continue}out.append(el('p','wk',head),dtable(['Category','Result','Projected edge','Change'],keys.map(k=>{const c=w.categories[k],flip=c.before!==c.after,d=c.margin_after-c.margin_before;return {cls:flip?'flip':'',cells:[c.label,flip?colored(`${c.before} → ${c.after}`,c.after==='W'?'good':c.after==='L'?'bad':'neutral'):c.after,edge(k,c.margin_after),Math.abs(d)<1e-9?'—':colored(edge(k,d),toneOf(d))]}}),[2,3]));out.lastChild.classList.add('narrow')}return out}
const flagShort=f=>f.startsWith('Asset downgrade')?'Asset downgrade':f.includes('weaker per game')?'Weaker per game':f.startsWith('Helps the short term')?'Short-term only':'Small loss ignored';
const tierText={preferable:'Does as well',comparable:'Close',reference:'For reference'};
function contributions(a){const notes=[],pair=x=>x?`${n(x.horizon_points)} / ${n(x.season_points)}`:'—';const rows=a.transactions.map(t=>{const m=[t.leave_one_out_note,t.independent_note].filter(Boolean);m.forEach(x=>{if(!notes.includes(x))notes.push(x)});return [`+${t.add} / −${t.drop}${m.length?' *':''}`,t.temporary?'Stream':'Permanent',pair(t.independent),pair(t.leave_one_out),t.verdict]});const w=el('div');w.append(dtable(['Move','Type','On its own','Share of this plan','Verdict'],rows,[2,3]),el('p','fine','Category wins, short term / rest of season. Share of this plan is the full plan minus the plan without that move.'+(a.interaction?.nonlinear?' The moves interact, so the shares do not add up to the total.':'')+(notes.length?' * '+notes.join('; ')+'.':'')));return w}
function whyBox(s){const d=s.display,a=s.audit,box=el('details','why'),body=el('div','why-body'),sec=(t,...nodes)=>{const x=el('section');x.append(el('h4',null,t),...nodes);body.append(x)};box.append(el('summary',null,'Why this plan?'));
const why=el('ul','plain');for(const w of d.why)why.append(el('li',null,w));sec('What it does',why);
sec('Category results',categoryResults(s),el('p','fine','A flipped category changes the matchup score. Projected edge is your projected lead in that category (negative means behind); an edge change without a flip is a margin improvement only.'));
sec('Projection sources',dtable(['Source','Category wins · '+d.short_term.weeks,`Rest of season · ${d.season.weeks} weeks`],d.sources.map(x=>[x.label,colored(n(x.short_term),toneOf(x.short_term)),x.season==null?'—':colored(n(x.season),toneOf(x.season))]),[1,2]),el('p','fine',['Category wins gained against holding, recalculated with each source. Sensitivity checks, not probabilities.',d.season.margin_note].filter(Boolean).join(' ')));
if(a){sec('Each move',contributions(a));if(a.replacements.length)sec('Permanent changes',dtable(['Change','Lineup value','Expected games','Lineup starts','Note'],a.replacements.map(r=>[`${r.outgoing} → ${r.incoming}`,`${r.lineup_value.outgoing.toFixed(0)} → ${r.lineup_value.incoming.toFixed(0)}`,`${r.expected_remaining_gp.outgoing} → ${r.expected_remaining_gp.incoming}`,`${r.usable_starts.outgoing.horizon+r.usable_starts.outgoing.post_horizon} → ${r.usable_starts.incoming.horizon+r.usable_starts.incoming.post_horizon}`,r.flags.map(flagShort).join('; ')||'—']),[1,2,3]),el('p','fine','Rest-of-season values for the player you drop → the player you add. Lineup value is the projected value of his lineup contributions; trade value is not modelled.'));
if(a.simpler_alternatives.length)sec('Simpler alternatives',dtable(['Plan','Adds','Short term','Rest of season','Verdict'],a.simpler_alternatives.map(x=>[x.label,String(x.acquisitions),n(x.horizon_points),n(x.season_points),[tierText[x.tier]||x.tier,...x.tradeoffs.slice(0,1)].join(' · ')]),[1,2,3]))}
const extra=[];if(s.similar_options?.length)extra.push(el('p','fine','Similar plans: '+s.similar_options.map(o=>o.moves).join(' | ')));if(s.alternative_drops?.length)extra.push(el('p','fine','Other drop choices: '+s.alternative_drops.map(x=>x.drops.join(' + ')).join(' | ')));if(extra.length)sec('Close variations',...extra);
if(d.yahoo.gaps.length){const u=el('ul','plain');d.yahoo.gaps.forEach(g=>u.append(el('li',null,g)));sec('Yahoo verification',u,el('p','fine','Limits of the Yahoo data, separate from the projection checks above.'))}
box.append(body);return box}
function strategyCard(s,index,prefix='Strategy'){const d=s.display,card=el('article','scard'+(s.moves.length?'':' hold')),head=el('div','scard-head'),t=el('div');if(prefix==='Strategy'&&s.moves.length)card.id='strategy-'+index;t.append(el('div','eyebrow',s.moves.length?`${prefix} ${index} · ${d.kind}`:'Baseline'),el('h3',null,d.title));head.append(t);if(s.moves.length)head.append(badge(d.yahoo.label==='Ready now'?'Ready now':'Yahoo: '+d.yahoo.label.toLowerCase(),d.yahoo.tone));card.append(head);
if(d.why_consider?.length)card.append(el('p','consider','Why consider it: '+d.why_consider.join(' · ')+'.'));
if(!s.moves.length)card.append(el('p','consider',s.why.join('. ')+'.'));
const tiles=el('div','tiles');tiles.append(tile('Category wins · '+d.short_term.weeks,d.short_term.text,d.short_term.tone,d.short_term.detail),tile('Rest of season',s.moves.length?d.season.label:'Baseline',s.moves.length?d.season.tone:'neutral',s.moves.length?`Category wins over ${d.season.weeks} weeks: ${d.season.detail}`:''),tile('Acquisitions',String(d.acquisitions.value),'neutral',d.acquisitions.detail||'None'),tile('Permanent changes',String(d.permanent.value),'neutral',d.permanent.pairs.join('\n')||'None'));card.append(tiles);
if(s.moves.length){card.append(el('div','section-label','Transaction plan'),timeline(d));if(d.yahoo.text)card.append(el('p','cond',d.yahoo.text))}
const rl=[...d.risks,...(s.why_not||[]).map(x=>({label:'Falls short',text:x+'.',tone:'bad'}))];if(rl.length)card.append(riskList(rl));
if(s.moves.length)card.append(whyBox(s));if(s.audit)card.append(auditBox(s.audit,s));return card}
function comparison(v){const rows=v.strategies.map((s,i)=>({s,label:`${i+1}. ${s.display.title}`,id:'strategy-'+(i+1)}));rows.push({s:v.hold,label:'Hold · keep the current roster'});const w=el('div','compare');w.append(el('div','section-label','Compare with holding'),dtable(['Plan','Short-term wins','Rest of season','Adds','Permanent','Main risk'],rows.map(({s,label,id})=>{const d=s.display,name=el('div'),link=el('span',id?'link':null,label);if(id)link.onclick=()=>e(id).scrollIntoView({behavior:'smooth',block:'start'});name.append(link);if(d.brief)name.append(el('span','plan-brief',d.brief));return {cls:s.moves.length?'':'holdrow',cells:[name,colored(d.short_term.text,d.short_term.tone),s.moves.length?colored(d.season.label,d.season.tone):'Baseline',String(s.acquisitions),String(d.permanent.value),d.risks[0]?.label||'—']}}),[1,3,4]),el('p','fine',`Holding projects ${v.hold.weeks.map(x=>`Week ${x.week} ${record(x.before)}`).join(' and ')}. Wins are category wins gained against holding.`));return w}
function dl(d){return d?`short term ${signed(d.horizon_points)}, rest of season ${signed(d.season_points)} category wins, margin score ${signed(d.season_margin,2)}`:'not a legal plan'}
function auditBox(a,s){const box=el('details','audit'),sum=a.summary,add=(title,nodes)=>{box.append(el('h4',null,title),...nodes)};box.append(el('summary',null,'Detailed audit'));
add('Summary',[el('p',null,`Short term ${signed(sum.short_term.category_wins)} category wins (${sum.short_term.weeks.map(w=>`Wk ${w.week} ${record(w.before)} → ${record(w.after)}`).join(', ')}). Rest of season ${signed(sum.rest_of_season.category_wins)} over ${sum.rest_of_season.weeks} periods (margin score ${signed(sum.rest_of_season.margin_score,2)}). ${sum.acquisitions} acquisition${sum.acquisitions>1?'s':''}, ${sum.temporary_adds} temporary. Net changes: ${sum.net_changes.join(', ')||'none'}. ${sum.preferable_simpler_plan?'A shorter plan does as well: '+sum.preferable_simpler_plan+'.':''} ${sum.confidence_limits.join(' ')}`)]);
add('Each transaction',[...a.transactions.map(t=>el('p',null,`${t.step}. +${t.add} / −${t.drop} (${t.date}${t.temporary?', stream':''}): ${t.verdict}. Removing it changes ${dl(t.leave_one_out&&{horizon_points:-t.leave_one_out.horizon_points,season_points:-t.leave_one_out.season_points,season_margin:-t.leave_one_out.season_margin})}${t.leave_one_out_note?' ('+t.leave_one_out_note+')':''}. In order: ${dl(t.chronological)}. Alone: ${dl(t.independent)}${t.independent_note?' ('+t.independent_note+')':''}.`)),...(a.interaction?[el('p','note',`Interaction (full plan minus moves made alone): ${dl(a.interaction)}${a.interaction.nonlinear?'; gains are not additive':''}.`)]:[])]);
const pg=x=>`${x.pts} PTS, ${x.reb} REB, ${x.ast} AST, ${x.three_ptm} 3PTM, ${x.stl} STL, ${x.blk} BLK, ${x.turnovers} TO, FG% ${x.fg_pct??'—'} (${x.fga} FGA), FT% ${x.ft_pct??'—'} (${x.fta} FTA), ${x.gp} GP`;
add('Permanent changes',[...a.replacements.map(r=>{const d=el('div');d.append(el('b',null,`${r.outgoing} → ${r.incoming}`),el('p',null,`Per game: ${r.outgoing} ${pg(r.per_game.outgoing)}. ${r.incoming} ${pg(r.per_game.incoming)}.`),el('p',null,`Rest of season: ${r.remaining_games.outgoing} vs ${r.remaining_games.incoming} scheduled games; expected GP ${r.expected_remaining_gp.outgoing} vs ${r.expected_remaining_gp.incoming}; lineup starts ${r.usable_starts.outgoing.horizon+r.usable_starts.outgoing.post_horizon} vs ${r.usable_starts.incoming.horizon+r.usable_starts.incoming.post_horizon} (${r.usable_starts.outgoing.expected_after_gp} vs ${r.usable_starts.incoming.expected_after_gp} after GP); lineup value ${r.lineup_value.outgoing} vs ${r.lineup_value.incoming}. This swap alone: ${r.independent.feasible?dl(r.independent):r.independent.infeasible_reason}. Its share of this plan: ${dl(r.within_plan)}. Scarcity: #${r.scarcity.incoming_pool_rank} of ${r.scarcity.pool_size} available players; ${r.scarcity.available_at_outgoing_positions_at_least_as_valuable} available at ${r.outgoing}'s positions are at least as valuable.`));if(r.flags.length)d.append(el('p','bad',r.flags.join('. ')+'.'));return d}),...a.temporary.map(t=>el('p',null,`Temporary: ${t.player}, ${t.added} to ${t.dropped}, ${t.starts} lineup start${t.starts===1?'':'s'}.`)),el('p','note',a.replacements[0]?.market_value||'')]);
add('Simpler alternatives',a.simpler_alternatives.length?a.simpler_alternatives.map(x=>el('p',null,`${x.label} [${x.tier}]: ${dl(x)}; ${x.tradeoffs.join('; ')}${x.rewired.length?' ('+x.rewired.join('; ')+')':''}.`)):[el('p',null,'No shorter legal plan matches it.')]);
const src=['selected','hashtag','leaguewinner','blended'];add('Projection sources',[miniTable([['p','Plan'],...src.filter(k=>a.sensitivity[0].sources[k]).map(k=>[k,a.sensitivity[0].sources[k].label+' (short / rest)'])],a.sensitivity.map(x=>[x.plan,...src.filter(k=>a.sensitivity[0].sources[k]).map(k=>{const v=x.sources[k];return !v||v.unavailable?'—':`${signed(v.horizon_points)} / ${signed(v.season_points)}`})])),el('p','note','Category-win deltas against HOLD under each source. Diagnostics, not probabilities.')]);
add('Warnings',a.warnings.length?[(()=>{const u=el('ul');for(const w of a.warnings)u.append(el('li',null,w));return u})()]:[el('p',null,'None.')]);const dis=(a.player_sources||[]).filter(x=>x.disagreement?.length);if(dis.length)add('Player projection disagreement',dis.map(x=>el('p',null,`${x.name}: ${x.disagreement.join(', ')}`)));add('Ranking inputs',[el('p',null,`Plan value ${s.value.toFixed(3)}; rank value ${s.rank_value.toFixed(3)}. Short-term margin score ${signed(s.margin_score_gained,2)}; rest-of-season margin score ${signed(s.season.margin_score_delta,2)} (assessment: ${s.season.assessment}); deterministic rest-of-season count ${signed(s.season.category_points_delta)} category wins. Margin scores are ranking aids, not probabilities. Usable starts ${signed(s.usable_starts_gained,0)}.`),el('p','note','Yahoo verification fields: '+(s.missing_verification.join(', ')||'none'))]);return box}
function miniTable(labels,rows){const t=el('table','mini');t.createTHead();t.createTBody();head(t,labels);for(const r of rows){const row=document.createElement('tr');for(const v of r)cell(row,v);t.tBodies[0].appendChild(row)}const wrap=el('div','scroll');wrap.append(t);return wrap}
function renderAdvisor(v){const D=v.display;e('advisorHeadline').textContent=D.headline;const ctx=e('advisorContext'),chips=el('div','chips');ctx.replaceChildren();for(const m of D.matchups){const c=el('div','mchip');c.append(el('b',null,`Week ${m.week} vs ${m.opponent}`),document.createTextNode(` · projected ${m.record} · ${plural(m.days,'day')} · ${plural(m.adds_left,'add')} left`));chips.append(c)}const cap=new Date(v.capture_at_utc),note=el('p','datanote');note.append(document.createTextNode(`${v.team_name} · Yahoo capture ${isNaN(cap)?v.capture_at_utc:cap.toLocaleString('en-CA',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}`));if(D.data_gaps.length)note.append(document.createTextNode(' · '),el('span','tone-warn',D.data_gaps.join('; ')+'. Refresh Yahoo before acting.'));ctx.append(chips,note);const box=e('advisorResults');box.replaceChildren();if(v.strategies.length)box.append(comparison(v));v.strategies.forEach((s,i)=>box.append(strategyCard(s,i+1)));if(!v.strategies.length)box.append(strategyCard(v.hold,0));if(v.closest_alternatives.length){box.append(el('div','section-label','Closest alternatives'));v.closest_alternatives.forEach((s,i)=>box.append(strategyCard(s,i+1,'Alternative')))}const d=v.diagnostics,p=d.pool,q=d.search,diag=e('advisorDiagnostics');diag.replaceChildren();diag.append(el('p',null,`Pool: ${p.yahoo_players} Yahoo players → ${p.unowned_free_agents_or_waivers} available → ${p.eligible_with_games} projectable, active and scheduled → all ${p.single_moves_searched} tried as single moves; ${p.carried_into_combinations} carried into combinations (${p.upgrade_candidates_carried} as upgrade candidates). Excluded: ${Object.entries(p.excluded).map(([k,n])=>`${words(k)} ${n}`).join(', ')||'none'}.`),el('p',null,`Search: ${q.evaluations} plans scored to depth ${q.depth_reached}; ${q.transaction_sets} transaction sets; ${q.season_rosters_evaluated} rest-of-season rosters; ${q.elapsed_seconds}s${q.truncated?'; evaluation cap reached':''}. ${q.heuristic}`),el('h3',null,'Your roster'),miniTable([['n','Player'],['r','Role'],['w','Why'],['v','Rest-of-season value'],['b','Best available at position']],d.roster.map(r=>[r.name+(r.yahoo_status?` (${r.yahoo_status})`:''),r.role,r.reason,r.season_value.toFixed(0),r.best_available_value.toFixed(0)])),el('h3',null,'Best matchup fits if simply added'),miniTable([['n','Player'],['t','Team'],['g','Games'],['c','Category wins'],['s','Usable starts']],p.top_screen.map(r=>[r.name+(r.status?` (${r.status})`:''),r.nba_team,r.horizon_games,signed(r.category_points_if_added),r.starts_if_added])),el('p','note',`Ranking: ${v.method.objective}. ${v.method.season_term}. ${v.method.margin_score} ${v.assumptions.join(' ')}`))}
function weightNow(){const s=e('source').value;return s==='hashtag'?1:s==='leaguewinner'?0:Number(e('weight').value)/100}
function plainList(items){const u=el('ul','plain');for(const x of items)u.append(el('li',null,x));return u}
const fmtDay=s=>new Date(s+'T12:00:00').toLocaleDateString('en-CA',{month:'short',day:'numeric'});
const fairTone=t=>labelTone(t.display.label),labelTone=l=>/opportunity|Mutually|balanced/.test(l)?'good':l==='Below threshold'?'bad':'warn',mktTxt=t=>{const m=t.value.market;return m&&m.partner_surplus!=null?`They ${m.partner_surplus>=0?'gain':'lose'} ${Math.abs(m.partner_surplus).toFixed(0)}`:t.value.label},checkTone=r=>r.startsWith('Holds')?'good':r.startsWith('Fails')?'bad':'warn';
const shortCheck=r=>r.startsWith('Holds')?'Holds':r.startsWith('Mixed')?'Mixed':r.startsWith('Fails')?'Fails':'Unchecked';
function smTile(...a){const x=tile(...a);x.children[1].classList.add('sm');return x}
function srcLine(t){return Object.values(t.sources||{}).map(s=>s.unavailable?`${s.label}: unavailable`:`${s.label}: you ${signed(s.me_points,1)}, them ${signed(s.partner_points,1)}`).join('\n')}
const checked=id=>[...document.querySelectorAll(`#${id} input:checked`)].map(x=>x.value),allOf=id=>document.querySelectorAll(`#${id} input`).length;let tradeSeq=0,tradeBusy=false,tradeApplied=null,tradeTick=null;
function tradeQuery(){const q=new URLSearchParams({hashtag_weight:String(weightNow())}),p=checked('tradePartner'),ty=checked('tradeTypes'),pr=checked('tradeProtect');if(p.length&&p.length<allOf('tradePartner'))q.set('partners',p.join(','));if(ty.length<allOf('tradeTypes'))q.set('types',ty.join(','));if(pr.length)q.set('protect',pr.join(','));return q}
function tradeFiltersChanged(){const p=checked('tradePartner'),ty=checked('tradeTypes'),btn=e('tradeApply'),note=e('tradeDirty'),bad=(allOf('tradePartner')>0&&!p.length)||!ty.length,dirty=tradeApplied!==null&&tradeQuery().toString()!==tradeApplied;btn.disabled=bad||tradeBusy;btn.classList.toggle('pulse',dirty&&!bad&&!tradeBusy);note.className='ts-dirty'+(bad?' tone-warn':'');const sc=e('tradeScope');if(sc)sc.textContent=tradeScope(tradeQuery());if(bad)e('tradeFilters').open=true;note.textContent=bad?'Choose at least one team and one trade type.':dirty&&!tradeBusy?'Changes not applied yet':''}
function tradeScope(q){const n=q.get('partners')?q.get('partners').split(',').length:(allOf('tradePartner')||9),t=q.get('types')?q.get('types').split(',').length:allOf('tradeTypes'),pr=q.get('protect')?q.get('protect').split(',').length:0;return `${n===(allOf('tradePartner')||9)?'all '+n+' teams':n+' team'+(n>1?'s':'')}, ${t===allOf('tradeTypes')?'all trade types':t+' trade type'+(t>1?'s':'')}${pr?`, ${pr} untouchable`:''}`}
function tradeWorking(on,q){const st=e('tradeStatus'),btn=e('tradeApply'),res=e('tradeResults'),had=!!res.querySelector('.scard');clearInterval(tradeTick);st.hidden=!on;btn.replaceChildren(...(on?[el('span','spin'),document.createTextNode('Searching…')]:[document.createTextNode('Search')]));res.classList.toggle('stale',on&&had);res.setAttribute('aria-busy',String(on));if(!on)return;if(!had){e('tradeHeadline').textContent='';res.replaceChildren(...[1,2,3].map(()=>el('div','skel')))}const t0=Date.now(),scope=tradeScope(q),say=()=>{const s=Math.round((Date.now()-t0)/1000);e('tradeStatusText').textContent=`Searching ${scope}: building every package, replaying the best against each scheduled matchup, then checking finalists under each projection source. ${s}s${had?'. Earlier results stay dimmed below until this finishes.':''}`};say();tradeTick=setInterval(say,1000)}
async function loadTrades(){const seq=++tradeSeq,q=tradeQuery();tradeBusy=true;tradeApplied=q.toString();tradeFiltersChanged();tradeWorking(true,q);try{const r=await fetch('/api/trades?'+q),v=await r.json();if(seq!==tradeSeq)return;if(!r.ok)throw Error(v.error||'Trade Lab unavailable');tradeWorking(false);renderTrades(v)}catch(err){if(seq===tradeSeq){tradeWorking(false);e('tradeResults').replaceChildren();e('tradeHeadline').textContent=err.message}}finally{if(seq===tradeSeq){tradeBusy=false;tradeFiltersChanged()}}}
function toggleChip(value,label,on){const l=el('label','mchip'),c=document.createElement('input');c.type='checkbox';c.value=value;c.checked=on;l.append(c,document.createTextNode(label));return l}
function tradeOptions(v){const tb=e('tradePartner');if(!tb.querySelector('input'))tb.replaceChildren(...v.options.partners.map(p=>toggleChip(p.team_id,p.team_name,v.options.selected_partners.includes(p.team_id))));const box=e('tradeProtect');if(!box.querySelector('input'))box.replaceChildren(...v.options.roster.map(p=>toggleChip(p.site_player_id,p.name+(p.slot==='IL'?' (IL)':''),v.options.protected_ids.includes(p.site_player_id))));tradeFiltersChanged()}
function renderTrades(v){tradeOptions(v);e('tradeHeadline').textContent=v.headline;const T=v.timing,V=v.verification,ctx=e('tradeContext'),chips=el('div','chips');ctx.replaceChildren();
for(const [b,t] of [[`No trade ${record(v.hold.record)}`,` · seed #${v.hold.seed}`],[`Effective ${fmtDay(T.earliest_effective_day)}`,' at the earliest'],[`Deadline ${fmtDay(T.trade_deadline)}`,` · ${T.review.toLowerCase()} review, ${T.reject_days}-day reject window`]]){const c=el('div','mchip');c.append(el('b',null,b),document.createTextNode(t));chips.append(c)}
const note=el('p','datanote',`Results from ${new Date().toLocaleTimeString('en-CA',{hour:'numeric',minute:'2-digit'})} · ${v.team_name} · ${V.basis==='current_snapshot'?'current Yahoo snapshot':'provisional: saved Yahoo capture'} · every card is a proposal idea, conditional on acceptance and review`);if(V.data_gaps.length)note.append(document.createTextNode(' · '),el('span','tone-warn',V.data_gaps.map(words).join('; ')+'. Refresh Yahoo before proposing.'));ctx.append(chips,note);
const box=e('tradeResults');box.replaceChildren();box.append(tradeCompare(v));v.cards.forEach((t,i)=>box.append(tradeCard(t,i+1)));box.append(tradeHold(v));
const d=v.diagnostics,diag=e('tradeDiagnostics');diag.replaceChildren();diag.append(el('p',null,`${d.pairs_screened} packages screened → ${d.plausible_after_screen} plausible → ${d.replayed} replayed in full → ${d.source_checked} checked under each source alone.${d.by_type?' By type: '+Object.entries(d.by_type).map(([k,x])=>`${k} ${x.generated||0}/${x.plausible||0}/${x.replayed||0}/${x.finalists||0}/${x.shown||0}`).join(', ')+' (generated/plausible/replayed/finalists/shown).':''} Skipped: ${Object.entries(d.skipped_players).map(([k,n])=>words(k)+' '+n).join(', ')||'none'}. ${d.seconds.total}s.`),el('p',null,'Excluded finalists: '+(Object.entries(d.excluded).filter(([k])=>!k.startsWith('not a')).map(([k,n])=>`${k} (${n})`).join('; ')||'none')+'.'),...(v.market?[el('h3',null,'Market value sources'),plainList(v.market.signals.map(x=>`${x.label}: ${x.available?'used':'not used'}, ${x.rostered_covered} of ${v.market.rostered} rostered players ranked${x.updated_at_utc?`, updated ${x.updated_at_utc.slice(0,10)}`:x.captured_at_utc?`, captured ${x.captured_at_utc.slice(0,10)}`:''}. ${x.note}`)),el('p','fine',`${v.market.rostered_disagreeing} rostered player(s) where the two signals disagree. Curve: ${v.market.curve}.`)]:[]),el('h3',null,'Assumptions'),plainList(v.assumptions))}
const adpTxt=p=>p.adp?p.adp.toFixed(1):'undrafted',gAll=t=>t.gives_all||[t.gives],rAll=t=>t.receives_all||[t.receives],pnames=a=>a.map(p=>p.name).join(' + '),eff=t=>t.effective||{me:t.me.adjusted_delta,partner:t.partner.adjusted_delta};
const mktTone=b=>b==='more'?'good':b==='less'||b==='expensive'?'warn':'neutral',srcTone=s=>s==='agree'?'good':s==='split'?'warn':s==='fails'?'bad':'neutral',srcShort=s=>({agree:'Both agree',split:'Split',fails:'Neither',unchecked:'—'})[s.state],weeksTxt=g=>g.weeks?`Weeks ${g.first_week}–${g.last_week}`:'rest of season';
function tradeCompare(v){const w=el('div','compare');w.append(el('div','section-label','Shortlist, measured against no trade'));const rows=v.cards.map((t,i)=>{const b=t.brief,n=el('span','link');n.append(el('b',null,`Get ${pnames(rAll(t))}`),document.createTextNode(` for ${pnames(gAll(t))}`));n.onclick=()=>e('trade-'+(i+1)).scrollIntoView({behavior:'smooth',block:'start'});return [String(i+1),n,t.display.partner,colored(signed(b.gain.robust,1),toneOf(b.gain.robust)),colored(`${signed(b.market.points,0)} (${signed(b.market.pct,0)}%)`,mktTone(b.market.band)),colored(signed(b.partner.change,1),toneOf(b.partner.change)),colored(srcShort(b.sources),srcTone(b.sources.state))]});rows.push({cls:'holdrow',cells:['—',`No trade · ${record(v.hold.record)}, seed #${v.hold.seed}`,'—','0.0','—','0.0','—']});w.append(dtable(['#','Trade','Partner','You','Market to them','Them','Sources'],rows,[3,4,5]),el('p','fine',v.cards.length?`You and Them: change in category wins over ${weeksTxt(v.cards[0].brief.gain)} (ties count half). You is the lower of the blended and single-source results, narrow flips at half, net of what any free-agent fill would be worth without the trade. Market to them: market points the partner gains or loses (and % of the larger side) by ADP and consensus rankings. Not probabilities.`:'No trade package cleared the gain and source checks. Holding is the recommendation.'));return w}
function xside(label,ps){const x=el('div','xside');x.append(el('span','tile-label',label));for(const p of ps){const r=el('div','xp'),n=el('div');n.append(el('b',null,p.name),el('small',null,` ${p.pos} · ${p.team}${p.status?' · '+p.status:''}`));r.append(n,el('small',null,`${p.adp?'ADP '+p.adp.toFixed(0):'undrafted'}${p.ecr?' · ECR '+p.ecr.toFixed(0):''}${p.market!=null?' · '+p.market.toFixed(0)+' pts':''}`));x.append(r)}return x}
function tradeCard(t,i){const d=t.display,b=t.brief,g=b.gain,m=b.market,P=b.partner,S=b.sources,card=el('article','scard tcard'),head=el('div','scard-head'),h=el('div');card.id='trade-'+i;h.append(el('div','eyebrow',`#${i} · ${d.package_label||'1-for-1'} · ${d.partner}`),el('h3',null,`Get ${pnames(rAll(t))}`));head.append(h,badge(d.label||d.tier_label,labelTone(d.label||d.tier_label)));card.append(head);
const ex=el('div','xchg');ex.append(xside('You send',b.send),el('div','xarrow','⇄'),xside('You receive',b.receive));card.append(ex);
const tiles=el('div','tiles tmetrics');tiles.append(tile('Your gain',signed(g.robust,1),toneOf(g.robust),`category wins, ${weeksTxt(g)}\nnext 3 weeks ${signed(g.next_three,1)}`),tile('Market to them',signed(m.points,0),mktTone(m.band),`${m.text}\n${signed(m.pct,0)}% · they get ${m.give.toFixed(0)}, give ${m.get.toFixed(0)}`),tile('Their categories',signed(P.change,1),toneOf(P.change),`category wins (model)${P.gained.length?'\nup: '+P.gained.slice(0,3).join(', '):''}${P.lost.length?'\ndown: '+P.lost.slice(0,3).join(', '):''}`),smTile('Sources',S.text,srcTone(S.state),S.rows.map(r=>`${r.label} ${signed(r.me,1)}`).join(' · ')));card.append(tiles);
card.append(el('p','tw-why',b.why));if(P.tension)card.append(el('div','tnote',`${d.partner} ${m.band==='more'?'receives more market value but':m.band==='less'?'gives up market value and':m.band==='expensive'?'receives a market premium but':'gets an even market exchange but'} projects ${signed(P.change,1)} category wins. Market value alone does not show whether they would accept.`));
if(b.roster.length){const r=el('div','tmoves');r.append(el('span','tile-label','Roster moves'));for(const x of b.roster)r.append(el('span','mchip'+(x.conditional?' cond':''),x.text+(x.conditional?` (${x.conditional})`:'')));card.append(r)}
const meta=[...b.shares.map(s=>`Shares ${s.players.join(' and ')} with #${s.card}; only one can happen.`),...(b.rank_reason?[b.rank_reason]:[])];if(meta.length)card.append(el('p','tmeta',meta.join(' ')));card.append(tradeWhy(t),tradeDetails(t));return card}
function tradeWhy(t){const d=t.display,b=t.brief,m=b.market,M=t.me,P=t.partner,box=el('details','why'),body=el('div','why-body'),sec=(h,...n)=>{const x=el('section');x.append(el('h4',null,h),...n);body.append(x)};box.append(el('summary',null,'Why this trade?'));
const cr=cats.filter(([k])=>(M.category_delta[k]||0)||(P.category_delta[k]||0)).map(([k,l])=>[l,colored(signed(M.category_delta[k]||0,1),toneOf(M.category_delta[k]||0)),`#${M.strength_before[k]} → #${M.strength_after[k]}`,colored(signed(P.category_delta[k]||0,1),toneOf(P.category_delta[k]||0))]);
sec('Why I benefit',el('p','consider',d.my_gain),cr.length?dtable(['Category','You','Your league rank','Them'],cr,[1,3]):el('p','fine','No category changes.'),plainList(d.why_benefit));
const mx=Math.max(m.give,m.get,m.my_cut||0,1),bar=(lab,val,cls)=>{const r=el('div','mkbar'+(cls?' '+cls:'')),tr=el('span','mtrack'),f=el('i');f.style.width=(100*val/mx).toFixed(1)+'%';tr.append(f);r.append(el('span',null,lab),tr,el('span','num',val.toFixed(0)));return r};
sec('What I give up',bar('You send',m.give,'send'),bar('You receive',m.get),...(m.my_cut>=.5?[bar('You cut',m.my_cut,'send')]:[]),el('p','consider',d.give_up),el('p','fine',`Market value: ${m.basis} Points run from 0 (replacement level) to about 100.`));
sec('Why they would consider it',el('p','consider',d.they_receive),plainList(d.why_consider));
if(d.balancing&&d.balancing.length)sec('Roster balancing',plainList(d.balancing));
if(b.sources.rows.length)sec('Projection sources',dtable(['Source','You','Them'],b.sources.rows.map(r=>[r.label,colored(signed(r.me,1),toneOf(r.me)),colored(signed(r.partner,1),toneOf(r.partner))]),[1,2]),el('p','fine',`Blended projection: ${signed(b.gain.blended,1)} for you; the headline uses the lowest of the three. Sensitivity checks, not probabilities.`));
if(d.warnings.length)sec('Risks',riskList(d.warnings.map(x=>({label:'Check',text:x,tone:'warn'}))));box.append(body);return box}
function tradeDetails(t){const box=el('details','why'),body=el('div','why-body'),M=t.me,P=t.partner,sec=(h,...n)=>{const x=el('section');x.append(el('h4',null,h),...n);body.append(x)};box.append(el('summary',null,'Detailed numbers'));
sec('Next three weeks',dtable(['Week','Team','Opponent','Before','After','Change'],[...M.near_term.map(w=>[String(w.week),'You',tname(w.opponent),record(w.before),record(w.after),signed(w.delta,1)]),...P.near_term.map(w=>[String(w.week),t.display.partner,tname(w.opponent),record(w.before),record(w.after),signed(w.delta,1)])],[5]));
sec('Rest-of-season categories',dtable(['Category','Your wins','Your rank','Their wins','Their rank'],cats.map(([k,l])=>[l,signed(M.category_delta[k]||0,1),`#${M.strength_before[k]} → #${M.strength_after[k]}`,signed(P.category_delta[k]||0,1),`#${P.strength_before[k]} → #${P.strength_after[k]}`]),[1,3]),el('p','fine','Wins: change in weekly category wins, ties count half. Rank: league rank of the remaining-season projected total.'));
const pl=p=>{const g=p.per_game;return [p.name,p.nba_team,p.positions.join('/'),adpTxt(p),p.ecr?p.ecr.toFixed(0):'—',p.market_value!=null?p.market_value.toFixed(0):'—',g.gp.toFixed(0),g.pts.toFixed(1),g.reb.toFixed(1),g.ast.toFixed(1),g.three_ptm.toFixed(1),g.stl.toFixed(1),g.blk.toFixed(1),g.turnovers.toFixed(1),g.fg_pct==null?'—':pct1(g.fg_pct),g.ft_pct==null?'—':pct1(g.ft_pct),p.value.value.toFixed(0)]};
sec('Players (per game)',dtable(['Player','Team','Pos','ADP','ECR','Market','GP','PTS','REB','AST','3PM','STL','BLK','TO','FG%','FT%','Model value'],[...gAll(t).map(pl),...rAll(t).map(pl)],[3,4,5,6,7,8,9,10,11,12,13,14,15,16]),el('p','fine',`ADP is Yahoo's preseason average draft position (standard scoring); ECR is the FantasyPros expert consensus rank. Market value averages the two on one curve and is how other managers are likely to see each player. Model value is rest-of-season value over the best free agent at the player's scarcest position (${t.gives.value.replacement_position||'—'} and ${t.receives.value.replacement_position||'—'}). A roster-neutral model estimate, shown only as an asset-sacrifice risk.`));
sec('Lineup fit',plainList([`${pnames(rAll(t))}: ${M.incoming_starts} starts in ${M.incoming_games} remaining team games for you; ${pnames(gAll(t))} had ${M.outgoing_starts} of ${M.outgoing_games}.`,`${pnames(gAll(t))}: ${P.incoming_starts} starts in ${P.incoming_games} for ${t.display.partner}; ${pnames(rAll(t))} had ${P.outgoing_starts} of ${P.outgoing_games}.`,`Your lineup starts over the rest of the season: ${M.lineup_starts_before} → ${M.lineup_starts_after}.`]));
const fl=M.flips.slice(0,8).map(f=>[`Week ${f.week}`,tname(f.opponent),catName[f.category],`${f.before} → ${f.after}`,f.margin_after==null?'—':edge(f.category,f.margin_after),f.narrow?'Narrow':'']);if(fl.length)sec('Your category flips',dtable(['Week','Opponent','Category','Result','Edge after','Note'],fl));
if(t.sources)sec('Projection sources',dtable(['Source','Your change','Their change','Value gap'],Object.values(t.sources).map(s=>s.unavailable?[s.label,'—','—','—']:[s.label,signed(s.me_points,1),signed(s.partner_points,1),signed(100*s.value_gap,0)+'%']),[1,2,3]),el('p','fine','Category-win changes before the narrow-flip discount. Sensitivity checks, not probabilities.'));
const B=t.balancing,bo=[];if(B)for(const [s,who] of [['me','You'],['partner',t.display.partner]])for(const o of B[s].options||[])bo.push([who,o.kind==='il'?'IL move':'Drop',o.name,signed(o.points_delta,1),o.site_player_id===(B[s].player||{}).site_player_id?'Chosen':'']);if(bo.length)sec('Roster balancing options',dtable(['Team','Move','Player','Overall change','Note'],bo,[3]),el('p','fine','Each legal option replayed in full; the team keeps the one best for itself. Changes are rest-of-season category wins for that team.'));
if(t.one_for_one_subsets)sec('Compared with 1-for-1 parts',dtable(['1-for-1','You','Them','Asset cost'],t.one_for_one_subsets.map(a=>[a.title,signed(a.me_adjusted,1),signed(a.partner_adjusted,1),a.asset_cost.toFixed(0)]),[1,2,3]),el('p','fine',t.simpler_alternative?`${t.simpler_alternative.title} gives a similar gain at lower asset cost, so this package ranks lower.`:'No 1-for-1 part matches the package at lower asset cost.'));
if(t.similar&&t.similar.length)sec('Essentially equivalent packages',plainList(t.similar.map(x=>`${x.title}: ${signed(x.me_adjusted,1)} for you, ${signed(x.partner_adjusted,1)} for them.`)));
box.append(body);return box}
function tradeHold(v){const h=v.hold,card=el('article','scard hold'),head=el('div','scard-head'),x=el('div');x.append(el('div','eyebrow','Baseline'),el('h3',null,'No trade'));head.append(x);card.append(head);const tiles=el('div','tiles');tiles.append(tile('Projected record',record(h.record),'neutral',`${pct1(h.win_pct)} of category decisions`),tile('Projected seed','#'+h.seed,'neutral','regular season'),smTile('Strongest',h.strongest.join(', '),'good','league rank'),smTile('Weakest',h.weakest.join(', '),'bad','league rank'));card.append(tiles,el('p','fine','Every trade above is measured against this baseline over the remaining regular season.'));return card}
async function loadAdvisor(){const source=e('source').value,weight=source==='hashtag'?1:source==='leaguewinner'?0:Number(e('weight').value)/100;const t0=Date.now(),say=()=>{e('advisorHeadline').textContent=`Searching every available player, drop and day, then improving the best plans · ${Math.round((Date.now()-t0)/1000)}s (usually about a minute and a half)`},tick=setInterval(say,1000);say();e('advisorResults').replaceChildren(...[1,2].map(()=>el('div','skel')));try{const r=await fetch('/api/advisor?'+new URLSearchParams({hashtag_weight:String(weight),horizon:e('advisorHorizon').value})),v=await r.json();if(!r.ok)throw Error(v.error||'Waiver Advisor unavailable');clearInterval(tick);renderAdvisor(v)}catch(err){clearInterval(tick);e('advisorResults').replaceChildren();e('advisorHeadline').textContent=err.message}}
async function loadYahoo(){try{const r=await fetch('/api/yahoo/status');const y=await r.json();e('yahooStatus').textContent=yahooLine(y);e('yahooBrief').textContent=yahooBrief(y);health(y)}catch(err){e('yahooStatus').textContent=err.message}}
async function refreshYahoo(){e('yahooRefresh').disabled=true;e('yahooStatus').textContent=e('yahooBrief').textContent='Reading Yahoo in the dedicated Chrome window…';try{const r=await fetch('/api/yahoo/refresh',{method:'POST'}),v=await r.json();e('yahooStatus').textContent=r.ok?yahooLine(v):'Refresh failed; previous data kept. '+v.error;e('yahooBrief').textContent=r.ok?yahooBrief(v):'Refresh failed; previous data kept';if(r.ok){health(v);load()}}catch(err){e('yahooStatus').textContent=err.message}finally{e('yahooRefresh').disabled=false}}

const tip=e('tip');function tipAt(ev){const t=ev.target.closest&&ev.target.closest('[data-tip]');if(!t){tip.hidden=true;return}tip.textContent=t.dataset.tip;tip.hidden=false;const r=t.getBoundingClientRect(),x=r.left+r.width/2-tip.offsetWidth/2;let y=r.top-tip.offsetHeight-8;if(y<8)y=r.bottom+8;tip.style.left=Math.max(8,Math.min(x,innerWidth-tip.offsetWidth-8))+'px';tip.style.top=y+'px'}
document.addEventListener('mouseover',tipAt);document.addEventListener('focusin',tipAt);addEventListener('scroll',()=>{tip.hidden=true},true);
for(const b of document.querySelectorAll('#sourceSeg button'))b.onclick=()=>{if(e('source').value===b.dataset.v)return;e('source').value=b.dataset.v;load()};
e('weight').oninput=syncSeg;e('weight').onchange=load;
e('settingsBtn').onclick=e('sourceChip').onclick=e('healthPill').onclick=()=>drawer(true);e('settingsClose').onclick=e('scrim').onclick=()=>drawer(false);document.addEventListener('keydown',ev=>{if(ev.key==='Escape'&&!e('settings').hidden)drawer(false)});
e('period').onchange=()=>{renderHeat();renderProfile()};e('week').onchange=renderWeek;e('prevWeek').onclick=()=>{e('week').value=selWeek-1;renderWeek()};e('nextWeek').onclick=()=>{e('week').value=selWeek+1;renderWeek()};
e('yahooRefresh').onclick=refreshYahoo;e('advisorRun').onclick=loadAdvisor;e('advisorHorizon').onchange=loadAdvisor;e('tradeApply').onclick=loadTrades;for(const id of ['tradePartner','tradeTypes','tradeProtect'])e(id).onchange=tradeFiltersChanged;document.querySelectorAll('[data-set]').forEach(b=>b.onclick=()=>{const [id,on]=b.dataset.set.split(':');document.querySelectorAll(`#${id} input`).forEach(x=>x.checked=on==='1');tradeFiltersChanged()});addEventListener('hashchange',show);show();load();
</script></body></html>'''


def _classified(value: dict) -> dict:
    for plan in value.get('plans', []):
        plan['eligibility_class'] = classify_plan(plan)
    return value


def load_captured_actual(inputs: dict, reports: Path):
    """Use captured actual state only when bound to the same capture; the forecast re-validates it."""
    path = reports / ACTUAL_NAME
    if not path.exists():
        return None, 'not_captured'
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('as_of_utc') != inputs['availability']['captured_at_utc']:
        return None, 'stale_not_bound_to_capture'
    if value.get('status') != 'captured':
        return None, 'rejected: ' + ', '.join(value.get('problems', []))[:300]
    if value.get('league_id') is not None and value['league_id'] != inputs['league']['league_id']:
        return None, 'rejected: actual_league_mismatch'
    if value.get('team_ids') is not None and set(value['team_ids']) != set(inputs['teams']):
        return None, 'rejected: actual_team_ids_mismatch'
    for key in ('player_fingerprint', 'roster_fingerprint'):
        if value.get(key) is not None and value[key] != inputs['availability'].get(key):
            return None, 'rejected: actual_capture_fingerprint_mismatch'
    completed = value.get('completed_weeks', 0)
    if completed:
        sources = value.get('sources', {})
        league = inputs['league']['league_id']
        expected = [f'/nba/{league}?lhst=matchups&matchup_week={w}&module=matchups'
                    for w in range(1, completed + 1)]
        if (sources.get('standings') != f'/nba/{league}?lhst=stand&module=standings' or
                sources.get('matchups') != expected):
            return None, 'rejected: completed_result_source_lineage_missing'
    try:
        from .simulator import _actual_state
        stamp = datetime.fromisoformat(value['as_of_utc'].replace('Z', '+00:00'))
        _actual_state(inputs, stamp, value)
    except (ValueError, KeyError, TypeError) as exc:
        return None, 'rejected: ' + str(exc)[:300]
    return value, 'captured'


def make_handler(inputs: dict, actual_state: dict | None = None, bracket: dict | None = None,
                 matchup_path: Path | None = None, transaction_evidence: dict | None = None,
                 yahoo_status: dict | None = None, root: Path | None = None, refresher=None):
    refresh_lock = threading.Lock()

    def refresh_yahoo():
        """Read-only capture, then reload. On any failure the previous snapshot stays in use."""
        nonlocal inputs, transaction_evidence, yahoo_status, actual_state
        if not refresh_lock.acquire(blocking=False):
            raise RuntimeError('A Yahoo refresh is already running')
        try:
            if refresher is None or root is None:
                raise RuntimeError('Yahoo refresh needs the local dashboard root and browser dependency')
            refresher()
            fresh = load_inputs(root)
            evidence, status = resolve_evidence(fresh, root / 'data' / 'reports')
            actual, status['actual_state'] = load_captured_actual(fresh, root / 'data' / 'reports')
            inputs, transaction_evidence, yahoo_status, actual_state = fresh, evidence, status, actual
            cached_forecast.cache_clear()
            cached_deterministic.cache_clear()
            cached_trades.cache_clear()
            return status
        finally:
            refresh_lock.release()

    @lru_cache(maxsize=8)
    def cached_forecast(weight: float, draws: int, seed: int, timestamp: str | None):
        as_of = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
        return simulate(inputs, matchup_path=matchup_path, hashtag_weight=weight, draws=draws, seed=seed,
                        as_of=as_of, actual_state=actual_state, bracket=bracket)

    @lru_cache(maxsize=8)
    def cached_deterministic(weight: float, timestamp: str | None):
        as_of = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
        return forecast(inputs, matchup_path=matchup_path, hashtag_weight=weight,
                        as_of=as_of, actual_state=actual_state)

    @lru_cache(maxsize=16)   # one entry per team/type toggle combination
    def cached_trades(weight: float, timestamp: str | None, partners: tuple, protect: tuple, types: tuple = ()):
        as_of = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
        bases = {}
        for source_weight in (1.0, 0.0):   # shared with the page's source toggle cache
            try:
                bases[source_weight] = cached_deterministic(source_weight, timestamp)
            except (ValueError, KeyError):
                pass
        return trade_lab(inputs, team_id=inputs['league']['my_team_id'], weight=weight, as_of=as_of,
                         actual_state=actual_state, evidence=transaction_evidence, yahoo_status=yahoo_status,
                         config=TradeConfig(partner_ids=partners or None, protected_ids=frozenset(protect),
                                            **({'package_types': types} if types else {})),
                         base=cached_deterministic(weight, timestamp), source_bases=bases)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/yahoo/status":
                content = json.dumps(yahoo_status or {'status': 'missing', 'reasons': ['yahoo_snapshot_missing']}).encode("utf-8")
                mime, status = "application/json; charset=utf-8", 200
            elif parsed.path == "/":
                content = HTML.encode("utf-8")
                mime = "text/html; charset=utf-8"
                status = 200
            elif parsed.path == "/api/deterministic":
                try:
                    query = parse_qs(parsed.query)
                    weight = float(query.get("hashtag_weight", ["0.5"])[0])
                    timestamp = query.get("as_of_utc", [actual_state.get("as_of_utc") if actual_state else None])[0]
                    # Additive identity fields for the page; the cached forecast is not mutated.
                    value = {**cached_deterministic(weight, timestamp),
                             'my_team_id': inputs['league']['my_team_id'],
                             'league_name': inputs['league'].get('league_name')}
                    content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    status = 200
                except (ValueError, KeyError) as exc:
                    message = str(exc)
                    reason = (yahoo_status or {}).get("actual_state") or ""
                    if actual_state is None and reason.startswith("rejected"):   # name the real cause
                        message += f" (the captured actual state was {reason})"
                    content = json.dumps({"error": message}).encode("utf-8")
                    status = 400
                mime = "application/json; charset=utf-8"
            elif parsed.path == "/api/advisor":
                try:
                    query = parse_qs(parsed.query)
                    weight = float(query.get("hashtag_weight", ["0.5"])[0])
                    horizon = query.get("horizon", ["auto"])[0]
                    timestamp = actual_state.get("as_of_utc") if actual_state else None
                    as_of = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
                    value = advise(inputs, team_id=inputs['league']['my_team_id'], weight=weight, as_of=as_of,
                                   actual_state=actual_state, evidence=transaction_evidence,
                                   matchup_path=matchup_path, now=datetime.now(timezone.utc),
                                   config=AdvisorConfig(horizon=None if horizon == "auto" else int(horizon)),
                                   base=cached_deterministic(weight, timestamp))
                    value["yahoo"] = yahoo_status
                    present(value)  # additive `display` labels; advisor values unchanged
                    content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    status = 200
                except (ValueError, KeyError) as exc:
                    message = str(exc)
                    reason = (yahoo_status or {}).get("actual_state") or ""
                    if actual_state is None and reason.startswith("rejected"):   # name the real cause
                        message += f" (the captured actual state was {reason})"
                    content = json.dumps({"error": message}).encode("utf-8")
                    status = 400
                mime = "application/json; charset=utf-8"
            elif parsed.path == "/api/trades":
                try:
                    query = parse_qs(parsed.query)
                    weight = float(query.get("hashtag_weight", ["0.5"])[0])
                    partners = tuple(sorted(x for x in query.get("partners", [""])[0].split(",") if x))
                    protect = tuple(sorted(x for x in query.get("protect", [""])[0].split(",") if x))
                    types = ()
                    if "types" in query:
                        asked = {x for x in query["types"][0].split(",") if x}
                        types = tuple(t for t in ("1-1", "2-1", "1-2", "2-2") if t in asked)
                        if not types or asked - set(types):
                            raise ValueError("Choose trade types from 1-1, 2-1, 1-2 and 2-2")
                    timestamp = actual_state.get("as_of_utc") if actual_state else None
                    # Cached analysis; verification is recomputed so evidence age stays current.
                    value = {**trade_briefs(cached_trades(weight, timestamp, partners, protect, types)), "yahoo": yahoo_status,
                             "verification": trade_verification(inputs, transaction_evidence, yahoo_status,
                                                                datetime.now(timezone.utc))}
                    content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    status = 200
                except (ValueError, KeyError) as exc:
                    content = json.dumps({"error": str(exc)}).encode("utf-8")
                    status = 400
                mime = "application/json; charset=utf-8"
            elif parsed.path == "/api/waiver":
                try:
                    query = parse_qs(parsed.query)
                    weight = float(query.get("hashtag_weight", ["0.5"])[0])
                    timestamp = actual_state.get("as_of_utc") if actual_state else None
                    as_of = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
                    add_ids = query.get("add_site_player_id", [])
                    value = evaluate_singles(inputs, team_id=inputs['league']['my_team_id'],
                        weight=weight, as_of=as_of, actual_state=actual_state,
                        matchup_path=matchup_path, add_ids=add_ids, evidence=transaction_evidence,
                        now=datetime.now(timezone.utc))
                    value['yahoo'] = yahoo_status
                    content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    status = 200
                except (ValueError, KeyError) as exc:
                    content = json.dumps({"error": str(exc)}).encode("utf-8")
                    status = 400
                mime = "application/json; charset=utf-8"
            elif parsed.path == "/api/streaming":
                try:
                    query = parse_qs(parsed.query)
                    weight = float(query.get("hashtag_weight", ["0.5"])[0])
                    width = min(40, max(1, int(query.get("beam_width", ["12"])[0])))
                    candidates = min(12, max(1, int(query.get("candidate_limit", ["6"])[0])))
                    timestamp = actual_state.get("as_of_utc") if actual_state else None
                    as_of = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
                    value = optimize_week(inputs, team_id=inputs['league']['my_team_id'], weight=weight,
                        as_of=as_of, actual_state=actual_state, evidence=transaction_evidence,
                        matchup_path=matchup_path, config=SearchConfig(beam_width=width, candidate_limit=candidates))
                    value = _classified(value)
                    content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    status = 200
                except (ValueError, KeyError) as exc:
                    content = json.dumps({"error": str(exc)}).encode("utf-8")
                    status = 400
                mime = "application/json; charset=utf-8"
            elif parsed.path == "/api/strategies":
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    weight = float(query.get('hashtag_weight', ['0.5'])[0])
                    horizon = int(query.get('horizon', ['2'])[0])
                    mode = query.get('mode', ['streaming'])[0]
                    tolerance = float(query.get('future_win_loss_tolerance', ['0'])[0])
                    protected = tuple(filter(None, query['protected_site_ids'][0].split(','))) if 'protected_site_ids' in query else None
                    slots = tuple(filter(None, query['streaming_slot_site_ids'][0].split(','))) if 'streaming_slot_site_ids' in query else None
                    timestamp = actual_state.get('as_of_utc') if actual_state else None
                    as_of = datetime.fromisoformat(timestamp.replace('Z', '+00:00')) if timestamp else None
                    value = optimize_horizon(inputs, horizon=horizon,
                        team_id=inputs['league']['my_team_id'], weight=weight, as_of=as_of,
                        actual_state=actual_state, evidence=transaction_evidence,
                        matchup_path=matchup_path,
                        strategy_policy=StrategyPolicy(mode=mode, protected_site_ids=protected,
                            streaming_slot_site_ids=slots, future_win_loss_tolerance=tolerance))
                    value = _classified(value)
                    content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
                    status = 200
                except (ValueError, KeyError) as exc:
                    content = json.dumps({'error': str(exc)}).encode('utf-8')
                    status = 400
                mime = 'application/json; charset=utf-8'
            elif parsed.path == "/api/rolling":
                try:
                    query = parse_qs(parsed.query)
                    weight = float(query.get("hashtag_weight", ["0.5"])[0])
                    horizon = int(query.get("horizon", ["2"])[0])
                    timestamp = actual_state.get("as_of_utc") if actual_state else None
                    as_of = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
                    value = optimize_horizon(inputs, horizon=horizon, team_id=inputs['league']['my_team_id'],
                        weight=weight, as_of=as_of, actual_state=actual_state,
                        evidence=transaction_evidence, matchup_path=matchup_path)
                    value = _classified(value)
                    content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    status = 200
                except (ValueError, KeyError) as exc:
                    content = json.dumps({"error": str(exc)}).encode("utf-8")
                    status = 400
                mime = "application/json; charset=utf-8"
            elif parsed.path == "/api/forecast":
                try:
                    query = parse_qs(parsed.query)
                    weight = float(query.get("hashtag_weight", ["0.5"])[0])
                    draws = int(query.get("draws", ["500"])[0])
                    seed = int(query.get("seed", ["20261020"])[0])
                    timestamp = query.get("as_of_utc", [actual_state.get("as_of_utc") if actual_state else None])[0]
                    content = json.dumps(cached_forecast(weight, draws, seed, timestamp),
                                         ensure_ascii=False, allow_nan=False).encode("utf-8")
                    status = 200
                except (ValueError, KeyError) as exc:
                    content = json.dumps({"error": str(exc)}).encode("utf-8")
                    status = 400
                mime = "application/json; charset=utf-8"
            elif parsed.path == "/api/rankings":
                try:
                    weight = float(parse_qs(parsed.query).get("hashtag_weight", ["0.5"])[0])
                    query = parse_qs(parsed.query)
                    requested = query["week"][0] if "week" in query else None
                    week = requested if requested in {"regular", "all"} else int(requested) if requested is not None else None
                    as_of = datetime.fromisoformat(query["as_of_utc"][0].replace("Z", "+00:00")) if "as_of_utc" in query else None
                    content = json.dumps(rank_league(inputs, weight, week, as_of), ensure_ascii=False, allow_nan=False).encode("utf-8")
                    status = 200
                except (ValueError, KeyError) as exc:
                    content = json.dumps({"error": str(exc)}).encode("utf-8")
                    status = 400
                mime = "application/json; charset=utf-8"
            else:
                content, mime, status = b"Not found", "text/plain; charset=utf-8", 404
            self._send(status, mime, content)

        def do_POST(self):
            if urlparse(self.path).path != "/api/yahoo/refresh":
                return self._send(404, "text/plain; charset=utf-8", b"Not found")
            try:
                value, status = refresh_yahoo(), 200
            except Exception as exc:  # report and keep the previous snapshot
                value, status = {"error": f"{type(exc).__name__}: {exc}", "previous": yahoo_status}, 502
            self._send(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False).encode("utf-8"))

        def _send(self, status, mime, content):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Local schedule-aware category-strength dashboard")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--actual-state", type=Path, help="Verified current standings, weekly results and current-week totals JSON")
    parser.add_argument("--bracket-evidence", type=Path, help="Reviewed Yahoo playoff bracket capture rules JSON")
    parser.add_argument("--league-id", default="92103")
    parser.add_argument("--team-id", default="4")
    args = parser.parse_args()
    inputs = load_inputs(args.root.resolve())
    actual_state = json.loads(args.actual_state.read_text(encoding="utf-8")) if args.actual_state else None
    bracket = json.loads(args.bracket_evidence.read_text(encoding="utf-8")) if args.bracket_evidence else None
    root = args.root.resolve()
    evidence, yahoo_status = resolve_evidence(inputs, root / "data" / "reports")
    if actual_state is None:
        actual_state, yahoo_status["actual_state"] = load_captured_actual(inputs, root / "data" / "reports")
    else:
        yahoo_status["actual_state"] = "supplied_by_flag"

    def refresher():
        from .browser_refresh import refresh
        refresh(root, args.league_id, team_id=args.team_id)

    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(inputs, actual_state, bracket, root / "H2H-Matchups.csv",
                                              evidence, yahoo_status, root, refresher))
    print(f"Dated schedule dashboard: http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
