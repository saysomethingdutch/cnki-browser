"""Auto-solvers for CNKI captchas: click-word ("请依次点击【呢,场,小】") and the slider.

Everything runs locally (ddddocr + OpenCV); no external model is called.

Click-word: ddddocr detects character boxes; for each required character the OCR model
scores how likely it is to be in each box (over several rotations), and the best joint
assignment is clicked in order. When even the best assignment is unlikely, the solver
refreshes for a new image instead of guessing.

CNKI often gives no verdict on the first correct answer and serves a second captcha;
that is normal, so the solver keeps going until it passes or the attempt budget runs out.

Both solvers return False when they cannot decide, so the caller falls back.
"""
import base64, io, os, random, re, sys, time

_det = _ocr = None

FIND_JS = r"""() => {
  const imgs = [...document.querySelectorAll('img')].filter(i => (i.src||'').startsWith('data:image') );
  const vis = imgs.filter(i => { const r = i.getBoundingClientRect(); return r.width > 150 && r.height > 60 && r.top > -1000; });
  const msg = [...document.querySelectorAll('.verify-msg, .verify-bar-area span, [class*=verify] span')]
               .map(e => e.innerText.trim()).find(t => /依次点击/.test(t)) || '';
  if (!vis.length) return null;
  const im = vis[0], r = im.getBoundingClientRect();
  return {src: im.src, x: r.left, y: r.top, w: r.width, h: r.height, nw: im.naturalWidth, nh: im.naturalHeight, msg};
}"""


def _models():
    global _det, _ocr
    if _det is None:
        import ddddocr
        _det = ddddocr.DdddOcr(det=True, show_ad=False)
        _ocr = ddddocr.DdddOcr(show_ad=False)
    return _det, _ocr


def _char_probs(img):
    """Per-step probability matrix (steps x charset) from ddddocr for one crop."""
    import numpy as np
    r = _ocr.classification(img, probability=True)
    return np.array([row for step in r["probabilities"] for row in step], dtype=float)


def _crop_variants(crop):
    """The captcha rotates each character up to about ±40°, so score a few rotations too."""
    out = [crop]
    for ang in (-40, -25, -12, 12, 25, 40):
        out.append(crop.rotate(ang, expand=True, fillcolor=(255, 255, 255)))
    return out


MIN_CHAR_SCORE = float(os.environ.get("CNKI_MIN_CHAR_SCORE", "0.05"))


def plan_clicks(png, chars):
    """Assign each required character to a detected box, then return click points in order.

    Instead of reading every box freely (which ddddocr often gets wrong on rotated,
    coloured characters), ask the classifier how likely *each required character* is in
    each box, over several rotations, and pick the assignment with the best joint score.
    Returns None when even the best assignment is too unlikely, so the caller refreshes.
    """
    import itertools
    import numpy as np
    from PIL import Image
    det, ocr = _models()
    boxes = det.detection(png)
    if len(boxes) < len(chars):
        return None
    charset = ocr.get_charset()
    index = {c: i for i, c in enumerate(charset)}
    if any(c not in index for c in chars):
        return None
    im = Image.open(io.BytesIO(png)).convert("RGB")
    S = np.zeros((len(chars), len(boxes)))
    for j, (x1, y1, x2, y2) in enumerate(boxes):
        crop = im.crop((max(0, x1 - 3), max(0, y1 - 3), x2 + 3, y2 + 3))
        mats = [_char_probs(v) for v in _crop_variants(crop)]
        for i, ch in enumerate(chars):
            S[i, j] = max(float(m[:, index[ch]].max()) for m in mats)
    best = max(itertools.permutations(range(len(boxes)), len(chars)),
               key=lambda p: sum(np.log(S[i, p[i]] + 1e-9) for i in range(len(chars))))
    scores = [S[i, best[i]] for i in range(len(chars))]
    print("captcha  char scores " + " ".join(f"{c}={s:.2f}" for c, s in zip(chars, scores)), file=sys.stderr)
    if min(scores) < MIN_CHAR_SCORE:
        return None
    pts = []
    for i in range(len(chars)):
        x1, y1, x2, y2 = boxes[best[i]]
        pts.append(((x1 + x2) / 2 + random.uniform(-3, 3), (y1 + y2) / 2 + random.uniform(-3, 3)))
    return pts


def _verify_page(pg):
    """验证码可能开在另一个 /verify/home 标签页里，先把页面找准。"""
    try:
        info = pg.evaluate(FIND_JS)
    except Exception:
        info = None
    if info and info.get("msg"):
        return pg, info
    try:
        pages = pg.context.pages
    except Exception:
        return pg, None
    for p in pages:
        try:
            if p is pg or not re.search(r"/verify/", p.url or ""):
                continue
            i2 = p.evaluate(FIND_JS)
            if i2 and i2.get("msg"):
                print("captcha  在另一个标签页里找到验证码", file=sys.stderr)
                return p, i2
        except Exception:
            continue
    return pg, None


MAX_CLICK_ATTEMPTS = int(os.environ.get("CNKI_CLICK_ATTEMPTS", "8"))


def _still_there(page):
    try:
        if page.is_closed():
            return False
        return "/verify/" in (page.url or "")
    except Exception:
        return False


def _click_verdict(page, timeout=4.0):
    """Read the widget's own result in .verify-msg: 验证成功 / 验证失败.
    'none' means CNKI answered nothing (common on the first correct try);
    'gone' means the page navigated away, which is what a pass looks like."""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.2)
        if not _still_there(page):
            return "gone"
        try:
            msg = page.evaluate("() => [...document.querySelectorAll('.verify-msg')].map(e => e.innerText).join('|')")
        except Exception:
            return "gone"
        if "成功" in msg:
            return "pass"
        if "失败" in msg:
            return "fail"
    return "none"


def _refresh(page):
    try:
        page.locator(".verify-refresh").first.click(timeout=3000)
    except Exception:
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception:
            pass
    time.sleep(random.uniform(1.2, 2.0))


def solve(pg):
    """Click-word solver. Keeps trying until the verify page goes away or the budget runs out.

    - low-confidence image  -> refresh for a new one (no click, no guess)
    - 'none' (no verdict)    -> normal for CNKI; the widget usually reloads a fresh
                                captcha, so just try again
    - 'fail'                 -> wait for the widget's own refresh, try again
    """
    for i in range(MAX_CLICK_ATTEMPTS):
        page, info = _verify_page(pg)
        if not info or not info.get("msg"):
            return bool(i) and not _still_there(page)
        m = re.search(r"【(.+?)】", info["msg"])
        if not m:
            return False
        chars = [c.strip() for c in re.split(r"[,，、]", m.group(1)) if c.strip()]
        png = base64.b64decode(info["src"].split(",", 1)[1])
        pts = plan_clicks(png, chars)
        if not pts:
            print(f"captcha  第 {i + 1} 次：识别把握不够，换一张", file=sys.stderr)
            _refresh(page)
            continue
        sx, sy = info["w"] / info["nw"], info["h"] / info["nh"]
        for x, y in pts:
            page.mouse.move(info["x"] + x * sx, info["y"] + y * sy, steps=random.randint(8, 16))
            time.sleep(random.uniform(0.25, 0.6))
            page.mouse.click(info["x"] + x * sx, info["y"] + y * sy)
        verdict = _click_verdict(page)
        if verdict in ("pass", "gone"):
            print(f"captcha  通过（第 {i + 1} 次）", file=sys.stderr)
            time.sleep(1.2)
            return True
        print(f"captcha  第 {i + 1} 次：{ {'fail': '验证失败', 'none': '知网未给结果'}.get(verdict, verdict) }，再来", file=sys.stderr)
        time.sleep(random.uniform(1.0, 1.6))
    return False


# ---------------------------------------------------------------------------
# Slider captchas — one adaptive solver for every "drag the piece into the gap" variant
#
# CNKI shows at least two slider widgets with unrelated DOM:
#   kns.cnki.net/verify/home?captchaType=blockPuzzle  (.verify-img-panel / .verify-sub-block / .verify-move-block)
#   bar.cnki.net/bar/verify/... on PDF download       (Tencent: .tencent-captcha-dy__verify-bg-img / __fg-item / __slider-block)
# Rather than one hand-written solver per widget, this finds the parts by structure:
#   background = the largest visible image-bearing element (>= 200 px wide)
#   piece      = an image-bearing element inside the background, 8–45 % of its width, at the left
#   handle     = a small pointer-cursor element below the background, inside its horizontal span
# and then:
#   1. shape    — screenshot the background with and without the piece; the difference is the
#                 piece's exact silhouette (no class names, no sprite parsing)
#   2. gap      — slide that silhouette along its own rows of the piece-hidden background and score
#                 "ring brighter than inside" (the gap is a shaded copy of the piece); confidence =
#                 best score / best score elsewhere, so decoy outlines and busy photos are rejected
#   3. drag     — closed loop: move the handle in small steps while reading where the piece actually
#                 is, and release when it sits on the gap. No assumption about the handle/piece ratio,
#                 page zoom or device pixel ratio.
#   4. verdict  — the page leaves /verify/, or shows 成功 → pass; 失败/错误 or the piece snapping back → fail
# ---------------------------------------------------------------------------

SLIDER_FIND_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && r.top > -500 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const hasImg = e => e.tagName === 'IMG' || e.tagName === 'CANVAS'
    || (getComputedStyle(e).backgroundImage || 'none') !== 'none' || !!e.querySelector('img,canvas');
  const all = [...document.querySelectorAll('body *')].filter(vis);
  const imgs = all.filter(e => (e.tagName === 'IMG' || e.tagName === 'CANVAS'
    || (getComputedStyle(e).backgroundImage || 'none') !== 'none'));
  const bgs = imgs.map(e => [e, e.getBoundingClientRect()])
    .filter(([e, r]) => r.width >= 200 && r.height >= 80 && r.width <= innerWidth * 0.8 && r.height <= innerHeight * 0.8)
    .sort((a, b) => b[1].width * b[1].height - a[1].width * a[1].height);
  const tag = (e) => { e.dataset.cnkiSlider = e.dataset.cnkiSlider || Math.random().toString(36).slice(2); return e.dataset.cnkiSlider; };
  const R = r => ({x: r.left, y: r.top, w: r.width, h: r.height});
  // try background candidates from largest down; keep the first that has a piece and a handle
  for (const [bg, rb] of bgs) {
    const pieces = all.filter(e => e !== bg && !e.contains(bg) && hasImg(e)).map(e => [e, e.getBoundingClientRect()])
      .filter(([e, r]) => r.width >= rb.width * 0.08 && r.width <= rb.width * 0.45 && r.height <= rb.height * 1.05
        && r.left >= rb.left - 5 && r.left < rb.left + rb.width * 0.35
        && r.top < rb.bottom && r.bottom > rb.top)
      .sort((a, b) => a[1].left - b[1].left);
    const handles = all.map(e => [e, e.getBoundingClientRect()])
      .filter(([e, r]) => getComputedStyle(e).cursor === 'pointer' && r.width >= 24 && r.width <= 140 && r.height >= 20 && r.height <= 90
        && r.top >= rb.top + rb.height * 0.5 && r.top <= rb.bottom + 120 && r.left >= rb.left - 5 && r.left < rb.left + rb.width * 0.4
        && !(r.top < rb.bottom - 5 && r.bottom > rb.top + 5))
      .sort((a, b) => (a[1].top - b[1].top) || (a[1].left - b[1].left));
    if (pieces.length && handles.length)
      return {bg: tag(bg), piece: tag(pieces[0][0]), handle: tag(handles[0][0]),
              rb: R(rb), rp: R(pieces[0][1]), rh: R(handles[0][1])};
  }
  return null;
}"""

SLIDER_MIN_CONF = float(os.environ.get("CNKI_SLIDER_MIN_CONF", "2.7"))  # single-cue threshold; agreeing cues always drag


def slider_target(full_png, back_png, rp, rb):
    """Where the piece's left edge must go (CSS px, relative to the background), and confidence.

    full_png / back_png: screenshots of the background element with the piece shown / hidden.
    """
    import numpy as np, cv2
    F = cv2.imdecode(np.frombuffer(full_png, np.uint8), cv2.IMREAD_COLOR)
    B = cv2.imdecode(np.frombuffer(back_png, np.uint8), cv2.IMREAD_COLOR)
    if F is None or B is None or F.shape != B.shape:
        return None, 0.0
    s = B.shape[1] / rb["w"]
    x0 = max(0, int((rp["x"] - rb["x"]) * s)); y0 = max(0, int((rp["y"] - rb["y"]) * s))
    w = min(int(rp["w"] * s), B.shape[1] - x0); h = min(int(rp["h"] * s), B.shape[0] - y0)
    if w < 8 or h < 8:
        return None, 0.0
    d = np.abs(F[y0:y0 + h, x0:x0 + w].astype(int) - B[y0:y0 + h, x0:x0 + w].astype(int)).sum(axis=2)
    mask = cv2.morphologyEx((d > 40).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    ys, xs = np.where(mask)
    if len(ys) < 50:
        return None, 0.0
    mx0, mx1, my0, my1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    m = mask[my0:my1, mx0:mx1]
    ring = cv2.dilate(m, np.ones((9, 9), np.uint8)) - m
    inside = m.astype(np.float32) / m.sum(); outside = ring.astype(np.float32) / max(ring.sum(), 1)
    band = cv2.cvtColor(B, cv2.COLOR_BGR2GRAY).astype(np.float32)[y0 + my0:y0 + my1]
    if band.shape[0] < m.shape[0]:
        return None, 0.0
    W = band.shape[1] - m.shape[1]
    start = x0 + mx0 + m.shape[1]
    if W <= start + 2:
        return None, 0.0

    def confidence(score):
        score = score.copy(); score[:start] = -1
        gx = int(np.argmax(score)); best = float(score[gx])
        far = np.abs(np.arange(len(score)) - gx) > m.shape[1] * 0.6
        far[:start] = False
        second = float(score[far].max()) if far.any() else 0.0
        return gx, best / max(second, 1e-6)

    # cue 1: shading — the gap is a darker/lighter copy of the piece (strong on the Tencent download gate)
    a = cv2.filter2D(band, -1, inside, anchor=(0, 0), borderType=cv2.BORDER_CONSTANT)
    b = cv2.filter2D(band, -1, outside, anchor=(0, 0), borderType=cv2.BORDER_CONSTANT)
    shade = np.abs(b - a)[0, :W]
    gx1, c1 = confidence(shade)
    # cue 2: outline — the gap's edges have the piece's shape (strong on kns, where decoys are outlined too)
    outline = cv2.morphologyEx(m * 255, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    edges = cv2.Canny(band.astype(np.uint8), 50, 150)
    res = cv2.matchTemplate(edges, outline, cv2.TM_CCORR_NORMED)
    edge = res.max(axis=0)[:W] if res.shape[1] >= W else np.pad(res.max(axis=0), (0, W - res.shape[1]))
    gx2, c2 = confidence(edge)
    if abs(gx1 - gx2) <= 3 * s and max(c1, c2) >= 1.5:
        # both cues agree: on 2026-11-02 every agreeing case passed, so always trust it
        gx, conf = (gx1 + gx2) // 2, max(SLIDER_MIN_CONF, max(c1, c2) * 1.5)
        cue = "both"
    elif c1 >= c2:
        gx, conf, cue = gx1, c1, "shade"
    else:
        gx, conf, cue = gx2, c2, "edge"
    print(f"captcha  slider cues shade={gx1 / s:.0f}@{c1:.1f} edge={gx2 / s:.0f}@{c2:.1f} -> {cue}", file=sys.stderr)
    return (gx - mx0) / s, conf


def _slider_page(pg):
    pages = [pg]
    try:
        pages += [p for p in pg.context.pages if p is not pg]
    except Exception:
        pass
    for p in pages:
        try:
            if p.is_closed() or "/verify/" not in (p.url or ""):
                continue
            info = p.evaluate(SLIDER_FIND_JS)
            if info:
                return p, info
        except Exception:
            continue
    return pg, None


def _sel(token):
    return f'[data-cnki-slider="{token}"]'


def _slider_verdict(page, piece_sel, start_x, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.25)
        try:
            if page.is_closed() or "/verify/" not in (page.url or ""):
                return "pass"
            txt = page.inner_text("body")
        except Exception:
            return "pass"
        if "成功" in txt:
            return "pass"
        if re.search(r"失败|错误|再试", txt):
            return "fail"
        try:
            box = page.locator(piece_sel).first.bounding_box()
            if box and abs(box["x"] - start_x) < 2:
                return "fail"
        except Exception:
            pass
    return "none"


def solve_slider(pg, attempts=int(os.environ.get("CNKI_SLIDER_ATTEMPTS", "6"))):
    """Adaptive slider solver; see the section comment. Returns True when the captcha passes."""
    for i in range(attempts):
        page, info = _slider_page(pg)
        if not info:
            return False
        bg, piece, handle = page.locator(_sel(info["bg"])).first, page.locator(_sel(info["piece"])).first, page.locator(_sel(info["handle"])).first
        rb, rp, rh = info["rb"], info["rp"], info["rh"]
        try:
            full = bg.screenshot(timeout=4000)
            page.evaluate("(s) => { const e = document.querySelector(s); e.dataset.cnkiVis = e.style.visibility; e.style.visibility = 'hidden'; }", _sel(info["piece"]))
            back = bg.screenshot(timeout=4000)
        finally:
            try:
                page.evaluate("(s) => { const e = document.querySelector(s); e.style.visibility = e.dataset.cnkiVis || ''; }", _sel(info["piece"]))
            except Exception:
                pass
        target, conf = slider_target(full, back, rp, rb)
        print(f"captcha  slider target={target if target is None else round(target, 1)} conf={conf:.1f}", file=sys.stderr)
        if target is None or conf < SLIDER_MIN_CONF:
            _refresh_slider(page)
            continue
        sx, sy = rh["x"] + rh["w"] / 2, rh["y"] + rh["h"] / 2
        page.mouse.move(sx, sy, steps=random.randint(5, 9))
        time.sleep(random.uniform(0.15, 0.3))
        page.mouse.down()
        time.sleep(random.uniform(0.08, 0.15))
        hand, moved, last = 0.0, False, None
        for _ in range(120):
            box = piece.bounding_box()
            if not box:
                break
            pos = box["x"] - rb["x"]
            if last is not None and abs(pos - last) > 0.3:
                moved = True
            last = pos
            err = target - pos
            if abs(err) < 0.8:
                break
            if hand > 25 and not moved:
                break
            step = max(0.6, min(abs(err) * 0.45, 14)) * (1 if err > 0 else -1)
            hand += step
            page.mouse.move(sx + hand, sy + random.uniform(-0.8, 0.8))
            time.sleep(random.uniform(0.01, 0.025))
        time.sleep(random.uniform(0.15, 0.3))
        page.mouse.up()
        if not moved:
            print("captcha  slider: piece did not follow the handle; wrong parts picked", file=sys.stderr)
            return False
        verdict = _slider_verdict(page, _sel(info["piece"]), rp["x"])
        if verdict == "pass":
            print(f"captcha  slider passed (attempt {i + 1})", file=sys.stderr)
            time.sleep(1.2)
            return True
        print(f"captcha  slider attempt {i + 1}: {verdict}", file=sys.stderr)
        time.sleep(random.uniform(0.8, 1.2))
        # some widgets keep the same image after a failure; ask for a new one
        _refresh_slider(page)
    return False


def _refresh_slider(page):
    for sel in (".verify-refresh", "[class*=refresh]", "[class*=reload]", "[aria-label*=刷新]", "[title*=刷新]"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=2000)
                time.sleep(random.uniform(1.0, 1.6))
                return
        except Exception:
            continue
    try:
        page.reload(wait_until="domcontentloaded")
    except Exception:
        pass
    time.sleep(random.uniform(1.2, 2.0))
