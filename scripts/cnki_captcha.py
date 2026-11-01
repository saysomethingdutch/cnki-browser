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
# Slider ("向右滑动完成验证", captchaType=blockPuzzle)
#
# CNKI's own VerifySlide component (verify/js/342.*.js) works like this:
#   - background <img> 310x155 and a piece <img> 47x155 (PNG with alpha) inside
#     .verify-sub-block, which moves with the handle .verify-move-block;
#   - mousedown on the handle, mousemove on window, mouseup on window;
#   - on mouseup it sends x = 310 * moveBlockLeft / imgWidth to /verify-api/web/check.
# So the piece's x on the background equals the handle's travel in page pixels,
# scaled by imgWidth/310.
# ---------------------------------------------------------------------------

SLIDER_JS = r"""() => {
  const bg = [...document.querySelectorAll('.verify-img-panel img')].find(i => (i.src||'').startsWith('data:image'));
  const pc = document.querySelector('.verify-sub-block img');
  const h = document.querySelector('.verify-move-block');
  if (!bg || !pc || !h) return null;
  const rb = bg.getBoundingClientRect(), rh = h.getBoundingClientRect();
  if (rb.width < 100 || rh.width < 10 || rb.top < -1000) return null;
  return {bg: bg.src, piece: pc.src, bgx: rb.left, bgw: rb.width, nw: bg.naturalWidth,
          hx: rh.left + rh.width / 2, hy: rh.top + rh.height / 2};
}"""

SLIDER_MIN_SCORE = 0.28


def slider_offset(bg_png, piece_png):
    """Return (x, score): where the piece's left edge belongs on the background, in natural pixels."""
    import numpy as np, cv2
    bg = cv2.imdecode(np.frombuffer(bg_png, np.uint8), cv2.IMREAD_COLOR)
    pc = cv2.imdecode(np.frombuffer(piece_png, np.uint8), cv2.IMREAD_UNCHANGED)
    if bg is None or pc is None or pc.ndim != 3 or pc.shape[2] != 4:
        return None, 0.0
    alpha = pc[:, :, 3]
    ys, xs = np.where(alpha > 0)
    if not len(ys):
        return None, 0.0
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    mask = (alpha[y0:y1, x0:x1] > 0).astype(np.uint8) * 255
    outline = cv2.morphologyEx(mask, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    band = bg[max(0, y0 - 3):y1 + 3]
    edges = cv2.Canny(cv2.cvtColor(band, cv2.COLOR_BGR2GRAY), 60, 180)
    if edges.shape[0] < outline.shape[0] or edges.shape[1] < outline.shape[1]:
        return None, 0.0
    res = cv2.matchTemplate(edges, outline, cv2.TM_CCORR_NORMED)
    # the piece starts at x=0 and always overlaps its own spot there; skip the first piece-width
    res[:, : max(1, x1 - x0)] = 0
    _, score, _, loc = cv2.minMaxLoc(res)
    return int(loc[0] - x0), float(score)


def _drag_path(dx):
    """Ease-out path with a small overshoot and settle, like a hand."""
    pts, n = [], random.randint(22, 32)
    over = dx + random.uniform(2, 5)
    for i in range(1, n + 1):
        f = 1 - (1 - i / n) ** 3
        pts.append((over * f, random.uniform(-1.2, 1.2)))
    for k in range(random.randint(3, 5)):
        pts.append((over - (over - dx) * (k + 1) / 4, random.uniform(-0.6, 0.6)))
    pts.append((dx, 0))
    return pts


def _slider_page(pg):
    try:
        info = pg.evaluate(SLIDER_JS)
    except Exception:
        info = None
    if info:
        return pg, info
    try:
        pages = pg.context.pages
    except Exception:
        return pg, None
    for p in pages:
        try:
            if p is pg or "/verify/" not in (p.url or ""):
                continue
            i2 = p.evaluate(SLIDER_JS)
            if i2:
                return p, i2
        except Exception:
            continue
    return pg, None


def solve_slider(pg, attempts=4):
    """Solve CNKI's blockPuzzle slider. Returns True when the verify page goes away."""
    for i in range(attempts):
        page, info = _slider_page(pg)
        if not info:
            return False
        bg = base64.b64decode(info["bg"].split(",", 1)[1])
        pc = base64.b64decode(info["piece"].split(",", 1)[1])
        x, score = slider_offset(bg, pc)
        print(f"captcha  slider match x={x} score={score:.3f}", file=sys.stderr)
        if x is None or score < SLIDER_MIN_SCORE:
            return False
        dx = x * info["bgw"] / info["nw"]
        page.mouse.move(info["hx"], info["hy"], steps=random.randint(6, 12))
        time.sleep(random.uniform(0.2, 0.45))
        page.mouse.down()
        for px, py in _drag_path(dx):
            page.mouse.move(info["hx"] + px, info["hy"] + py)
            time.sleep(random.uniform(0.012, 0.03))
        time.sleep(random.uniform(0.15, 0.35))
        page.mouse.up()
        verdict = _slider_verdict(page)
        if verdict == "pass":
            print("captcha  slider passed", file=sys.stderr)
            time.sleep(1.5)
            return True
        print(f"captcha  slider attempt {i + 1}: {verdict}, new image", file=sys.stderr)
        time.sleep(random.uniform(1.2, 1.8))
    return False


def _slider_verdict(page, timeout=4.0):
    """Read the widget's own result: icon-check / 验证成功 = pass, icon-close / 验证失败 = fail.
    The page navigates away ~1 s after a pass, so a vanished widget also counts as a pass."""
    js = """() => { const i = document.querySelector('.verify-move-block i');
                     const t = document.querySelector('.verify-tips');
                     return {icon: i ? i.className : '', tip: t ? t.innerText : '',
                             here: !!document.querySelector('.verify-move-block')}; }"""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.2)
        try:
            s = page.evaluate(js)
        except Exception:
            return "pass"
        if "icon-check" in s["icon"] or "成功" in s["tip"]:
            return "pass"
        if "icon-close" in s["icon"] or "失败" in s["tip"]:
            return "fail"
        if not s["here"]:
            return "pass"
    return "no-verdict"
