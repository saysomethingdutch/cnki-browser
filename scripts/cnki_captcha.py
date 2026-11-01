"""Auto-solver for CNKI captchas: click-word ("请依次点击【呢,场,小】") and Tencent slider.

Pipeline:
  1. Find the captcha image (data:image/png;base64 <img> inside the verify widget)
     and the required characters from its prompt text.
  2. ddddocr detection gives character boxes; ddddocr OCR reads each box.
  3. Assign required characters to boxes: exact OCR matches first; leftovers go to
     the vision model (GLM via the relay) which returns the box index for each char.
  4. Click the box centres in order with small human-like delays.

Both solvers return False when they cannot decide, so the caller falls back to the human.
"""
import base64, io, json, os, random, re, sys, time, urllib.request

_det = _ocr = None
RELAY = os.environ.get("CNKI_VISION_URL", "http://127.0.0.1:15721/v1/responses")
VISION_MODEL = os.environ.get("CNKI_VISION_MODEL", "ccs-modelbest-codex-self-bui/DEEPSEEK_xsicpn")

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


def _vision_assign(png, boxes, chars):
    """Ask the vision model which numbered box holds each required character."""
    from PIL import Image, ImageDraw
    im = Image.open(io.BytesIO(png)).convert("RGB")
    from PIL import ImageFont
    im = im.resize((im.width * 2, im.height * 2))
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 28)
    except Exception:
        font = None
    for i, (x1, y1, x2, y2) in enumerate(boxes):
        x1, y1, x2, y2 = (v * 2 for v in (x1, y1, x2, y2))
        d.rectangle((x1, y1, x2, y2), outline=(255, 0, 0), width=3)
        d.rectangle((x1, y1 - 30, x1 + 26, y1), fill=(255, 0, 0))
        d.text((x1 + 5, y1 - 31), str(i), fill=(255, 255, 255), font=font)
    buf = io.BytesIO(); im.save(buf, "PNG")
    prompt = ("图中每个红框上方的红底白字是它的编号（0 起）。只有部分红框里是要找的字，其余可能是干扰字或图标。请找出下列汉字分别在哪个编号的红框里，"
              f"汉字可能被旋转或变形：{'、'.join(chars)}。只输出 JSON，例如 {{\"呢\":1,\"场\":0}}，不要其他文字。")
    body = {"model": VISION_MODEL, "max_output_tokens": 4000, "reasoning": {"effort": "low"}, "stream": False,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}]}]}
    req = urllib.request.Request(RELAY, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer PROXY_MANAGED"})
    d = json.load(urllib.request.urlopen(req, timeout=90))
    txt = "".join(c.get("text", "") for o in d.get("output", []) if o.get("type") == "message" for c in o.get("content", []))
    m = re.search(r"\{.*\}", txt, re.S)
    return {k: int(v) for k, v in json.loads(m.group(0)).items()} if m else {}


def plan_clicks(png, chars, decoy_vision=False):
    det, ocr = _models()
    boxes = det.detection(png)
    if len(boxes) < len(chars):
        return None
    from PIL import Image
    im = Image.open(io.BytesIO(png))
    reads = []
    for b in boxes:
        c = im.crop(tuple(b)); buf = io.BytesIO(); c.save(buf, "PNG")
        reads.append(ocr.classification(buf.getvalue()).strip())
    assign, used = {}, set()
    for ch in chars:
        idx = next((i for i, r in enumerate(reads) if r == ch and i not in used), None)
        if idx is not None:
            assign[ch] = idx; used.add(idx)
    missing = [c for c in chars if c not in assign]
    need_vision = bool(missing) or (decoy_vision and len(boxes) > len(chars))
    if need_vision:
        v = {}
        for _ in range(3):
            try:
                v = _vision_assign(png, boxes, chars)
                if all(c in v for c in missing):
                    break
            except Exception:
                time.sleep(3)
        if decoy_vision and len(boxes) > len(chars) and all(c in v for c in chars):
            idxs = [v[c] for c in chars]
            if len(set(idxs)) == len(idxs) and all(0 <= i < len(boxes) for i in idxs):
                assign = dict(v)
                missing = []
        for ch in missing:
            idx = v.get(ch)
            if idx is None or idx in used or not (0 <= idx < len(boxes)):
                return None
            assign[ch] = idx; used.add(idx)
    pts = []
    for ch in chars:
        x1, y1, x2, y2 = boxes[assign[ch]]
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


_FAIL_STREAK = 0
MAX_OCR_ATTEMPTS = 5


def _still_there(page):
    try:
        if page.is_closed():
            return False
        return "/verify/" in (page.url or "")
    except Exception:
        return False


def solve(pg):
    """连续 OCR 尝试；连着 MAX_OCR_ATTEMPTS 次过不了，下一次才用视觉模型兜底。"""
    global _FAIL_STREAK
    for i in range(MAX_OCR_ATTEMPTS + 1):
        page, info = _verify_page(pg)
        if not info or not info.get("msg"):
            return True if i else False
        m = re.search(r"【(.+?)】", info["msg"])
        if not m:
            return False
        chars = [c.strip() for c in re.split(r"[,，、]", m.group(1)) if c.strip()]
        png = base64.b64decode(info["src"].split(",", 1)[1])
        use_vision = _FAIL_STREAK >= MAX_OCR_ATTEMPTS
        if use_vision:
            # 用掉一次视觉兜底后归零，下一张回到 OCR 阶梯，避免从此每张都走慢路径
            _FAIL_STREAK = 0
        pts = plan_clicks(png, chars, decoy_vision=use_vision)
        if pts:
            sx, sy = info["w"] / info["nw"], info["h"] / info["nh"]
            for x, y in pts:
                page.mouse.move(info["x"] + x * sx, info["y"] + y * sy, steps=random.randint(8, 16))
                time.sleep(random.uniform(0.25, 0.6))
                page.mouse.click(info["x"] + x * sx, info["y"] + y * sy)
            time.sleep(1.6)
            if not _still_there(page):
                _FAIL_STREAK = 0
                print("captcha  通过（连续失败 %d 次后）" % i if i else "captcha  通过", file=sys.stderr)
                return True
            print(f"captcha  第 {i + 1} 次未通过，换图重试（累计失败 {_FAIL_STREAK + 1}）", file=sys.stderr)
        _FAIL_STREAK += 1
        time.sleep(random.uniform(0.5, 1.0))
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
            return bool(i)
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
