"""Turns a time range into a vertical clip: 9:16 layout, bouncy word captions, hook title, cover."""
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import config, roman
from .media import Cancelled, run, run_cancellable

W, H = config.OUT_W, config.OUT_H

# ASS colours are &HBBGGRR. `size` is the ASS font size (line height); _Font turns it into real pixels.
STYLES = {
    "bounce": {"label": "Bounce", "font": "Poppins ExtraBold", "size": 132, "base": "FFFFFF", "active": "00F0FF",
               "strong": "FFFFFF", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hype": {"label": "Hype", "font": "Anton", "size": 152, "base": "FFFFFF", "active": "3C3CFF",
             "strong": "00D7FF", "outline": 7, "pop": 114, "upper": True, "per": 2},
    "clean": {"label": "Clean", "font": "Poppins ExtraBold", "size": 100, "base": "FFFFFF", "active": "6BD7FF",
              "strong": "FFFFFF", "outline": 5, "pop": 106, "upper": False, "per": 4},
}
HOOK = {"font": "Poppins ExtraBold", "size": 96, "ink": "14100E", "box": "FFFFFF", "pad_x": 36, "pad_y": 22, "radius": 26,
        "max_w": 900}
FONT_FILES = {"Poppins ExtraBold": "Poppins-ExtraBold.ttf", "Anton": "Anton-Regular.ttf", "Archivo Black": "ArchivoBlack-Regular.ttf"}
CAP_MAX_W = 960   # captions never run wider than this, whatever is said


class _Font:
    """Measures text exactly as the renderer will draw it, so every word can be placed by hand: the highlighted
    word can grow without pushing its neighbours, and the heading box fits its text."""
    _cache: dict = {}

    def __init__(self, name):
        import struct
        path = Path(__file__).resolve().parent / "fonts" / FONT_FILES.get(name, FONT_FILES["Poppins ExtraBold"])
        d = path.read_bytes()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        self.upm = struct.unpack(">H", d[tables[b"head"] + 18: tables[b"head"] + 20])[0]
        o = tables[b"OS/2"]
        self.asc, self.desc = struct.unpack(">HH", d[o + 74: o + 78])      # what the renderer sizes the font by
        self.cap = struct.unpack(">h", d[o + 88: o + 90])[0] or int(self.upm * 0.7)
        self.path = str(path)
        self._pil = {}

    @classmethod
    def get(cls, name):
        if name not in cls._cache:
            cls._cache[name] = cls(name)
        return cls._cache[name]

    def em(self, size):
        return size * self.upm / (self.asc + self.desc)

    def width(self, text, size):
        """Pixels wide at this ASS size."""
        try:
            from PIL import ImageFont
            if "f" not in self._pil:
                self._pil["f"] = ImageFont.truetype(self.path, 200)
            return self._pil["f"].getlength(text) * self.em(size) / 200
        except Exception:  # noqa: BLE001 - no measuring library: a fair estimate keeps clips rendering
            return len(text) * self.em(size) * 0.62

    def cap_shift(self, size):
        """How far below the anchor the middle of a capital letter sits when text is centred on a point."""
        return ((self.asc - self.desc) / 2 - self.cap / 2) * size / (self.asc + self.desc)


CAPTION_LEAD = 0.0  # measured: word stamps are within 0.15 s of the voice, so no shift is needed
def install_fonts():
    """Copy the bundled caption fonts next to the data, so captions look the same on every machine and server."""
    src = Path(__file__).resolve().parent / "fonts"
    for f in src.glob("*.ttf"):
        if not (config.FONTS_DIR / f.name).exists():
            shutil.copyfile(f, config.FONTS_DIR / f.name)


LAYOUTS = {"blur": "Full frame + blurred fill", "zoom": "Zoomed in + blurred fill", "crop": "Fill the screen (centre crop)",
           "left": "Fill the screen, keep the left side", "right": "Fill the screen, keep the right side",
           "fit": "Full frame on black", "split": "Split screen: left half on top, right half below (two people)",
           "gameplay": "Gameplay fills the screen, facecam in the corner"}
_FILLS = ("crop", "left", "right", "gameplay")      # layouts where the picture covers the whole frame


def _ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


def _clean(text):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", re.sub(r"[{}\\]", "", str(text))).strip()


def _key(word):
    return re.sub(r"[^a-z0-9']", "", word.lower())


def _groups(words, per):
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= per or sum(len(x["w"]) for x in cur) + len(w["w"]) > 16
                    or w["s"] - cur[-1]["e"] > 0.45 or cur[-1]["w"][-1:] in ".?!"):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def geometry(src_w, src_h, layout):
    """Where the video sits in the 1080x1920 frame, and where hook and captions go."""
    if src_h >= src_w and layout != "crop":
        layout = "crop"  # already vertical or square: nothing to gain from a blurred fill
    if layout not in LAYOUTS:
        layout = "blur"
    if layout in ("blur", "fit"):
        fg_h = min(H, int(round(W * src_h / src_w / 2)) * 2)
    elif layout == "zoom":
        fg_h = 1000
    else:
        fg_h = H
    top = (H - fg_h) // 2
    if layout in _FILLS:
        return layout, fg_h, 300, 1380
    if layout == "split":
        return layout, fg_h, 230, H // 2        # captions sit on the line between the two people
    return layout, fg_h, max(230, top - 190), min(1640, top + fg_h + 180)


def _hook_events(hook, dur, y):
    """The heading: dark text in a white rounded box that fits it, on one line or two balanced ones."""
    f, size = _Font.get(HOOK["font"]), HOOK["size"]
    words = hook.split()
    if not words:
        return []
    lines = [" ".join(words)]
    if f.width(lines[0], size) > HOOK["max_w"] and len(words) > 1:      # two lines, split where they come out most even
        cut = min(range(1, len(words)), key=lambda i: max(f.width(" ".join(words[:i]), size), f.width(" ".join(words[i:]), size)))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    widest = max(f.width(l, size) for l in lines)
    if widest > HOOK["max_w"]:
        size = max(40, int(size * HOOK["max_w"] / widest))
        widest = max(f.width(l, size) for l in lines)
    line_h = f.em(size) * 1.16
    bw, bh = widest + 2 * HOOK["pad_x"], line_h * len(lines) + 2 * HOOK["pad_y"]
    x0, y0, r = (W - bw) / 2, y - bh / 2, min(HOOK["radius"], bh / 2)
    x1, y1 = x0 + bw, y0 + bh
    n = lambda v: f"{v:.0f}"                                                         # noqa: E731
    box = (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
           f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} {n(x1 - r)} {n(y1)} "
           f"l {n(x0 + r)} {n(y1)} b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
           f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} {n(x0 + r)} {n(y0)}")
    span = f"{_ts(0)},{_ts(dur)}"
    ev = [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)\\c&H{HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{\\an5\\pos({W // 2},{cy:.0f})\\fad(160,0)\\fs{size}}}{line}")
    return ev


def build_ass(words, start, end, hook, style_name, highlight, hook_y, cap_y) -> str:
    st = STYLES.get(style_name, STYLES["bounce"])
    words = [dict(w, s=w["s"] - CAPTION_LEAD, e=w["e"] - CAPTION_LEAD) for w in words]
    strong = {_key(h) for h in highlight if _key(h)}
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},&H00{st['base']},&H00{st['base']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{st['outline']},0,5,0,0,0,1
Style: Hook,{HOOK['font']},{HOOK['size']},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = end - start
    ev = _hook_events(_clean(hook), dur, hook_y)
    f = _Font.get(st["font"])
    inside = [dict(w, w=_clean(w["w"])) for w in words if w["s"] >= start - 0.05 and w["s"] < end]
    inside = sorted((w for w in inside if w["w"]), key=lambda w: w["s"])
    for i, w in enumerate(inside[:-1]):
        w["e"] = max(w["s"], min(w["e"], inside[i + 1]["s"]))
    groups = _groups(inside, st["per"])
    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["s"] - start)
        g_end = min(g[-1]["e"] + 0.25, groups[gi + 1][0]["s"] if gi + 1 < len(groups) else end) - start
        if g_end - g_start < 0.02:
            continue
        texts = []
        for x in g:
            t = re.sub(r"[.,;:]+$", "", x["w"])
            texts.append(t.upper() if st["upper"] else t)
        if not all(roman.drawable(t) for t in texts):
            # A script the caption font cannot draw (Japanese, Thai, Chinese...): its words cannot be measured, so
            # they are not placed one by one. The line is drawn whole and centred, in a font the system has for it.
            line = " ".join(texts)
            fs = max(44, min(int(st["size"] * 0.7), int(1500 / max(1, len(line)) * 1.6)))
            ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,60,60,0,,{{\\an5\\pos({W // 2},{cap_y:.0f})\\q0\\fs{fs}}}{line}")
            continue
        size = st["size"]
        gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / st["size"]   # a space, plus room for both outlines
        total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        if total > CAP_MAX_W:                                  # a long phrase: shrink it to fit, never run off the frame
            size = max(36, int(size * CAP_MAX_W / total))
            gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / st["size"]   # a space, plus room for both outlines
            total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cy = cap_y - f.cap_shift(size)
        x = (W - total) / 2
        p = st["pop"]
        for i, (w, t) in enumerate(zip(g, texts)):
            wd = f.width(t, size)
            p = max(104, min(st["pop"], 100 + int(2200 / max(wd, 1))))   # long words grow less, so they never touch their neighbours
            pos = f"\\an5\\pos({x + wd / 2:.0f},{cy:.0f})\\fs{size}"
            x += wd + gap
            a = max(g_start, w["s"] - start)
            b = (g[i + 1]["s"] - start) if i + 1 < len(g) else g_end
            base = st["strong"] if _key(w["w"]) in strong else st["base"]
            if a - g_start >= 0.02:                                                   # waiting to be said
                ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(a)},Cap,,0,0,0,,{{{pos}\\c&H{base}&}}{t}")
            if b - a >= 0.02:                                                         # being said: colour and a quick pop, in front
                ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&\\fscx94\\fscy94"
                          f"\\t(0,90,\\fscx{p}\\fscy{p})\\t(90,200,\\fscx{p - 5}\\fscy{p - 5})}}{t}")
            if g_end - b >= 0.02:                                                     # said
                ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,{{{pos}\\c&H{base}&\\fscx{p - 5}\\fscy{p - 5}"
                          f"\\t(0,90,\\fscx100\\fscy100)}}{t}")
    return head + "\n".join(ev) + "\n"


def remap_words(words, segments):
    """Filter out words in cut gaps and shift words in later segments so subtitles match concatenated video."""
    if not segments or len(segments) <= 1:
        return words
    out = []
    seg_cum = []
    cum = 0.0
    for s0, s1 in segments:
        seg_cum.append((s0, s1, cum))
        cum += (s1 - s0)
    for w in words:
        ws, we = w.get("s", 0.0), w.get("e", 0.0)
        for s0, s1, c_offset in seg_cum:
            if s0 - 0.05 <= ws < s1 + 0.05:
                new_s = c_offset + max(0.0, ws - s0)
                new_e = c_offset + max(0.0, min(s1 - s0, we - s0))
                base_start = segments[0][0]
                out.append(dict(w, s=round(base_start + new_s, 2), e=round(base_start + new_e, 2)))
                break
    return out


PIP_W = 380            # how wide the facecam is in the 1080-wide frame
PIP_MARGIN = 26


def _filter(layout, fg_h, facecam=None, v_in="0:v"):
    v_tag = f"[{v_in}]" if not v_in.startswith("[") else v_in
    if layout == "gameplay" and facecam:
        # The game fills the screen, and the webcam sits in the top corner of it, so a viewer sees
        # the play and the face at the same time.
        fx, fy, fw, fh = (int(v) for v in facecam)
        if fw > 8 and fh > 8:
            pip_h = max(2, int(round(PIP_W * fh / fw)))
            return (f"{v_tag}fps={config.OUT_FPS},split=2[base][fc];"
                    f"[base]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[b];"
                    f"[fc]crop={fw}:{fh}:{fx}:{fy},scale={PIP_W}:{pip_h},setsar=1[p];"
                    f"[b][p]overlay={W - PIP_W - PIP_MARGIN}:{PIP_MARGIN}[v0];")
    fill = f"{v_tag}fps={config.OUT_FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    if layout == "crop" or (layout == "gameplay" and not facecam):
        return fill + ",setsar=1[v0];"
    if layout == "left":
        return fill + ":0:0,setsar=1[v0];"
    if layout == "right":
        return fill + ":in_w-out_w:0,setsar=1[v0];"
    if layout == "fit":
        return f"{v_tag}fps={config.OUT_FPS},scale={W}:{fg_h},pad={W}:{H}:0:(oh-ih)/2:black,setsar=1[v0];"
    if layout == "split":
        half = f"scale={W}:{H // 2}:force_original_aspect_ratio=increase,crop={W}:{H // 2}"
        return (f"{v_tag}fps={config.OUT_FPS},split=2[l][r];[l]crop=iw/2:ih:0:0,{half}[t];"
                f"[r]crop=iw/2:ih:iw/2:0,{half}[u];[t][u]vstack,setsar=1[v0];")
    return (f"{v_tag}fps={config.OUT_FPS},split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
            f"scale={W}:{H},eq=brightness=-0.14:saturation=1.15[bg];"
            f"[b]scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h}[fg];"
            f"[bg][fg]overlay=0:(H-h)/2,setsar=1[v0];")


def _hook_y(layout, hook_y, facecam=None):
    """Where the heading goes. In the gameplay layout the webcam sits in the top corner, so a heading
    across the top would run under it: it goes just below the picture instead."""
    if layout == "gameplay" and facecam and len(facecam) == 4 and facecam[2] > 8:
        return PIP_MARGIN + int(round(PIP_W * facecam[3] / facecam[2])) + 32
    return hook_y


_qsv_ok = None
RENDER_SECONDS = 600      # least time one clip's encode may take; longer clips and slower computers get more (see _timeout)
WEAK_CPU = (os.cpu_count() or 4) <= 4     # a laptop-class processor: encode lighter so it finishes in sane time
# x264 speed: "ultrafast" is ~40% quicker than "veryfast" on a weak CPU for a slightly larger file. PEAKCUT_X264_PRESET overrides.
X264 = (os.environ.get("PEAKCUT_X264_PRESET", "").strip() or ("ultrafast" if WEAK_CPU else "veryfast"),
        "24" if WEAK_CPU else "21")


def _timeout(seconds):
    """A clip made on a slow computer, while it is also busy with other videos, can run many times slower than
    real time. Allow generously by clip length so a slow-but-working encode is not killed; a frozen one still is."""
    return max(RENDER_SECONDS, seconds * (40 if WEAK_CPU else 15))


def has_audio(src: Path) -> bool:
    """Does this file have any sound? A silent video is a normal thing to clip (finding clips by what is on screen)."""
    try:
        out = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(src)])
    except RuntimeError:
        return True        # cannot tell: behave as before
    return bool(out.strip())


LOUD = "loudnorm=I=-14:TP=-1.5:LRA=11"
DUCK = 0.25          # how loud the video's own sound is left under a voice-over


def _sound(has_sound: bool, voice: bool, speed: float = 1.0, a_in: str = "0:a:0") -> tuple:
    """The sound of a clip: (filter text to add after the picture, ffmpeg arguments for it). Four cases:
    the video's own sound; no sound at all; a voice-over alone; a voice-over over the video's own sound, turned down."""
    sp_filter = ""
    if speed != 1.0:
        if speed == -1.0:
            sp_filter = ",areverse"
        elif speed == -2.0:
            sp_filter = ",areverse,atempo=2.0"
        elif speed > 0:
            sp_filter = f",atempo={speed:.4f}"

    in_tag = f"[{a_in}]" if not a_in.startswith("0:") else a_in
    in_filt_tag = f"[{a_in}]" if not a_in.startswith("[") else a_in
    if voice and has_sound:
        return (f";{in_filt_tag}volume={DUCK}{sp_filter}[o];[1:a:0][o]amix=inputs=2:duration=longest:normalize=0,{LOUD}[a]", ["-map", "[a]"])
    if voice:
        return (f";[1:a:0]{LOUD},apad[a]", ["-map", "[a]"])
    if has_sound:
        af = f"{sp_filter[1:]},{LOUD}" if sp_filter else LOUD
        if a_in and a_in != "0:a:0":
            return (f";{in_filt_tag}{af}[a]", ["-map", "[a]"])
        return ("", ["-map", in_tag, "-af", af])
    return ("", ["-an"])


def render_clip(src: Path, out_dir: Path, name: str, clip: dict, words: list, src_w: int, src_h: int, voiceover: Path | None = None, cancel=None) -> dict:
    """Writes <name>.mp4, <name>.jpg and <name>.ass into out_dir. Returns file names.
    `voiceover` is a sound file (made by tts.py) read over the clip, which starts at second 0 of the clip.
    `cancel` is asked twice a second while ffmpeg works; when it says yes, ffmpeg is stopped and Cancelled is raised."""
    src = Path(src).resolve()
    if voiceover:
        voiceover = Path(voiceover).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = clip["start"], clip["end"]
    segments = clip.get("segments")
    if segments and isinstance(segments, list) and len(segments) > 1:
        clean_segs = []
        for s in segments:
            if isinstance(s, (list, tuple)) and len(s) == 2:
                s0, s1 = round(float(s[0]), 2), round(float(s[1]), 2)
                if s1 > s0:
                    clean_segs.append([s0, s1])
        if clean_segs:
            clean_segs.sort(key=lambda x: x[0])
            segments = clean_segs
            start = segments[0][0]
            end = segments[-1][1]
            total_dur = sum(s1 - s0 for s0, s1 in segments)
            words = remap_words(words, segments)
        else:
            segments = None
            total_dur = end - start
    else:
        segments = None
        total_dur = end - start

    speed = float(clip.get("speed", 1.0) or 1.0)
    if speed not in (-2.0, -1.0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
        speed = 1.0
    layout, fg_h, hook_y, cap_y = geometry(src_w, src_h, clip.get("layout", "blur"))
    face = clip.get("facecam")
    if layout == "gameplay" and not (face and len(face) == 4):
        layout = "crop"     # no webcam found: the game fills the screen, which is still the right shape
    hook_y = _hook_y(layout, hook_y, face)
    hook = clip.get("hook", "") if clip.get("big_text", True) else ""   # big text switched off: no title box on the video

    if speed > 0 and speed != 1.0 and words:
        words = [dict(w, s=start + (w["s"] - start) / speed, e=start + (w["e"] - start) / speed) for w in words]
        end_ass = start + total_dur / speed
    elif speed < 0:
        words = []
        end_ass = start + total_dur / abs(speed)
    else:
        end_ass = start + total_dur

    ass = build_ass(words if clip.get("captions", True) else [], start, end_ass, hook, clip.get("style", "bounce"),
                    clip.get("highlight_words", []), hook_y, cap_y)
    # The clip is made in a short work folder and moved into place afterwards. The caption step of ffmpeg cannot
    # open files whose full path is longer than 260 characters on Windows, which a data folder deep inside other
    # folders easily reaches. Inside the work folder every name is short and relative (a drive colon would also
    # break the filter syntax).
    work = Path(tempfile.mkdtemp(prefix="pk"))
    try:
        # The title and description are also written into the file itself, for any place that reads them from it.
        meta = {"title": clip.get("title") or "", "description": clip.get("description") or "", "comment": clip.get("description") or ""}
        return _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, face=face, meta=meta, voiceover=voiceover, cancel=cancel, speed=speed, segments=segments)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_in(work: Path, src: Path, out_dir: Path, name: str, start, end, layout, fg_h, ass: str, face=None, meta=None, voiceover=None, cancel=None, speed: float = 1.0, segments=None) -> dict:
    global _qsv_ok
    (work / f"{name}.ass").write_text(ass, encoding="utf-8")
    (work / "f").mkdir()
    for f in config.FONTS_DIR.glob("*.ttf"):
        shutil.copyfile(f, work / "f" / f.name)
    voice = bool(voiceover) and Path(voiceover).is_file()

    total_dur = sum(s1 - s0 for s0, s1 in segments) if (segments and len(segments) > 1) else (end - start)
    out_dur = total_dur / abs(speed)

    trim_fc = ""
    v_in = "0:v"
    a_in = "0:a:0"
    if segments and len(segments) > 1:
        N = len(segments)
        for i, (s0, s1) in enumerate(segments):
            rel_s0 = s0 - start
            rel_s1 = s1 - start
            trim_fc += f"[0:v]trim=start={rel_s0:.2f}:end={rel_s1:.2f},setpts=PTS-STARTPTS[v_seg_{i}];"
            if has_audio(src):
                trim_fc += f"[0:a]atrim=start={rel_s0:.2f}:end={rel_s1:.2f},asetpts=PTS-STARTPTS[a_seg_{i}];"
        inputs = ""
        for i in range(N):
            inputs += f"[v_seg_{i}]"
            if has_audio(src):
                inputs += f"[a_seg_{i}]"
        if has_audio(src):
            trim_fc += f"{inputs}concat=n={N}:v=1:a=1[src_v][src_a];"
            v_in = "src_v"
            a_in = "src_a"
        else:
            trim_fc += f"{inputs}concat=n={N}:v=1:a=0[src_v];"
            v_in = "src_v"
            a_in = None

    sound_fc, sound_args = _sound(has_audio(src), voice, speed=speed, a_in=a_in)

    if speed == 1.0:
        fc = trim_fc + _filter(layout, fg_h, face, v_in=v_in) + f"[v0]ass={name}.ass:fontsdir=f[v]" + sound_fc
    else:
        if speed == -2.0:
            sp_vf = "reverse,setpts=0.5*PTS"
        elif speed == -1.0:
            sp_vf = "reverse,setpts=PTS-STARTPTS"
        else:
            sp_vf = f"setpts={(1.0 / speed):.6f}*PTS"
        fc = trim_fc + _filter(layout, fg_h, face, v_in=v_in) + f"[v0]ass={name}.ass:fontsdir=f[v_sub];[v_sub]{sp_vf}[v]" + sound_fc

    base = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(src)]
    if voice:
        base += ["-i", str(voiceover)]
    base += ["-filter_complex", fc, "-map", "[v]", *sound_args, "-t", f"{out_dur:.2f}"]
    if "-an" not in sound_args:
        base += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    for k, v in (meta or {}).items():
        if v:
            base += ["-metadata", f"{k}={v}"]
    base += ["-movflags", "+faststart", "-pix_fmt"]
    encoders = []
    if _qsv_ok is not False:
        encoders.append(("qsv", ["nv12", "-c:v", "h264_qsv", "-global_quality", "23", "-preset", "fast"]))
    encoders.append(("x264", ["yuv420p", "-c:v", "libx264", "-crf", X264[1], "-preset", X264[0]]))
    err = None
    for label, enc in encoders:
        p = run_cancellable(base + enc + [f"{name}.mp4"], cwd=work, timeout=_timeout(out_dur), cancel=cancel)
        if p.returncode == 0:
            if label == "qsv":
                _qsv_ok = True
            break
        err = p.stderr[-1200:]
        if label == "qsv":
            _qsv_ok = False
    else:
        raise RuntimeError(f"Render failed: {err}")
    cover_t = min(1.2, out_dur / 2)
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", "-i", f"{name}.mp4", "-frames:v", "1",
         "-q:v", "3", f"{name}.jpg"], cwd=work)
    for ext in ("mp4", "jpg", "ass"):
        shutil.move(str(work / f"{name}.{ext}"), str(out_dir / f"{name}.{ext}"))
    return {"file": f"{name}.mp4", "cover": f"{name}.jpg", "layout": layout}
