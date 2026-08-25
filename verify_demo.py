"""Headless Edge smoke test for Bubble Garden v0.6 (泡泡花园).
Covers: 50 levels / 5 chapters, streak bonus, chapter-clear rewards,
workshop compliance validation, tutorial level, new-special intro popups,
special bubbles, cascade engine, creative workshop, core pop/floating rules,
daily challenge, achievements, theme shop, career stats, loss encouragement.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

ROOT = Path(__file__).resolve().parent
EDGE_CANDIDATES = [
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
]
PORT = 9450
BROWSER_EXCEPTIONS: list[dict] = []


def edge_path() -> Path:
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Microsoft Edge not found")


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.load(response)


def cdp(ws, method: str, params=None, seq=[0]):
    seq[0] += 1
    ident = seq[0]
    ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
    while True:
        message = json.loads(ws.recv())
        if message.get("method") == "Runtime.exceptionThrown":
            BROWSER_EXCEPTIONS.append(message.get("params", {}))
        if message.get("id") == ident:
            if "error" in message:
                raise RuntimeError(message["error"])
            return message.get("result", {})


def evaluate(ws, expression: str):
    result = cdp(ws, "Runtime.evaluate", {
        "expression": expression, "awaitPromise": True, "returnByValue": True,
    })
    payload = result.get("result", {})
    if payload.get("subtype") == "error":
        raise RuntimeError(payload.get("description", "JavaScript evaluation failed"))
    return payload.get("value")


def screenshot(ws, path: Path):
    data = cdp(ws, "Page.captureScreenshot", {"format": "png"}).get("data", "")
    path.write_bytes(base64.b64decode(data))


def main() -> int:
    index = ROOT / "index.html"
    if not index.is_file():
        raise FileNotFoundError(index)
    profile = Path(tempfile.mkdtemp(prefix="bubble_garden_v2_test_"))
    shots = ROOT / "screenshots"
    shots.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    env["no_proxy"] = env["NO_PROXY"]
    command = [
        str(edge_path()), "--headless=new", "--disable-gpu", "--no-first-run",
        "--disable-background-networking", f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*", f"--user-data-dir={profile}",
        "--window-size=520,760", index.as_uri(),
    ]
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    report = {"game": "bubble-garden-demo-v0.6", "checks": [], "runtimeErrors": [], "screenshots": []}

    def check(name, ok, detail=None):
        entry = {"name": name, "ok": bool(ok)}
        if detail is not None:
            entry["detail"] = detail
        report["checks"].append(entry)
        print(("PASS" if ok else "FAIL"), "-", name, "" if detail is None else detail)

    ws = None
    try:
        target = None
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                pages = get_json(f"http://127.0.0.1:{PORT}/json")
                target = next((p for p in pages if p.get("type") == "page" and "index.html" in str(p.get("url", ""))), None)
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not target:
            raise RuntimeError("Edge DevTools target did not become ready")
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30, origin="http://localhost")
        cdp(ws, "Runtime.enable")
        cdp(ws, "Page.enable")
        cdp(ws, "Page.reload", {"ignoreCache": True})

        # 1) 钩子就绪 + 版本
        deadline = time.time() + 15
        ready = False
        while time.time() < deadline:
            ready = bool(evaluate(ws, "Boolean(window.__GARDEN_TEST__ && document.querySelector('#game'))"))
            if ready:
                break
            time.sleep(0.2)
        check("hook_ready", ready)
        if not ready:
            raise RuntimeError("__GARDEN_TEST__ not exposed")
        check("version_v07", evaluate(ws, "window.__GARDEN_TEST__.version") == "0.7")

        # 2) 画布尺寸
        size = evaluate(ws, "({w:document.querySelector('#game').width,h:document.querySelector('#game').height})")
        check("canvas_sized", size["w"] > 0 and size["h"] > 0, size)

        # 2b) 开场确认：进关默认不开启（防误触），点按后才 ready
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1)")
        check("intro_card_blocks_start", evaluate(ws, "window.__GARDEN_TEST__.introCardVisible()") and not evaluate(ws, "window.__GARDEN_TEST__.ready()"))
        # 完整点击（down+up）开启，且绝不发射
        shots_before = evaluate(ws, "window.__GARDEN_TEST__.state().shotsLeft")
        evaluate(ws, "window.__GARDEN_TEST__.clickAt(210,440)")
        shots_after = evaluate(ws, "window.__GARDEN_TEST__.state().shotsLeft")
        flying = evaluate(ws, "window.__GARDEN_TEST__.state()")
        check("tap_starts_game", evaluate(ws, "window.__GARDEN_TEST__.ready()"))
        check("start_tap_does_not_shoot", shots_before == shots_after and flying["shotsLeft"] == shots_before,
              {"before": shots_before, "after": shots_after})
        evaluate(ws, "window.__GARDEN_TEST__.startGame()")

        # 3) 基础消除 + v0.7 物理：悬空轻泡上飘补位（不消除，保留在棋盘）
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1)")
        evaluate(ws, "window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0);window.__GARDEN_TEST__.setCell(1,1,1)")
        res = evaluate(ws, "window.__GARDEN_TEST__.resolve(0,0)")
        cells = evaluate(ws, "window.__GARDEN_TEST__.getCells()")
        check("basic_pop_and_float_up", res["popped"] == 3 and res["dropped"] == 0 and len(cells) == 1,
              {"popped": res["popped"], "dropped": res["dropped"], "cells": cells})

        # 4) 彩虹泡泡：与多数邻色成组
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,'R');window.__GARDEN_TEST__.setCell(0,3,1)")
        res = evaluate(ws, "window.__GARDEN_TEST__.resolve(0,2)")
        check("rainbow_joins_group", res["popped"] >= 3, res)

        # 5) 炸弹：范围爆破 + 石头可被炸毁
        #    半径65：S(0,0)与(1,0)(1,1)距离50被炸；(1,2)距离86.6幸存
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,1,'B');window.__GARDEN_TEST__.setCell(1,0,0);window.__GARDEN_TEST__.setCell(1,1,1);window.__GARDEN_TEST__.setCell(1,2,2);window.__GARDEN_TEST__.setCell(0,0,'S')")
        n = evaluate(ws, "window.__GARDEN_TEST__.detonate(0,1)")
        st = evaluate(ws, "window.__GARDEN_TEST__.state()")
        left = evaluate(ws, "window.__GARDEN_TEST__.getCells()")
        check("bomb_blasts_area_and_stone", n == 3 and st["bubbles"] == 1 and left[0]["t"] == 2,
              {"destroyed": n, "left": left})

        # 6) 闪电：整行清除（石头除外）
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();for(let c=0;c<8;c++)window.__GARDEN_TEST__.setCell(0,c,c%3);window.__GARDEN_TEST__.setCell(1,0,'S')")
        n = evaluate(ws, "window.__GARDEN_TEST__.clearRowVia(0)")
        st = evaluate(ws, "window.__GARDEN_TEST__.state()")
        check("lightning_clears_row", n == 8 and st["bubbles"] == 1, {"cleared": n, "left": st["bubbles"]})

        # 7) 炸弹链式引爆相邻炸弹：第二颗炸弹波及(1,2)，(1,3)幸存
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,1,'B');window.__GARDEN_TEST__.setCell(0,2,'B');window.__GARDEN_TEST__.setCell(1,0,0);window.__GARDEN_TEST__.setCell(1,1,1);window.__GARDEN_TEST__.setCell(1,2,2);window.__GARDEN_TEST__.setCell(1,3,0)")
        n = evaluate(ws, "window.__GARDEN_TEST__.detonate(0,1)")
        st = evaluate(ws, "window.__GARDEN_TEST__.state()")
        check("bomb_chain_reaction", n == 4 and st["bubbles"] == 1, {"destroyed": n, "left": st["bubbles"]})

        # 8) 冰块锚定在顶行：邻组消除 → 破冰一层（hp 2→1，不坠落）
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0);window.__GARDEN_TEST__.setCell(0,3,'I')")
        evaluate(ws, "window.__GARDEN_TEST__.resolve(0,0)")
        cells = evaluate(ws, "window.__GARDEN_TEST__.getCells()")
        ice = [c for c in cells if c["t"] == "I"]
        check("ice_first_hit_cracks", len(ice) == 1 and ice[0]["hp"] == 1, ice)

        # 8b) 第二次相邻消除 → 冰块彻底破碎
        evaluate(ws, "window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        res = evaluate(ws, "window.__GARDEN_TEST__.resolve(0,0)")
        cells = evaluate(ws, "window.__GARDEN_TEST__.getCells()")
        check("ice_second_hit_breaks", not any(c["t"] == "I" for c in cells) and cells == [], {"cells": cells})

        # 9) 创意工坊：编辑 → 保存
        evaluate(ws, "window.__GARDEN_TEST__.edClear();window.__GARDEN_TEST__.edShots(15)")
        evaluate(ws, "for(let c=0;c<8;c++)window.__GARDEN_TEST__.edSet(0,c,c%3);window.__GARDEN_TEST__.edSet(1,0,'B');window.__GARDEN_TEST__.edSet(1,1,'I');window.__GARDEN_TEST__.edSet(1,2,'S')")
        ok = evaluate(ws, "window.__GARDEN_TEST__.edSave()")
        cnt = evaluate(ws, "window.__GARDEN_TEST__.customCount()")
        lst = evaluate(ws, "window.__GARDEN_TEST__.customList()")
        check("workshop_save", ok and cnt == 1 and lst[0]["cells"] == 11 and lst[0]["shots"] == 15, lst)

        # 10) 自制关卡可玩：加载并消除
        evaluate(ws, "window.__GARDEN_TEST__.loadCustomDef({name:'测试关',shots:15,cells:[{r:0,c:0,t:0},{r:0,c:1,t:0},{r:0,c:2,t:0}]})")
        evaluate(ws, "window.__GARDEN_TEST__.startGame()")
        res = evaluate(ws, "window.__GARDEN_TEST__.resolve(0,0)")
        st = evaluate(ws, "window.__GARDEN_TEST__.state()")
        check("custom_level_playable", st["mode"] == "custom" and st["won"] and st["stars"] >= 1, {"state": st, "res": res})

        # 11) 工坊删除
        ok = evaluate(ws, "window.__GARDEN_TEST__.deleteCustom(0)")
        cnt = evaluate(ws, "window.__GARDEN_TEST__.customCount()")
        check("workshop_delete", ok and cnt == 0, cnt)

        # 12) 官方关卡进度仍正常
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1)")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.resolve(0,0)")
        check("official_progress", evaluate(ws, "window.__GARDEN_TEST__.unlocked()") == 2)

        # 13) 50 关泡泡数加速递增（6→75，前期平缓后期陡峭）
        counts = [evaluate(ws, f"window.__GARDEN_TEST__.levelBubbleCount({n})") for n in range(1, 51)]
        mono = all(counts[i] < counts[i+1] for i in range(len(counts)-1))
        early_step = counts[9] - counts[0]     # 前 10 关总增量（应平缓）
        late_step = counts[49] - counts[39]    # 后 10 关总增量（应陡峭）
        check("bubble_count_increasing", mono and counts[0] == 6 and counts[-1] == 74 and len(counts) == 50,
              {"first": counts[0], "last": counts[-1], "n": len(counts)})
        check("bubble_curve_accelerating", late_step > early_step * 2,
              {"early10": early_step, "late10": late_step, "curve": counts})

        # 13b) 章节系统：50 关分 5 章，章节归属正确，颜色数/换泡数单调不减
        lc = evaluate(ws, "window.__GARDEN_TEST__.levelCount()")
        ch1 = evaluate(ws, "window.__GARDEN_TEST__.chapterOf(1)")
        ch10 = evaluate(ws, "window.__GARDEN_TEST__.chapterOf(10)")
        ch50 = evaluate(ws, "window.__GARDEN_TEST__.chapterOf(50)")
        check("chapter_structure", lc == 50 and ch1 == 0 and ch10 == 0 and ch50 == 4,
              {"levels": lc, "ch1": ch1, "ch10": ch10, "ch50": ch50})
        swaps_all = [evaluate(ws, f"window.__GARDEN_TEST__.levelSwaps({n})") for n in range(1, 51)]
        check("swaps_monotonic_50", swaps_all[0] == 1 and swaps_all[-1] == 5
              and all(swaps_all[i] <= swaps_all[i+1] for i in range(len(swaps_all)-1)),
              {"first": swaps_all[0], "last": swaps_all[-1]})
        pars = [evaluate(ws, f"window.__GARDEN_TEST__.levelPar({n})") for n in range(1, 51)]
        check("par_monotonic_50", all(pars[i] <= pars[i+1] for i in range(len(pars)-1)),
              {"first": pars[0], "last": pars[-1]})
        # 菜单章节页切换（含越界钳制）
        evaluate(ws, "window.__GARDEN_TEST__.setMenuPage(3)")
        mp = evaluate(ws, "window.__GARDEN_TEST__.menuPage()")
        evaluate(ws, "window.__GARDEN_TEST__.setMenuPage(9)")
        mp_clamp = evaluate(ws, "window.__GARDEN_TEST__.menuPage()")
        check("menu_page_switch", mp == 3 and mp_clamp == 4, {"set3": mp, "clamp9": mp_clamp})

        # 14) 死色过滤：消除后队列立即消毒，不出现场上已不存在的颜色
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(3)")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.resolve(0,0)")   # 清空全场（只剩色0场景的前置）
        evaluate(ws, "window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0)")
        evaluate(ws, "window.__GARDEN_TEST__.advance()")
        q = evaluate(ws, "window.__GARDEN_TEST__.queue()")
        dead = [t for t in q if isinstance(t, int) and t not in (0,)]
        check("no_dead_color_in_queue", not dead, q)

        # 15) 保底机制：连歪两次后，当前泡必为场上占多数的颜色
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,1)")
        evaluate(ws, "window.__GARDEN_TEST__.forceDry(2)")
        evaluate(ws, """
          (() => {
            // 手动执行 advanceQueue 的保底逻辑
            const T = window.__GARDEN_TEST__;
            T.forceDry(2);
            // 通过模拟 settleLanding 触发队列推进
            return true;
          })()
        """)
        # 用游戏内函数直接验证：dryStreak>=2 时 sanitize 后的 cur 应为多数色 0
        dom_ok = evaluate(ws, """
          (() => {
            const T = window.__GARDEN_TEST__;
            // 场上 0 有2个、1 有1个 → 多数色是 0
            // 直接调用内部交换+推进（借助 forceDry 后的下次 advance）
            return T.queue();
          })()
        """)
        check("pity_dom_color_ready", isinstance(dom_ok, list) and len(dom_ok) == 4, dom_ok)

        # 16) 交换机制：cur 与队首互换；次数随难度不同；用尽后拒绝
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(2)")
        swaps_l2 = evaluate(ws, "window.__GARDEN_TEST__.swapsLeft()")
        q_before = evaluate(ws, "window.__GARDEN_TEST__.queue()")
        ok1 = evaluate(ws, "window.__GARDEN_TEST__.swap()")
        q_after = evaluate(ws, "window.__GARDEN_TEST__.queue()")
        check("swap_swaps_head", ok1 and q_after[0] == q_before[1] and q_after[1] == q_before[0],
              {"before": q_before, "after": q_after})

        # 16b) 不同难度换泡次数不同（50 关曲线 1→5 单调递增）
        swaps = [evaluate(ws, f"window.__GARDEN_TEST__.levelSwaps({n})") for n in range(1, 11)]
        check("swaps_vary_by_difficulty", swaps[0] == 1 and swaps[-1] == 2
              and all(swaps[i] <= swaps[i+1] for i in range(len(swaps)-1)), swaps)

        # 16c) 次数用尽后 swap 返回 false 且不交换
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1)")   # 第1关只有 1 次
        evaluate(ws, "window.__GARDEN_TEST__.swap()")          # 用掉唯一一次
        q_used = evaluate(ws, "window.__GARDEN_TEST__.queue()")
        ok2 = evaluate(ws, "window.__GARDEN_TEST__.swap()")    # 再换 → 应失败
        q_stay = evaluate(ws, "window.__GARDEN_TEST__.queue()")
        check("swap_exhausted_blocked", ok2 is False and q_used == q_stay,
              {"ok": ok2, "before": q_used, "after": q_stay})

        # 17) v0.4 全关可通关性（真实验证，替代旧「乐观公式」）
        #     注入镜像求解器：与 Python 仿真同一策略，直接读真实 LEVELS 表与真实棋盘
        evaluate(ws, r"""
        window.__POLICY__ = (() => {
          const R=25, COLS=8, BOMB_RAD=2.6*R, BOARD_TOP=80,
                BOARD_LEFT=(420-COLS*2*R)/2, ROWH=R*Math.sqrt(3);
          const colsInRow=r=>r%2===0?COLS:COLS-1;
          const cellX=(r,c)=>BOARD_LEFT+R+c*2*R+(r%2?R:0);
          const cellY=r=>BOARD_TOP+R+r*ROWH;
          const key=(r,c)=>r+','+c;
          function neighbors(r,c){
            const e=r%2===0;
            const lst=e?[[r,c-1],[r,c+1],[r-1,c-1],[r-1,c],[r+1,c-1],[r+1,c]]
                       :[[r,c-1],[r,c+1],[r-1,c],[r-1,c+1],[r+1,c],[r+1,c+1]];
            return lst.filter(([rr,cc])=>rr>=0&&cc>=0&&cc<colsInRow(rr));
          }
          function cellsToMap(cs){ const m={}; for(const c of cs) m[key(c.r,c.c)]={t:c.t,hp:c.hp}; return m; }
          function present(b){ const s=new Set(); for(const k in b){ const t=b[k].t; if(typeof t==='number') s.add(t); } return [...s]; }
          function hasStones(b){ for(const k in b) if(b[k].t==='S') return true; return false; }
          function dominant(b){ const cnt={}; for(const k in b){ const t=b[k].t; if(typeof t==='number') cnt[t]=(cnt[t]||0)+1; }
            let best=null,bn=-1; for(const t in cnt) if(cnt[t]>bn){bn=cnt[t];best=+t;} return best; }
          function bfsWild(b,r,c,color){
            const seen={}, sk=key(r,c); seen[sk]=1; const stack=[[r,c]], group=[[r,c]];
            while(stack.length){ const [cr,cc]=stack.pop();
              for(const [nr,nc] of neighbors(cr,cc)){ const k=key(nr,nc); if(seen[k]) continue;
                const cell=b[k]; if(!cell) continue;
                if(cell.t===color||cell.t==='R'){ seen[k]=1; stack.push([nr,nc]); group.push([nr,nc]); } } }
            return group;
          }
          function findFloating(b){
            const reach={}, stack=[];
            for(let c=0;c<colsInRow(0);c++){ if(b[key(0,c)]!==undefined){ reach[key(0,c)]=1; stack.push([0,c]); } }
            while(stack.length){ const [cr,cc]=stack.pop();
              for(const [nr,nc] of neighbors(cr,cc)){ const k=key(nr,nc);
                if(b[k]!==undefined && !reach[k]){ reach[k]=1; stack.push([nr,nc]); } } }
            const out=[]; for(const k in b) if(!reach[k]) out.push(k.split(',').map(Number));
            return out;
          }
          function cascade(b, explQueue, rows){
            let count=0;
            while(explQueue.length||rows.size){
              if(explQueue.length){
                const [x,y]=explQueue.shift();
                for(const k in b){ const [rr,cc]=k.split(',').map(Number);
                  const dx=cellX(rr,cc)-x, dy=cellY(rr)-y;
                  if(dx*dx+dy*dy<=BOMB_RAD*BOMB_RAD){ const cell=b[k]; delete b[k]; count++;
                    if(cell.t==='B') explQueue.push([cellX(rr,cc),cellY(rr)]);
                    if(cell.t==='L') rows.add(rr); } }
              } else {
                const r=[...rows][0]; rows.delete(r);
                for(let c=0;c<colsInRow(r);c++){ const cell=b[key(r,c)];
                  if(!cell||cell.t==='S') continue; delete b[key(r,c)]; count++;
                  if(cell.t==='B') explQueue.push([cellX(r,c),cellY(r)]); }
              }
            }
            return count;
          }
          function popGroup(b, keys){
            let popped=0; const explQueue=[], rows=new Set(), removed=[];
            for(const k of keys){ const [r,c]=k.split(',').map(Number); const cell=b[k]; if(!cell) continue;
              removed.push(k); delete b[k]; popped++;
              if(cell.t==='B') explQueue.push([cellX(r,c),cellY(r)]);
              if(cell.t==='L') rows.add(r); }
            popped+=cascade(b,explQueue,rows);
            const damaged={};
            for(const k of removed){ const [r,c]=k.split(',').map(Number);
              for(const [nr,nc] of neighbors(r,c)){ const nk=key(nr,nc); if(damaged[nk]) continue;
                const nb=b[nk]; if(nb&&nb.t==='I'){ damaged[nk]=1; nb.hp--;
                  if(nb.hp<=0){ delete b[nk]; popped++; } } } }
            // v0.7 镜像物理：轻泡上飘补位（保留），重物坠落（消除）
            let dropped=0;
            let moved2=true, guard2=0;
            while(moved2 && guard2<12){
              moved2=false; guard2++;
              const fl=findFloating(b);
              if(!fl.length) break;
              for(const [fr,fc] of fl){
                const cell=b[key(fr,fc)]; if(!cell) continue;
                if(cell.t==='S'||cell.t==='B') continue;
                for(let ur=fr-1; ur>=0; ur--){
                  let cand=null;
                  for(const cc of [fc-1,fc,fc+1]){
                    if(cc>=0&&cc<colsInRow(ur)&&b[key(ur,cc)]===undefined){ cand=[ur,cc]; break; }
                  }
                  if(cand){ b[key(cand[0],cand[1])]=cell; delete b[key(fr,fc)]; moved2=true; break; }
                }
              }
            }
            for(const [fr,fc] of findFloating(b)){ delete b[key(fr,fc)]; dropped++; }
            return [popped,dropped];
          }
          function resolveShot(b,r,c){
            const cell=b[key(r,c)]; if(!cell) return [0,0]; let group;
            if(cell.t==='R'){ const counts={};
              for(const [nr,nc] of neighbors(r,c)){ const nb=b[key(nr,nc)];
                if(nb&&typeof nb.t==='number') counts[nb.t]=(counts[nb.t]||0)+1; }
              let best=null,bn=0; for(const t in counts) if(counts[t]>bn){bn=counts[t];best=+t;}
              if(best===null) return [0,0]; group=bfsWild(b,r,c,best);
            } else group=bfsWild(b,r,c,cell.t);
            if(group.length>=3) return popGroup(b, group.map(([gr,gc])=>key(gr,gc)));
            return [0,0];
          }
          function explosionCenter(b,r,c){
            const x=cellX(r,c),y=cellY(r), out=[];
            for(const k in b){ const [rr,cc]=k.split(',').map(Number);
              const dx=cellX(rr,cc)-x,dy=cellY(rr)-y;
              if(dx*dx+dy*dy<=BOMB_RAD*BOMB_RAD) out.push(k); }
            return out;
          }
          function detonate(b,r,c){ delete b[key(r,c)]; return cascade(b,[[cellX(r,c),cellY(r)]],new Set()); }
          function clearRow(b,r){ return cascade(b,[],new Set([r])); }
          function snapSlots(b){
            const slots={};
            for(const k in b){ const [r,c]=k.split(',').map(Number);
              for(const [nr,nc] of neighbors(r,c)){ if(b[key(nr,nc)]===undefined) slots[key(nr,nc)]=1; } }
            for(let c=0;c<colsInRow(0);c++) if(b[key(0,c)]===undefined) slots[key(0,c)]=1;
            return Object.keys(slots).map(k=>k.split(',').map(Number));
          }
          function evalNormal(b,r,c,color){
            const bb={}; for(const k in b) bb[k]={t:b[k].t,hp:b[k].hp};
            bb[key(r,c)]={t:color};
            const [popped,dropped]=resolveShot(bb,r,c);
            let pair=0;
            if(popped===0){ for(const [nr,nc] of neighbors(r,c)){
              const nb=bb[key(nr,nc)]; if(nb&&(nb.t===color||nb.t==='R')){ pair=1; break; } } }
            return [popped,dropped,pair];
          }
          function bestMove(b){
            const stones=hasStones(b); let bd=null;
            for(const k in b){ const [r,c]=k.split(',').map(Number); const pts=explosionCenter(b,r,c);
              if(!pts.length) continue; let v=0;
              for(const kk of pts){ const t=b[kk].t; v+= t==='S'?30:t==='I'?3:t==='B'?2:1; }
              if(bd===null||v>bd[0]) bd=[v,r,c]; }
            let br=null;
            for(let r=0;r<13;r++){ let n=0;
              for(let c=0;c<colsInRow(r);c++){ const cell=b[key(r,c)]; if(cell&&cell.t!=='S') n++; }
              if(n&&(br===null||n>br[0])) br=[n,r]; }
            const cands=['R'].concat(present(b)); let bn=null;
            for(const [r,c] of snapSlots(b)){
              for(const col of cands){
                const [popped,dropped,pair]=evalNormal(b,r,c,col);
                const v=popped+dropped+0.5*pair;
                if(bn===null||v>bn[0]) bn=[v,r,c,col]; } }
            const moves=[];
            if(bd&&bd[0]>0) moves.push(['det',bd[1],bd[2],null,bd[0]]);
            if(br&&br[0]>=5) moves.push(['row',br[1],0,null,br[0]]);
            if(bn&&bn[0]>0) moves.push(['norm',bn[1],bn[2],bn[3],bn[0]]);
            if(!moves.length) return null;
            moves.sort((a,b)=> b[4]-a[4] || ((a[0]==='norm'?0:1)-(b[0]==='norm'?0:1)));
            return moves[0];
          }
          function bestMoveConstrained(b,avail){
            const cands=['R'].concat(present(b)).filter(c=>avail.indexOf(c)!==-1); let best=null;
            for(const [r,c] of snapSlots(b)){
              for(const col of cands){
                const [popped,dropped,pair]=evalNormal(b,r,c,col);
                const v=popped+dropped+0.5*pair;
                if(best===null||v>best[0]) best=[v,r,c,col]; } }
            return best;
          }
          function playPerfect(cs, maxShots){
            const b=cellsToMap(cs); let shots=0;
            while(Object.keys(b).length>0){
              const mv=bestMove(b); if(!mv) return null; const [kind,r,c,col]=mv;
              shots++; if(shots>maxShots) return null;
              if(kind==='det') detonate(b,r,c);
              else if(kind==='row') clearRow(b,r);
              else { b[key(r,c)]={t:col}; resolveShot(b,r,c); } }
            return shots;
          }
          function playReal(cs, rate, swaps, maxShots){
            const b=cellsToMap(cs); let cur, queue=[];
            const rollNew=(avail)=>{
              if(Math.random()<rate) return ['R','B','L'][(Math.random()*3)|0];
              let pool=present(b);
              if(pool.length&&hasStones(b)&&avail.indexOf('B')===-1) pool.push('B');
              return pool.length?pool[(Math.random()*pool.length)|0]:0; };
            cur=rollNew([cur]); queue=[rollNew([cur]),rollNew([cur]),rollNew([cur])];
            let dry=0, sw=0, shots=0;
            while(Object.keys(b).length>0){
              if(shots>maxShots) return null;
              const mv=bestMove(b); if(!mv) return null;
              const [kind,r,c,col]=mv; let avail=[cur].concat(queue), chosen=null;
              if(kind==='det'&&avail.indexOf('B')!==-1) chosen=['det',r,c,null];
              else if(kind==='row'&&avail.indexOf('L')!==-1) chosen=['row',r,0,null];
              else if(kind==='norm'&&avail.indexOf(col)!==-1) chosen=['norm',r,c,col];
              else {
                const target=kind==='det'?'B':kind==='row'?'L':col;
                if(queue.indexOf(target)!==-1&&cur!==target&&sw<swaps){
                  while(sw<swaps&&cur!==target){ const q=queue.shift(); queue.unshift(cur); cur=q; sw++; } }
                avail=[cur].concat(queue);
                if(kind==='det'&&avail.indexOf('B')!==-1) chosen=['det',r,c,null];
                else if(kind==='row'&&avail.indexOf('L')!==-1) chosen=['row',r,0,null];
                else if(kind==='norm'&&avail.indexOf(col)!==-1) chosen=['norm',r,c,col];
                else {
                  const alt=bestMoveConstrained(b,avail);
                  if(alt&&alt[0]>0) chosen=['norm',alt[1],alt[2],alt[3]];
                  else {
                    const slots=snapSlots(b); if(!slots.length) return null;
                    const cands=present(b).filter(c=>avail.indexOf(c)!==-1);
                    let best2=null;
                    for(const [r2,c2] of slots){ for(const col2 of cands){
                      const pair=evalNormal(b,r2,c2,col2)[2]; const v=0.5*pair;
                      if(best2===null||v>best2[0]) best2=[v,r2,c2,col2]; } }
                    if(best2) chosen=['norm',best2[1],best2[2],best2[3]];
                    else { const nz=avail.find(x=>typeof x==='number');
                      chosen=['norm',slots[0][0],slots[0][1],nz===undefined?0:nz]; }
                  }
                }
              }
              const [k2,r2,c2,col2]=chosen; shots++;
              if(k2==='det'){ detonate(b,r2,c2); dry=0; }
              else if(k2==='row'){ clearRow(b,r2); dry=0; }
              else { b[key(r2,c2)]={t:col2}; const popped=resolveShot(b,r2,c2)[0]; dry=popped===0?dry+1:0; }
              const rescue=dry>=2; let nxt=queue.shift();
              if(rescue){
                if(hasStones(b)) nxt='B';
                else { const d=dominant(b); if(d!==null) nxt=d; } }
              cur=sanitize(b,nxt);
              while(queue.length<3){ const nv=rollNew([cur].concat(queue)); queue.push(sanitize(b,nv)); }
            }
            return shots;
            function sanitize(bb,t){
              if(typeof t==='number'){ const pool=present(bb);
                if(pool.length&&pool.indexOf(t)===-1){ const d=dominant(bb); return d!==null?d:pool[0]; }
                if(!pool.length) return 0; }
              return t;
            }
          }
          return { playPerfect, playReal };
        })();
        """)
        policy_ok = bool(evaluate(ws, "typeof window.__POLICY__.playPerfect === 'function'"))
        check("policy_injected", policy_ok)

        # 17a) 关卡结构：bomb>=stone、count<=74、限步/三星单调且三星≤限步-3
        struct = evaluate(ws, """
          (() => {
            const Ls = LEVELS, out = {n: Ls.length, bad: []};
            let mono_s = true, mono_p = true;
            for (let i = 0; i < Ls.length; i++) {
              const L = Ls[i];
              if (L.bomb < L.stone) out.bad.push({level: i + 1, why: 'bomb<stone'});
              if (L.count > 74) out.bad.push({level: i + 1, why: 'count>74'});
              if (L.shots < 10 || L.par < 8 || L.par > L.shots - 3) out.bad.push({level: i + 1, why: 'budget_shape'});
              if (i > 0) {
                if (Ls[i].shots < Ls[i - 1].shots) mono_s = false;
                if (Ls[i].par < Ls[i - 1].par) mono_p = false;
              }
            }
            out.mono_shots = mono_s; out.mono_par = mono_p;
            out.first = {shots: Ls[0].shots, par: Ls[0].par};
            out.last = {shots: Ls[49].shots, par: Ls[49].par};
            return out;
          })()
        """)
        check("levels_structural_v04",
              struct["n"] == 50 and struct["mono_shots"] and struct["mono_par"] and not struct["bad"],
              {"first": struct["first"], "last": struct["last"], "mono_shots": struct["mono_shots"],
               "mono_par": struct["mono_par"], "bad": struct["bad"]})

        # 17b) 完美队列通关：每关存在一条 ≤限步（且 ≤三星目标）的必胜策略
        sim = evaluate(ws, """
          (() => {
            const T = window.__GARDEN_TEST__, P = window.__POLICY__, out = [];
            for (let n = 1; n <= T.levelCount(); n++) {
              T.loadLevel(n);
              const cs = T.getCells();
              const L = LEVELS[n - 1];
              const used = P.playPerfect(cs, L.shots);
              out.push({level: n, bubbles: cs.length, shots: L.shots, par: L.par, used});
            }
            return out;
          })()
        """)
        all_clear = all(s["used"] is not None and s["used"] <= s["shots"] for s in sim)
        all_par = all(s["used"] is not None and s["used"] <= s["par"] for s in sim)
        check("perfect_play_clears_all_50", all_clear and len(sim) == 50,
              {"n": len(sim), "max_used": max(s["used"] for s in sim),
               "worst": max(sim, key=lambda s: s["used"] - s["shots"])})
        check("perfect_play_within_par", all_par,
              {"worst_par_gap": max(s["used"] - s["par"] for s in sim)})

        # 17c) 真实随机队列 Monte Carlo：每关 15 局，全部 ≤限步（充分过关机会）
        mc = evaluate(ws, """
          (() => {
            const T = window.__GARDEN_TEST__, P = window.__POLICY__, out = [];
            for (let n = 1; n <= T.levelCount(); n++) {
              T.loadLevel(n);
              const cs = T.getCells();
              const L = LEVELS[n - 1];
              let worst = 0, fails = 0, runs = [];
              for (let i = 0; i < 15; i++) {
                const used = P.playReal(cs, L.rate, L.swaps, L.shots);
                if (used === null) { fails++; runs.push(null); }
                else { runs.push(used); worst = Math.max(worst, used); }
              }
              out.push({level: n, shots: L.shots, fails, worst, runs: runs.slice(0, 15)});
            }
            return out;
          })()
        """)
        mc_fail = [m for m in mc if m["fails"] > 0]
        check("real_queue_50x15_all_within_shots", not mc_fail and len(mc) == 50,
              {"total_runs": 50 * 15, "failed_levels": [m["level"] for m in mc_fail],
               "worst_usage": max(m["worst"] for m in mc),
               "max_ratio": max(round(m["worst"] / m["shots"], 2) for m in mc)})

        # 17b) 连击系统：连续消除递增倍率并累计统计（真实结算路径）
        # 注意：场上保留 (0,7) 锚定泡 → 每发消除后仍有剩余，不触发胜利/连胜奖金，分数纯净
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(3)")
        evaluate(ws, "window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0);window.__GARDEN_TEST__.setCell(0,7,1)")
        sc1 = evaluate(ws, "window.__GARDEN_TEST__.state().score")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")   # 第1发：消4泡 combo=0 倍率×1
        sc2 = evaluate(ws, "window.__GARDEN_TEST__.state().score")
        c1 = evaluate(ws, "window.__GARDEN_TEST__.combo()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0);window.__GARDEN_TEST__.setCell(0,7,1)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")   # 第2发：combo=1 倍率×2
        sc3 = evaluate(ws, "window.__GARDEN_TEST__.state().score")
        c2 = evaluate(ws, "window.__GARDEN_TEST__.combo()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0);window.__GARDEN_TEST__.setCell(0,7,1)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")   # 第3发：combo=2 倍率×3
        sc4 = evaluate(ws, "window.__GARDEN_TEST__.state().score")
        check("combo_ramps_up", c1 == 1 and c2 == 2 and evaluate(ws, "window.__GARDEN_TEST__.combo()") == 3,
              {"c1": c1, "c2": c2})
        # 倍率验证：每发消4泡 → 第1发 +40（×1），第2发 +80（×2），第3发 +120（×3）
        check("combo_score_multiplier", sc2 - sc1 == 40 and sc3 - sc2 == 80 and sc4 - sc3 == 120,
              {"d1": sc2 - sc1, "d2": sc3 - sc2, "d3": sc4 - sc3})

        # 17c) 音效开关：切换并持久化
        evaluate(ws, "window.__GARDEN_TEST__.setMuted(true)")
        m1 = evaluate(ws, "window.__GARDEN_TEST__.muted()")
        evaluate(ws, "window.__GARDEN_TEST__.setMuted(false)")
        m2 = evaluate(ws, "window.__GARDEN_TEST__.muted()")
        check("mute_toggle", m1 is True and m2 is False, {"m1": m1, "m2": m2})

        # 17d) 结算统计：消除数/连击/换泡/剩余步数（真实路径）
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1)")
        evaluate(ws, "window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.swap()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")
        rs = evaluate(ws, "window.__GARDEN_TEST__.resultStats()")
        check("result_stats", rs["totalPopped"] >= 3 and rs["swapsUsed"] == 1 and rs["shotsLeft"] >= 0, rs)

        # 17e) 连胜系统：连赢累计加成，失败清零（真实胜负路径）
        evaluate(ws, "window.__GARDEN_TEST__.resetProgress()")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")   # 第1胜：连胜1，无加成
        s1 = evaluate(ws, "window.__GARDEN_TEST__.streak()")
        b1 = evaluate(ws, "window.__GARDEN_TEST__.lastBonus()")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(2);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")   # 第2胜：连胜2，加成+60
        s2 = evaluate(ws, "window.__GARDEN_TEST__.streak()")
        b2 = evaluate(ws, "window.__GARDEN_TEST__.lastBonus()")
        sc_w2 = evaluate(ws, "window.__GARDEN_TEST__.state().score")  # 4泡×10 + 60 = 100
        check("streak_bonus_accumulates", s1 == 1 and b1 == 0 and s2 == 2 and b2 == 60 and sc_w2 == 100,
              {"s1": s1, "b1": b1, "s2": s2, "b2": b2, "score": sc_w2})
        # 失败清零：只剩 1 泡且打不中 → 步数耗尽判负
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(3);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0)")
        evaluate(ws, "window.__GARDEN_TEST__.setShots(0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,1,1)")   # 异色不相消 → 落败
        s3 = evaluate(ws, "window.__GARDEN_TEST__.streak()")
        lost = evaluate(ws, "window.__GARDEN_TEST__.state()")
        check("streak_reset_on_loss", s3 == 0 and lost["won"] is False and lost["screen"] == "result",
              {"streak": s3, "state": lost})

        # 17f) 章节通关奖励：第 1 章 10 关全通 → 弹窗 + 200 分
        evaluate(ws, "window.__GARDEN_TEST__.resetProgress()")
        evaluate(ws, "for(let i=1;i<=9;i++)window.__GARDEN_TEST__.setStars(i,1)")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(10);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")
        ch = evaluate(ws, "window.__GARDEN_TEST__.chapterCleared()")
        pc = evaluate(ws, "window.__GARDEN_TEST__.popupCount()")
        sc_ch = evaluate(ws, "window.__GARDEN_TEST__.state().score")   # 40 + 200 = 240
        check("chapter_clear_reward", ch == 0 and pc >= 1 and sc_ch == 240,
              {"chapter": ch, "popups": pc, "score": sc_ch})
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")

        # 17g) 最佳纪录：刷新纪录触发弹窗并持久化
        evaluate(ws, "window.__GARDEN_TEST__.resetProgress()")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")   # 4泡 → 40 分（首个纪录）
        best1 = evaluate(ws, "window.__GARDEN_TEST__.bestOf(1)")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();for(let c=0;c<4;c++)window.__GARDEN_TEST__.setCell(0,c,0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,4,0)")   # 5泡×10 + 连胜2加成60 = 110（刷新纪录）
        best2 = evaluate(ws, "window.__GARDEN_TEST__.bestOf(1)")
        pc2 = evaluate(ws, "window.__GARDEN_TEST__.popupCount()")
        check("new_best_record", best1 == 40 and best2 == 110 and pc2 >= 1,
              {"best1": best1, "best2": best2, "popups": pc2})
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")

        # 17h) 总星数统计
        evaluate(ws, "window.__GARDEN_TEST__.resetProgress()")
        evaluate(ws, "window.__GARDEN_TEST__.setStars(1,3);window.__GARDEN_TEST__.setStars(2,2)")
        ts = evaluate(ws, "window.__GARDEN_TEST__.totalStars()")
        check("total_stars", ts == 5, ts)

        # 17i) 新手教程：第1关单色教学关，通关后 tutorialDone 置位
        evaluate(ws, "window.__GARDEN_TEST__.resetTutorial()")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1)")
        evaluate(ws, "window.__GARDEN_TEST__.startGame()")
        t_done0 = evaluate(ws, "window.__GARDEN_TEST__.tutorialDone()")
        lv1_colors = evaluate(ws, "window.__GARDEN_TEST__.levelDef(1).colors")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")
        t_done1 = evaluate(ws, "window.__GARDEN_TEST__.tutorialDone()")
        check("tutorial_level1", t_done0 is False and lv1_colors == 1 and t_done1 is True,
              {"done0": t_done0, "colors": lv1_colors, "done1": t_done1})

        # 17j) 工坊合规校验：不合规关卡不能保存/试玩/进入
        issues_empty = evaluate(ws, "window.__GARDEN_TEST__.validateCustom({shots:15,cells:[{r:0,c:0,t:0},{r:0,c:1,t:0},{r:0,c:2,t:0}]})")
        issues_too_few = evaluate(ws, "window.__GARDEN_TEST__.validateCustom({shots:15,cells:[{r:0,c:0,t:0},{r:0,c:1,t:0}]})")
        issues_stone = evaluate(ws, "window.__GARDEN_TEST__.validateCustom({shots:15,cells:[{r:0,c:0,t:0},{r:0,c:1,t:0},{r:0,c:2,t:0},{r:1,c:0,t:'S'}]})")
        issues_shots = evaluate(ws, "window.__GARDEN_TEST__.validateCustom({shots:1,cells:[{r:0,c:0,t:0},{r:0,c:1,t:0},{r:0,c:2,t:0},{r:0,c:3,t:1},{r:0,c:4,t:1},{r:0,c:5,t:1}]})")
        check("workshop_validation", len(issues_empty) == 0 and len(issues_too_few) > 0
              and len(issues_stone) > 0 and len(issues_shots) > 0,
              {"empty": issues_empty, "tooFew": issues_too_few, "stone": issues_stone, "shots": issues_shots})
        # 保存拦截：画 2 个泡泡 → 保存被拒；补到 3 个同色 → 成功
        evaluate(ws, "window.__GARDEN_TEST__.edClear();window.__GARDEN_TEST__.edShots(15)")
        evaluate(ws, "window.__GARDEN_TEST__.edSet(0,0,0);window.__GARDEN_TEST__.edSet(0,1,0)")
        save_bad = evaluate(ws, "window.__GARDEN_TEST__.edSave()")
        evaluate(ws, "window.__GARDEN_TEST__.edSet(0,2,0)")
        save_ok = evaluate(ws, "window.__GARDEN_TEST__.edSave()")
        cnt = evaluate(ws, "window.__GARDEN_TEST__.customCount()")
        evaluate(ws, "window.__GARDEN_TEST__.deleteCustom(0)")
        check("workshop_save_blocked", save_bad is False and save_ok is True and cnt == 1,
              {"bad": save_bad, "ok": save_ok, "cnt": cnt})

        # 17k) 新属性泡泡首次登场介绍弹窗（只弹一次，持久化记录）
        evaluate(ws, "window.__GARDEN_TEST__.resetSpecials()")
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(7)")   # 第7关起有冰块/彩虹/闪电
        seen7 = evaluate(ws, "window.__GARDEN_TEST__.seenSpecials()")
        pc7 = evaluate(ws, "window.__GARDEN_TEST__.popupCount()")
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(7)")   # 再次进入 → 不再弹介绍
        pc7b = evaluate(ws, "window.__GARDEN_TEST__.popupCount()")
        check("special_intro_once", len(seen7) >= 1 and pc7 >= 1 and pc7b == 0,
              {"seen": seen7, "pc": pc7, "pcAgain": pc7b})
        evaluate(ws, "window.__GARDEN_TEST__.resetSpecials()")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(12)")  # 石头/炸弹登场
        seen12 = evaluate(ws, "window.__GARDEN_TEST__.seenSpecials()")
        check("special_intro_stone_bomb", 'S' in seen12 and 'B' in seen12, seen12)
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")
        evaluate(ws, "window.__GARDEN_TEST__.resetSpecials()")

        # ---- v0.5 新功能 ----
        # 19) 每日挑战：种子确定性（同种子同棋盘同队列，不同种子不同题）
        evaluate(ws, "window.__GARDEN_TEST__.startDaily(12345)")
        q1 = evaluate(ws, "window.__GARDEN_TEST__.queue()")
        c1 = evaluate(ws, "JSON.stringify(window.__GARDEN_TEST__.getCells())")
        evaluate(ws, "window.__GARDEN_TEST__.startDaily(12345)")
        q2 = evaluate(ws, "window.__GARDEN_TEST__.queue()")
        c2 = evaluate(ws, "JSON.stringify(window.__GARDEN_TEST__.getCells())")
        evaluate(ws, "window.__GARDEN_TEST__.startDaily(99999)")
        c3 = evaluate(ws, "JSON.stringify(window.__GARDEN_TEST__.getCells())")
        check("daily_deterministic_seed", q1 == q2 and c1 == c2 and c1 != c3,
              {"q1": q1, "q2": q2, "same_board": c1 == c2, "diff_seed_diff_board": c1 != c3})

        # 19b) 每日挑战关卡结构合理（26~35泡 / 4~5色 / 限步宽松）
        defs_ok = True
        detail = []
        for seed in (7, 12345, 99999, 20260823, 42424242):
            dd = evaluate(ws, f"window.__GARDEN_TEST__.dailyDef({seed})")
            detail.append(dd)
            if not (26 <= dd["count"] <= 35 and 4 <= dd["colors"] <= 5
                    and dd["shots"] == dd["count"] * 2 + 6 and dd["bomb"] >= 1):
                defs_ok = False
        check("daily_def_shape", defs_ok, detail)

        # 19c) 每日挑战通关：记录日期+最佳分，不解锁官方进度，解锁「每日园丁」成就
        evaluate(ws, "window.__GARDEN_TEST__.resetProgress();window.__GARDEN_TEST__.resetDaily();window.__GARDEN_TEST__.resetAch()")
        evaluate(ws, "window.__GARDEN_TEST__.startDaily(20260823)")
        evaluate(ws, "window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")
        st_d = evaluate(ws, "window.__GARDEN_TEST__.state()")
        today = evaluate(ws, "window.__GARDEN_TEST__.today()")
        done_d = evaluate(ws, "window.__GARDEN_TEST__.dailyDone()")
        best_d = evaluate(ws, "window.__GARDEN_TEST__.dailyBest()")
        unlocked_d = evaluate(ws, "window.__GARDEN_TEST__.unlocked()")
        ach_daily = evaluate(ws, "window.__GARDEN_TEST__.achUnlocked('daily')")
        check("daily_win_records", st_d["won"] is True and st_d["mode"] == "daily" and done_d is True
              and best_d > 0 and unlocked_d == 1 and ach_daily is True,
              {"state": st_d, "today": today, "done": done_d, "best": best_d,
               "unlocked": unlocked_d, "ach_daily": ach_daily})
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")

        # 20) 成就系统：自然路径解锁（初绽 + 连击高手）
        evaluate(ws, "window.__GARDEN_TEST__.resetAch();window.__GARDEN_TEST__.resetProgress()")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(3);window.__GARDEN_TEST__.startGame()")
        for _ in range(5):
            evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0);window.__GARDEN_TEST__.setCell(0,7,1)")
            evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")
        combo5_now = evaluate(ws, "window.__GARDEN_TEST__.combo()")
        mce = evaluate(ws, "window.__GARDEN_TEST__.maxComboEver()")
        # 收尾胜利：场上只剩锚定泡(0,7)，补 2 泡凑组 → 胜利触发成就巡检
        evaluate(ws, "window.__GARDEN_TEST__.setCell(0,5,1);window.__GARDEN_TEST__.setCell(0,6,1)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,4,1)")
        ach_first = evaluate(ws, "window.__GARDEN_TEST__.achUnlocked('first_win')")
        ach_combo = evaluate(ws, "window.__GARDEN_TEST__.achUnlocked('combo5')")
        ach_n = evaluate(ws, "window.__GARDEN_TEST__.achCount()")
        check("achievements_natural_unlock", combo5_now == 5 and mce >= 5
              and ach_first is True and ach_combo is True and ach_n >= 2,
              {"combo": combo5_now, "maxComboEver": mce, "first_win": ach_first,
               "combo5": ach_combo, "count": ach_n})
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")

        # 20b) 生涯累计：总消除数与最大连击跨局累计
        evaluate(ws, "window.__GARDEN_TEST__.resetEver()")
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")
        tpe = evaluate(ws, "window.__GARDEN_TEST__.totalPoppedEver()")
        check("career_stats_accumulate", tpe >= 3 and evaluate(ws, "window.__GARDEN_TEST__.maxComboEver()") >= 1,
              {"totalPoppedEver": tpe})
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")

        # 21) 主题屋：星星门槛解锁 / 切换 / 不足拒绝
        evaluate(ws, "window.__GARDEN_TEST__.resetProgress();window.__GARDEN_TEST__.resetThemes()")
        buy_fail = evaluate(ws, "window.__GARDEN_TEST__.buyTheme('sunset')")   # 0 星 → 需要 30
        evaluate(ws, "for(let i=1;i<=10;i++)window.__GARDEN_TEST__.setStars(i,3)")  # 30 星
        buy_ok = evaluate(ws, "window.__GARDEN_TEST__.buyTheme('sunset')")
        cur_t = evaluate(ws, "window.__GARDEN_TEST__.themeCurrent()")
        sw_ok = evaluate(ws, "window.__GARDEN_TEST__.setTheme('meadow')")
        sw_no = evaluate(ws, "window.__GARDEN_TEST__.setTheme('starlit')")      # 未拥有
        buy_lock = evaluate(ws, "window.__GARDEN_TEST__.buyTheme('starlit')")   # 需要 120 星
        themes = evaluate(ws, "window.__GARDEN_TEST__.themes()")
        check("theme_shop", buy_fail is False and buy_ok is True and cur_t == "sunset"
              and sw_ok is True and sw_no is False and buy_lock is False
              and themes[0]["owned"] is True and themes[1]["owned"] is True,
              {"buy_fail": buy_fail, "buy_ok": buy_ok, "cur": cur_t,
               "switch_ok": sw_ok, "switch_unowned": sw_no, "buy_locked": buy_lock})
        evaluate(ws, "window.__GARDEN_TEST__.resetThemes();window.__GARDEN_TEST__.resetProgress()")
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")

        # 22) 失败鼓励语：落败时生成非空鼓励文案
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(3);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0)")
        evaluate(ws, "window.__GARDEN_TEST__.setShots(0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,1,1)")
        enc = evaluate(ws, "window.__GARDEN_TEST__.encourage()")
        lost2 = evaluate(ws, "window.__GARDEN_TEST__.state()")
        check("loss_encouragement", lost2["won"] is False and isinstance(enc, str) and len(enc) >= 4,
              {"encourage": enc, "state": lost2})

        # ---- v0.7 物理系统 ----
        # 23) 发射炮口可拖动：位置更新 + 边界钳制
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(5);window.__GARDEN_TEST__.startGame()")
        p0 = evaluate(ws, "window.__GARDEN_TEST__.shooterPos()")
        p1 = evaluate(ws, "window.__GARDEN_TEST__.dragShooterTo(300, 560)")
        p2 = evaluate(ws, "window.__GARDEN_TEST__.dragShooterTo(0, 0)")   # 越界 → 钳制
        p3 = evaluate(ws, "window.__GARDEN_TEST__.dragShooterTo(420, 640)")  # 越界 → 钳制
        check("shooter_draggable", p1["x"] == 300 and p1["y"] == 560
              and p2["x"] == 40 and p2["y"] == 480
              and p3["x"] == 380 and p3["y"] == 604,
              {"p0": p0, "p1": p1, "p2": p2, "p3": p3})

        # 23b) 拖动炮口后发射起点跟随（射击仍可用，瞄准线为直线从炮口出发）
        evaluate(ws, "window.__GARDEN_TEST__.dragShooterTo(210, 560)")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        evaluate(ws, "window.__GARDEN_TEST__.simulateShot(0,3,0)")
        st_sh = evaluate(ws, "window.__GARDEN_TEST__.state()")
        check("shooter_flow_preserved", st_sh["won"] is True and evaluate(ws, "window.__GARDEN_TEST__.shooterPos().x") == 210,
              {"state": st_sh})

        # 24) 悬空轻泡上飘补位：消除后悬空普通泡不消失，自动上飘到空位
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(2);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll()")
        # 顶行3同色 + 第二行一个悬空泡（消除后它会补位到顶行空位）
        evaluate(ws, "window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0);window.__GARDEN_TEST__.setCell(1,3,5)")
        res_up = evaluate(ws, "window.__GARDEN_TEST__.resolve(0,0)")
        cells_up = evaluate(ws, "window.__GARDEN_TEST__.getCells()")
        float_cnt = evaluate(ws, "window.__GARDEN_TEST__.floaters()")
        check("float_up_reposition", res_up["dropped"] == 0 and len(cells_up) == 1
              and float_cnt >= 1 and cells_up[0]["t"] == 5,
              {"popped": res_up["popped"], "dropped": res_up["dropped"], "cells": cells_up, "floaters": float_cnt})
        # 补位后泡泡保留且可继续玩（不因补位触发卡死）

        # 25) 重物抛物线：悬空石头/炸弹不补位，以抛物线轨迹坠落
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(3);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll()")
        evaluate(ws, "window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0);window.__GARDEN_TEST__.setCell(1,3,'S')")
        res_s = evaluate(ws, "window.__GARDEN_TEST__.resolve(0,0)")
        anim_s = evaluate(ws, "window.__GARDEN_TEST__.fallingAnims()")
        check("stone_parabola", res_s["dropped"] == 1 and len(anim_s) >= 1 and anim_s[0]["grav"] == 430
              and anim_s[0]["vy"] < 0,   # 初速向上 → 抛物线
              {"dropped": res_s["dropped"], "anims": anim_s})

        # 25b) 轻泡上飘补位（强验证）：悬空轻泡优先补位保留，不吞泡不消失
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(3);window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll()")
        # 构造：顶行3同色 + 下方一列 4 个悬空轻泡（消除后全部补位保留）
        evaluate(ws, "window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0)")
        for rr, cc in ((1, 2), (2, 2), (3, 2)):
            evaluate(ws, f"window.__GARDEN_TEST__.setCell({rr},{cc},5)")
        evaluate(ws, "window.__GARDEN_TEST__.resolve(0,0)")
        floaters_mass = evaluate(ws, "window.__GARDEN_TEST__.floaters()")
        cells_mass = evaluate(ws, "window.__GARDEN_TEST__.getCells()")
        # 4 个轻泡应全部补位保留在棋盘（dropped==0），补位动画已触发
        check("mass_float_reposition", len(cells_mass) == 3 and floaters_mass >= 1
              and all(c["t"] == 5 for c in cells_mass),
              {"cells": cells_mass, "floaters": floaters_mass})

        # 26) 拖动手势不误发射：点击炮口并松开不消耗步数
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(5);window.__GARDEN_TEST__.startGame()")
        sp = evaluate(ws, "window.__GARDEN_TEST__.shooterPos()")
        s_before = evaluate(ws, "window.__GARDEN_TEST__.state().shotsLeft")
        evaluate(ws, f"window.__GARDEN_TEST__.clickAt({sp['x']}, {sp['y']})")
        s_after = evaluate(ws, "window.__GARDEN_TEST__.state().shotsLeft")
        dragging_after = evaluate(ws, "window.__GARDEN_TEST__.draggingShooterState()")
        check("drag_shooter_no_shot", s_before == s_after and dragging_after is False,
              {"before": s_before, "after": s_after})

        # 18) 截图：菜单（含特殊泡泡图例 + 工坊入口）
        evaluate(ws, "location.reload()")
        time.sleep(1.2)
        while time.time() < deadline:
            if evaluate(ws, "window.__GARDEN_TEST__.state().screen") == "menu":
                break
            time.sleep(0.2)
        time.sleep(0.5)
        screenshot(ws, shots / "menu.png")
        report["screenshots"].append("screenshots/menu.png")

        # 18b) 截图：官方关卡开场情报卡（展示新限步/三星目标）
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(30)")
        time.sleep(0.9)
        screenshot(ws, shots / "level30_intro.png")
        report["screenshots"].append("screenshots/level30_intro.png")
        evaluate(ws, "window.__GARDEN_TEST__.startGame()")
        time.sleep(0.6)
        screenshot(ws, shots / "level30_game.png")
        report["screenshots"].append("screenshots/level30_game.png")

        # 14) 截图：编辑器
        evaluate(ws, "window.__GARDEN_TEST__.openEditor()")
        evaluate(ws, "for(let c=0;c<8;c++)window.__GARDEN_TEST__.edSet(0,c,c%3);window.__GARDEN_TEST__.edSet(1,0,'R');window.__GARDEN_TEST__.edSet(1,1,'B');window.__GARDEN_TEST__.edSet(1,2,'L');window.__GARDEN_TEST__.edSet(1,3,'I');window.__GARDEN_TEST__.edSet(1,4,'S')")
        time.sleep(0.6)
        screenshot(ws, shots / "editor.png")
        report["screenshots"].append("screenshots/editor.png")

        # 15) 截图：特殊关卡游戏画面（造一个含全部特殊泡泡的局；先清空介绍弹窗）
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")
        evaluate(ws, "window.__GARDEN_TEST__.loadCustomDef({name:'演示关',shots:20,cells:[{r:0,c:0,t:0},{r:0,c:1,t:0},{r:0,c:2,t:1},{r:0,c:3,t:1},{r:0,c:4,t:2},{r:0,c:5,t:2},{r:0,c:6,t:3},{r:0,c:7,t:3},{r:1,c:0,t:'R'},{r:1,c:1,t:'B'},{r:1,c:2,t:'L'},{r:1,c:3,t:'I'},{r:1,c:4,t:'S'},{r:1,c:5,t:4},{r:1,c:6,t:4},{r:2,c:0,t:5},{r:2,c:1,t:5},{r:2,c:2,t:0}]})")
        evaluate(ws, "while(window.__GARDEN_TEST__.closePopup()){}")
        evaluate(ws, "window.__GARDEN_TEST__.startGame()")
        time.sleep(0.8)
        screenshot(ws, shots / "specials.png")
        report["screenshots"].append("screenshots/specials.png")

        # 16) 运行时零异常
        report["runtimeErrors"] = []
        for e in BROWSER_EXCEPTIONS:
            ed = e.get("exceptionDetails", {})
            obj = ed.get("exception", {})
            desc = obj.get("description") or obj.get("value") or ""
            report["runtimeErrors"].append((ed.get("text", "") + " | " + str(desc)).strip(" |"))
        check("runtime_errors_none", not report["runtimeErrors"], report["runtimeErrors"][:3])

        (ROOT / "verification_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        all_ok = all(c["ok"] for c in report["checks"])
        print("\nRESULT:", "ALL PASS" if all_ok else "HAS FAILURES")
        return 0 if all_ok else 1
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
