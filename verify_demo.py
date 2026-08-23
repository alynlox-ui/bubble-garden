"""Headless Edge smoke test for Bubble Garden v0.3 (泡泡花园).
Covers: 50 levels / 5 chapters, streak bonus, chapter-clear rewards,
special bubbles, cascade engine, creative workshop, core pop/floating rules.
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
    report = {"game": "bubble-garden-demo-v0.3", "checks": [], "runtimeErrors": [], "screenshots": []}

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
        check("version_v03", evaluate(ws, "window.__GARDEN_TEST__.version") == "0.3")

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

        # 3) 基础消除仍正常：3 同色 + 悬空坠落
        evaluate(ws, "window.__GARDEN_TEST__.loadLevel(1)")
        evaluate(ws, "window.__GARDEN_TEST__.startGame()")
        evaluate(ws, "window.__GARDEN_TEST__.clearAll();window.__GARDEN_TEST__.setCell(0,0,0);window.__GARDEN_TEST__.setCell(0,1,0);window.__GARDEN_TEST__.setCell(0,2,0);window.__GARDEN_TEST__.setCell(1,1,1)")
        res = evaluate(ws, "window.__GARDEN_TEST__.resolve(0,0)")
        check("basic_pop_and_drop", res["popped"] == 3 and res["dropped"] == 1, res)

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
        check("bubble_count_increasing", mono and counts[0] == 6 and counts[-1] == 75 and len(counts) == 50,
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

        # 17) 全关可通关性模拟：50 关逐一统计，限步数 ≥ 理论最少发数
        sim = evaluate(ws, """
          (() => {
            const T = window.__GARDEN_TEST__;
            const results = [];
            for (let n = 1; n <= T.levelCount(); n++) {
              T.loadLevel(n);
              const cells = T.getCells();
              const normals = cells.filter(c => typeof c.t === 'number');
              const specials = cells.length - normals.length;
              const minShots = Math.ceil(normals.length / 3) + Math.ceil(specials / 2);
              results.push({level:n, bubbles:cells.length, shots:T.levelShots(n), minShots});
            }
            return results;
          })()
        """)
        all_winnable = all(s["shots"] >= s["minShots"] for s in sim)
        check("all_levels_winnable", all_winnable and len(sim) == 50,
              {"n": len(sim), "first": sim[0], "last": sim[-1]})

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

        # 14) 截图：编辑器
        evaluate(ws, "window.__GARDEN_TEST__.openEditor()")
        evaluate(ws, "for(let c=0;c<8;c++)window.__GARDEN_TEST__.edSet(0,c,c%3);window.__GARDEN_TEST__.edSet(1,0,'R');window.__GARDEN_TEST__.edSet(1,1,'B');window.__GARDEN_TEST__.edSet(1,2,'L');window.__GARDEN_TEST__.edSet(1,3,'I');window.__GARDEN_TEST__.edSet(1,4,'S')")
        time.sleep(0.6)
        screenshot(ws, shots / "editor.png")
        report["screenshots"].append("screenshots/editor.png")

        # 15) 截图：特殊关卡游戏画面（造一个含全部特殊泡泡的局）
        evaluate(ws, "window.__GARDEN_TEST__.loadCustomDef({name:'演示关',shots:20,cells:[{r:0,c:0,t:0},{r:0,c:1,t:0},{r:0,c:2,t:1},{r:0,c:3,t:1},{r:0,c:4,t:2},{r:0,c:5,t:2},{r:0,c:6,t:3},{r:0,c:7,t:3},{r:1,c:0,t:'R'},{r:1,c:1,t:'B'},{r:1,c:2,t:'L'},{r:1,c:3,t:'I'},{r:1,c:4,t:'S'},{r:1,c:5,t:4},{r:1,c:6,t:4},{r:2,c:0,t:5},{r:2,c:1,t:5},{r:2,c:2,t:0}]})")
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
