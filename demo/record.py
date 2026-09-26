#!/usr/bin/env python3
"""Record the hackathon demo videos with Playwright (against the local API on quad).

  uv run --with playwright==1.56.0 python demo/record.py [--only 2,3] [--no-combine]

Writes demo/out/NN_<scene>.mp4 and demo/out/floodroutes_demo.mp4 (all scenes + title/end cards).
Live demo reports go to quad's local database and are cleared before and after.
Needs: the local API (:8086) and GraphHopper (:8989) running, ffmpeg, PLAYWRIGHT_BROWSERS_PATH.
"""
import argparse
import html
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo" / "out"
BASE = os.getenv("DEMO_BASE", "http://127.0.0.1:8086/")
W, H = 1280, 720

TITLE = "Dynamic Flood Routing in Houston, Texas"
SUBTITLE = "Driving directions that avoid streets with a history of flooding, and update from live reports"
AUTHOR = "A. Lester Buck III"
SITE = "floodroutes.nbolt.com"

TRIPS = {
    "heights_second": ((29.797687, -95.398446, "Houston Heights"), (29.748083, -95.326831, "Second Ward")),
    "second_montrose": ((29.748083, -95.326831, "Second Ward"), (29.744698, -95.391275, "Montrose")),
    "clearlake": ((29.581087, -95.098607, "University of Houston - Clear Lake"), (29.542805, -95.148206, "Baybrook Mall")),
    "braeswood_rice": ((29.681555, -95.460975, "4800 N Braeswood Blvd, 77096"), (29.717700, -95.401700, "Rice University")),
}


def trip_url(key, extra=""):
    (alat, alon, alab), (blat, blon, blab) = TRIPS[key]
    return (f"{BASE}?from={alat:.6f},{alon:.6f}&fromLabel={quote(alab)}"
            f"&to={blat:.6f},{blon:.6f}&toLabel={quote(blab)}{extra}")


# ---- page helpers: captions and a visible pointer (Playwright videos show no cursor) ----
OVERLAY_JS = """
(() => {
  if (document.getElementById('demo-cap')) return;
  const st = document.createElement('style');
  st.textContent = `
    #demo-cap { position: fixed; left: calc((100% - 370px) / 2); bottom: 26px; transform: translateX(-50%); z-index: 99999;
      width: max-content; max-width: calc(100% - 420px); padding: 12px 20px; border-radius: 12px; background: rgba(17,24,39,.88); color: #fff;
      font: 600 21px/1.35 Inter, system-ui, sans-serif; text-align: center; box-shadow: 0 6px 24px rgba(0,0,0,.35);
      transition: opacity .35s; opacity: 0; pointer-events: none; }
    #demo-ptr { position: fixed; z-index: 100000; width: 22px; height: 22px; margin: -11px 0 0 -11px; border-radius: 50%;
      background: rgba(250,204,21,.9); border: 3px solid #111; box-shadow: 0 2px 8px rgba(0,0,0,.4);
      pointer-events: none; transition: left .45s ease, top .45s ease, transform .15s; left: 50%; top: 50%; }
    #demo-ptr.down { transform: scale(.7); }
    body.driving #demo-cap { left: 50%; max-width: 860px; }
    @media (max-width: 700px) {
      #demo-cap { left: 50%; bottom: 120px; max-width: 350px; font-size: 16px; padding: 10px 14px; }
    }`;
  document.head.appendChild(st);
  const c = document.createElement('div'); c.id = 'demo-cap'; document.body.appendChild(c);
  const p = document.createElement('div'); p.id = 'demo-ptr'; document.body.appendChild(p);
})();
"""


class Scene:
    def __init__(self, page):
        self.p = page

    def overlay(self):
        self.p.evaluate(OVERLAY_JS)

    def cap(self, text, secs=3.5):
        self.overlay()
        self.p.evaluate("t => { const c = document.getElementById('demo-cap'); c.innerHTML = t; c.style.opacity = t ? 1 : 0; }", text)
        if secs:
            self.p.wait_for_timeout(int(secs * 1000))

    def point(self, x, y, pause=0.5):
        self.overlay()
        self.p.evaluate("([x, y]) => { const e = document.getElementById('demo-ptr'); e.style.left = x + 'px'; e.style.top = y + 'px'; }", [x, y])
        self.p.mouse.move(x, y)
        self.p.wait_for_timeout(int(pause * 1000))

    def click(self, selector, pause=0.6):
        box = self.p.locator(selector).first.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.point(x, y, 0.55)
        self.p.evaluate("() => document.getElementById('demo-ptr').classList.add('down')")
        self.p.locator(selector).first.click()
        self.p.wait_for_timeout(150)
        self.p.evaluate("() => document.getElementById('demo-ptr').classList.remove('down')")
        self.p.wait_for_timeout(int(pause * 1000))

    def type_pick(self, selector, text, pick_contains=None):
        self.click(selector, 0.3)
        self.p.locator(selector).fill("")
        self.p.locator(selector).type(text, delay=85)
        self.p.wait_for_selector("#suggest:not([hidden]) li[data-i]")
        self.p.wait_for_timeout(700)
        item = self.p.locator("#suggest li[data-i]", has_text=pick_contains or text).first
        box = item.bounding_box()
        self.point(box["x"] + 60, box["y"] + box["height"] / 2, 0.5)
        item.dispatch_event("mousedown")
        self.p.wait_for_timeout(600)

    def map_point(self, lat, lon):
        return self.p.evaluate("([a, b]) => { const pt = map.latLngToContainerPoint([a, b]); const r = document.getElementById('map').getBoundingClientRect(); return [pt.x + r.left, pt.y + r.top]; }", [lat, lon])

    def settle(self, secs=2.0):
        """Let the map tiles finish loading."""
        try:
            self.p.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        self.p.wait_for_timeout(int(secs * 1000))

    def wait_route(self):
        self.p.wait_for_selector("#result .endpoints", state="attached", timeout=30000)  # hidden in the phone sheet
        self.p.wait_for_function("() => document.querySelector('#result .endpoints').textContent.trim().length > 3")


def card_html(title, lines, foot=""):
    body = "".join(f"<p class='l{i}'>{html.escape(t)}</p>" for i, t in enumerate(lines))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      html,body{{margin:0;height:100%;background:#0b1220;color:#fff;font-family:Inter,system-ui,sans-serif}}
      .wrap{{height:100%;display:flex;flex-direction:column;justify-content:center;padding:0 90px}}
      .bar{{width:120px;height:8px;background:#b91c1c;border-radius:4px;margin-bottom:28px}}
      h1{{font-size:54px;line-height:1.1;margin:0 0 18px;font-weight:800;letter-spacing:-.5px}}
      .l0{{font-size:26px;color:#cbd5e1;margin:0 0 30px;line-height:1.35;max-width:1000px}}
      .l1{{font-size:28px;font-weight:600;margin:0 0 6px}} .l2{{font-size:22px;color:#93c5fd;margin:0}}
      .l3,.l4,.l5{{font-size:21px;color:#cbd5e1;margin:6px 0 0}}
      .foot{{position:absolute;bottom:34px;left:90px;right:90px;font-size:17px;color:#94a3b8}}
      .blue{{color:#60a5fa}} .grey{{color:#9ca3af}}
    </style></head><body><div class="wrap"><div class="bar"></div><h1>{html.escape(title)}</h1>{body}</div>
    <div class="foot">{foot}</div></body></html>"""


# ---- scenes -----------------------------------------------------------------------------------
def scene_title(s):
    s.p.set_content(card_html(TITLE, [SUBTITLE, AUTHOR, SITE],
                              "Houston 311 flood reports 2011–2026 · Houston firefighters' 2018 survey · live reports from drivers"))
    s.p.wait_for_timeout(6500)


def scene_search_follow(s):
    s.p.goto(BASE)
    s.settle(1.5)
    s.cap("Houston floods often, and the same streets flood again and again.", 3.5)
    s.cap("The coloured hexagons are 15 years of <b>311 flood reports</b>, scored by how often a spot floods in separate storms.", 4.5)
    s.cap("Plan a trip: type where you're starting…", 0)
    s.type_pick("#fromInput", "houston heights", "Houston Heights")
    s.cap("…and where you're going.", 0)
    s.type_pick("#toInput", "second ward", "Second Ward")
    s.wait_route()
    s.settle(1.5)
    s.cap("<span style='color:#9ca3af'>Grey</span> is the normal fastest route. <span style='color:#60a5fa'>Blue</span> is the flood-aware route.", 4.5)
    s.cap("The blue route avoids <b>14 flood-history hotspots</b>, including every high-risk one…", 4)
    s.cap("…for about <b>two extra minutes</b>. On a storm day, that's the difference.", 4)
    s.click("#simBtn", 0.3)
    s.cap("Simulated drive along the flood-aware route (8× speed). Voice alerts announce risky areas ahead.", 5)
    s.cap("", 0)
    s.p.wait_for_timeout(14000)
    s.click("#stopDrive", 0.8)


def scene_ignore(s):
    s.p.goto(trip_url("second_montrose"))
    s.wait_route()
    s.settle(1.5)
    s.cap("What if the driver ignores the advice and stays on the fastest route?", 4)
    s.click("#simIgnoreBtn", 0.3)
    s.cap("The simulated car keeps to the <span style='color:#9ca3af'>grey</span> route. Dots show where it has been.", 5)
    s.cap("Each time it leaves the <span style='color:#60a5fa'>blue</span> advice, the app re-plans from the car's position, live.", 6)
    s.cap("", 0)
    s.p.wait_for_timeout(16000)
    s.cap("The blue <b>↻ re-plans</b> counter in the top-right card keeps count. Warnings describe the road actually being driven.", 5)
    s.click("#stopDrive", 0.8)


def scene_firefighters(s):
    s.p.goto(trip_url("clearlake"))
    s.wait_route()
    s.settle(1.5)
    s.cap("311 misses places where few residents call, like roads in front of fire stations.", 4.5)
    s.cap("So we added the <b>Houston firefighters' 2018 flood survey</b> (Rice University / Kinder Institute).", 4.5)
    x, y = s.map_point(29.5723, -95.1095)
    s.point(x, y, 0.3)
    s.p.mouse.move(x + 1, y + 1)
    s.cap("", 0)
    s.p.wait_for_timeout(3500)
    s.cap("Station 72: <i>“Bay Area Blvd … floods all the way to the front of fire station 72.”</i>", 5)
    s.point(640, 650, 0.2)
    s.cap("The fastest route runs straight down Bay Area Blvd. The flood-aware route avoids almost a <b>mile</b> of it, for one minute.", 6)


def scene_live_report(s):
    s.p.goto(trip_url("braeswood_rice"))
    s.wait_route()
    s.settle(1.5)
    s.cap("History isn't enough on the day. Drivers can report flooding they see.", 4)
    s.click("#simBtn", 0.3)
    s.cap("", 0)
    s.p.wait_for_timeout(5500)
    s.cap("A driver meets a flooded road and taps <b>⛔ Impassable</b>.", 2.5)
    s.click("#drive .report-row button[data-kind=impassable]", 0.5)
    s.cap("The report snaps to the road, and the flood-aware route re-plans at once, for everyone.", 5)
    s.cap("One report is a strong warning. Two different phones within 30 minutes close the road. Reports expire after a few hours.", 6)
    s.cap("", 0)
    s.p.wait_for_timeout(6000)
    s.click("#stopDrive", 0.8)
    s.settle(1)
    s.cap("The report stays on the map (⛔) until it expires or other drivers say the water is gone.", 5)


def scene_context(s):
    s.p.goto(f"{BASE}?view=29.690,-95.448,14&surface=1")
    s.settle(2.5)
    s.cap("The smoothed risk surface: Brays Bayou through Meyerland stands out.", 5)
    s.p.goto(f"{BASE}?view=29.781,-95.405,13&surface=1")
    s.settle(2.5)
    s.cap("Between White Oak and Buffalo Bayous, the same streets flooded in up to 19 separate storms.", 5)
    s.click("#hfdToggle", 0.3)
    s.click("#riskToggle", 0.3)
    s.cap("Everything runs on public data and our own server: no personal data leaves it.", 4.5)


def scene_phone(s):
    s.p.goto(trip_url("heights_second"))
    s.wait_route()
    s.settle(1.5)
    s.cap("On a phone: addresses at the top, results in a sheet.", 4)
    s.click("#peek", 0.5)
    s.cap("Drive mode follows GPS, keeps the screen on, and speaks alerts.", 4)
    s.p.goto(trip_url("heights_second", "&sim=1"))
    s.wait_route()
    s.cap("", 0)
    s.p.wait_for_timeout(9000)


def scene_end(s):
    s.p.set_content(card_html(TITLE, [
        "Flood-aware routes from 163,000 flood reports (311, 2011–2026), 22 firefighter-reported streets, and live driver reports.",
        AUTHOR, SITE,
        "Not for emergency use. Turn around, don't drown.",
    ], "Map data © OpenStreetMap contributors · Routing: GraphHopper · Addresses: US Census TIGER · "
       "311 data: City of Houston · Firefighter survey: Rice University / Kinder Institute"))
    s.p.wait_for_timeout(7000)


SCENES = [
    ("title", scene_title, "desktop"),
    ("search_follow", scene_search_follow, "desktop"),
    ("ignore_advice", scene_ignore, "desktop"),
    ("firefighters", scene_firefighters, "desktop"),
    ("live_report", scene_live_report, "desktop"),
    ("context", scene_context, "desktop"),
    ("phone", scene_phone, "phone"),
    ("end", scene_end, "desktop"),
]


def clear_reports():
    subprocess.run([sys.executable, "-m", "api.live_admin", "clear-all"], cwd=ROOT, capture_output=True)


def to_mp4(webm: Path, mp4: Path, phone: bool):
    vf = ("scale=-2:720,pad=1280:720:(ow-iw)/2:0:color=0x0b1220,format=yuv420p" if phone
          else "scale=1280:720,format=yuv420p")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm), "-vf", vf, "-r", "30",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", str(mp4)], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated scene numbers (1-based) to record")
    ap.add_argument("--no-combine", action="store_true")
    args = ap.parse_args()
    only = {int(x) for x in args.only.split(",")} if args.only else None
    OUT.mkdir(parents=True, exist_ok=True)
    raw = OUT / "raw"
    clear_reports()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        for n, (name, fn, kind) in enumerate(SCENES, 1):
            if only and n not in only:
                continue
            vid_dir = raw / f"{n:02d}_{name}"
            shutil.rmtree(vid_dir, ignore_errors=True)
            if kind == "phone":
                ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                                          has_touch=True, record_video_dir=str(vid_dir),
                                          record_video_size={"width": 390, "height": 844})
            else:
                ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(vid_dir),
                                          record_video_size={"width": W, "height": H})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            t0 = time.time()
            fn(Scene(page))
            ctx.close()
            webm = next(vid_dir.glob("*.webm"))
            mp4 = OUT / f"{n:02d}_{name}.mp4"
            to_mp4(webm, mp4, kind == "phone")
            print(f"{mp4.name}: {time.time() - t0:.0f} s" + (f"  page errors: {errors}" if errors else ""), flush=True)
            clear_reports()
        browser.close()
    if not args.no_combine:
        clips = sorted(OUT.glob("[0-9][0-9]_*.mp4"))
        lst = OUT / "concat.txt"
        lst.write_text("".join(f"file '{c.name}'\n" for c in clips))
        final = OUT / "floodroutes_demo.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", "-movflags", "+faststart", str(final)], check=True, cwd=OUT)
        dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(final)],
                             capture_output=True, text=True).stdout.strip()
        print(f"{final}: {float(dur):.0f} s, {final.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
