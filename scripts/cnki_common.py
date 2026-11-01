"""Shared helpers for cnki-browser: attach to the dedicated Chrome, exit codes, captcha handling."""
import contextlib, json, os, pathlib, re, subprocess, sys, time

STATE = pathlib.Path.home() / ".agents/state/cnki-browser"
OUT_DIR = STATE / "out"
PORT = int(os.environ.get("CNKI_CDP_PORT", "9334"))
ADV_URL = "https://kns.cnki.net/kns8s/AdvSearch"
PAUSE = tuple(float(x) for x in os.environ.get("CNKI_PAUSE", "2.0,4.5").split(","))

EXIT_OK, EXIT_ERR, EXIT_LOGIN, EXIT_BROWSER, EXIT_CAPTCHA, EXIT_LAYOUT = 0, 1, 3, 4, 5, 6

CAPTCHA_SEL = "#tCaptchaDyContent, .tencent-captcha-dy__content"


class SkillError(Exception):
    def __init__(self, msg, code=EXIT_ERR):
        super().__init__(msg)
        self.code = code


def run(fn):
    try:
        return fn()
    except SkillError as e:
        print(f"error  {e}", file=sys.stderr)
        print(f"exit   {e.code}", file=sys.stderr)
        return e.code


@contextlib.contextmanager
def attached():
    from playwright.sync_api import sync_playwright
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1.5)
    except Exception:
        raise SkillError(f"dedicated Chrome not running on :{PORT}; run cnki_chrome.py start", EXIT_BROWSER)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        ctx = browser.contexts[0]
        pg = ctx.new_page()
        try:
            yield ctx, pg
        finally:
            with contextlib.suppress(Exception):
                pg.close()


def captcha_visible(pg):
    return pg.evaluate("""(sel) => {
      const e = document.querySelector(sel); if (!e) return false;
      const r = e.getBoundingClientRect(); if (r.width < 20 || r.height < 20) return false;
      if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) return false;
      for (let x = e; x; x = x.parentElement) { const s = getComputedStyle(x);
        if (s.display === 'none' || s.visibility === 'hidden' || +s.opacity === 0) return false; }
      return true; }""", CAPTCHA_SEL) or bool(re.search(r"/verify/", pg.url))


def notify(msg):
    with contextlib.suppress(Exception):
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "CNKI 脚本" sound name "Glass"'])


def handle_captcha(pg, wait_s=300):
    """If a captcha is on screen: try the auto solver, else wait for the user to clear it."""
    if not captcha_visible(pg):
        return False
    try:
        import cnki_captcha
        for attempt in range(1):  # solve() 内部已做 5 次 OCR + 1 次视觉的阶梯
            solved = False
            for fn in ("solve", "solve_slider"):
                f = getattr(cnki_captcha, fn, None)
                if f and f(pg):
                    solved = True
                    break
            if solved:
                time.sleep(2)
                if not captcha_visible(pg):
                    print(f"captcha  auto-solved (attempt {attempt + 1})", file=sys.stderr)
                    return True
    except ImportError:
        pass
    except Exception as e:
        print(f"captcha  auto-solver error: {e}", file=sys.stderr)
    wait_s = int(os.environ.get("CNKI_CAPTCHA_WAIT", "0") or 0)
    if wait_s <= 0:
        # 默认不干等：这一篇跳过，调用方继续下一篇，回头补
        print("captcha  自动过不了，跳过这一篇，稍后重试", file=sys.stderr)
        raise SkillError("captcha skipped", EXIT_CAPTCHA)
    notify("知网弹出验证码，请在专用 Chrome 窗口里完成")
    print("captcha  waiting for the user to solve it in the CNKI Chrome window ...", file=sys.stderr)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(2)
        if not captcha_visible(pg):
            print("captcha  cleared", file=sys.stderr)
            return True
    raise SkillError("captcha not solved in time", EXIT_CAPTCHA)


def default_out(query):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w一-鿿]+", "_", query)[:40]
    return OUT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.jsonl"


def digest(meta, lines=()):
    for k, v in meta.items():
        print(f"{k:<9}{v}")
    for l in lines:
        print("  " + l)
