"""Job pipeline: get video -> audio -> transcript -> AI picks -> rendered clips. One heavy task at a time."""
import collections
import json
import os
import math
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import brain, config, creators, ctx, facecam, fetch_model, games, hashtags, load, media, quality, references, render, roman, safe, transcribe, tts, usage, vision

class _FairQueue:
    """Whose video is next. Each person has their own line and the lines take turns, so someone who adds ten
    videos does not make everyone else wait behind all ten. Small tasks (re-making one clip) go first."""

    def __init__(self):
        self.cv = threading.Condition()
        self.quick = collections.deque()
        self.lines = {}                       # user -> their waiting videos, oldest first
        self.turn = collections.deque()       # users with something waiting, in the order they will be served
        self.epoch = 0                        # goes up whenever the line moves
        self._pos = (0.0, {})
        self.running = 0                      # videos being worked on right now
        self.last_start = 0.0
        self.held = False                     # videos are waiting only because the machine is near full

    def _room(self) -> bool:
        """May one more video start? One always may. More only while the machine has room, and a few seconds
        after the last one started, so its weight is already showing in the measurements."""
        if self.running == 0:
            return True
        return self.running < config.JOB_WORKERS and time.time() - self.last_start > 6 and not load.busy()

    def done(self):
        with self.cv:
            self.running = max(0, self.running - 1)
            self.cv.notify_all()

    def put(self, item, user=None):
        with self.cv:
            if user is None:
                self.quick.append(item)
            else:
                if user not in self.lines:
                    self.lines[user] = collections.deque()
                    self.turn.append(user)
                self.lines[user].append(item)
            self.epoch += 1
            self.cv.notify()

    def get(self, quick_only=False):
        with self.cv:
            while True:
                if self.quick:
                    self.epoch += 1
                    return self.quick.popleft()
                if not quick_only and self.turn:
                    if self._room():
                        break
                    if not self.held:
                        self.held = True
                        self.epoch += 1
                elif self.held:
                    self.held = False
                    self.epoch += 1
                self.cv.wait(1.0)             # looked at again every second: the line moves the moment there is room
            self.epoch += 1
            self.held = False
            self.running += 1
            self.last_start = time.time()
            user = self.turn.popleft()
            item = self.lines[user].popleft()
            if self.lines[user]:
                self.turn.append(user)
            else:
                del self.lines[user]
            return item

    def ahead(self) -> dict:
        """job id -> how many videos will start before it. Worked out at most once a second."""
        with self.cv:
            if time.time() - self._pos[0] < 1:
                return self._pos[1]
            out, lines, order, n = {}, {u: list(q) for u, q in self.lines.items()}, list(self.turn), 0
            depth = 0
            while order:
                nxt = []
                for u in order:
                    if depth < len(lines[u]):
                        out[lines[u][depth][1]] = n
                        n += 1
                        if depth + 1 < len(lines[u]):
                            nxt.append(u)
                order, depth = nxt, depth + 1
            self._pos = (time.time(), out)
            return out


_q = _FairQueue()
_live_q: "queue.Queue" = queue.Queue()   # live clips never wait behind a long video
_jobs: dict = {}
_by_user: dict = {}                       # user -> {job id: job}: listing one person's videos never scans everyone's
_lock = threading.RLock()
on_clip_ready: list = []   # callbacks (job, clip), run once when a clip first finishes

DEFAULTS = {"max_clips": 6, "min_len": 15, "max_len": 60, "style": "bounce", "layout": "blur", "quality": 720,
            "range_start": None, "range_end": None, "captions": True, "big_text": True, "hook_text": "",
            "description_text": "", "lang": "en", "caption_lang": "hinglish",   # lang: the language spoken, caption_lang: caption language
            # finding clips by what is on screen (studio/vision.py): detail is how many frames, parts how many pieces
            # the video is chopped into (0: about one per eight minutes), reference a saved reference's id
            "vision": False, "parts": 0, "detail": vision.DEFAULT_DETAIL, "reference": "", "ref_prompt": "",
            "voiceover": False, "voice": tts.DEFAULT_VOICE}


LIMITS = {"max_clips": (1, 20), "min_len": (5, 120), "max_len": (10, 180), "quality": (360, 1080), "parts": (0, vision.MAX_PARTS)}


def clean_options(options) -> dict:
    """Only known settings, each inside its allowed range. Raises ValueError on nonsense."""
    out = {}
    for k, v in (options if isinstance(options, dict) else {}).items():
        if k in ("captions", "big_text"):
            if not isinstance(v, bool):
                raise ValueError("Captions must be on or off." if k == "captions" else "Big text must be on or off.")
            out[k] = v
            continue
        if k in ("vision", "voiceover"):
            if not isinstance(v, bool):
                raise ValueError("Finding clips by sight and the voice-over must each be on or off.")
            out[k] = v
            continue
        if k == "caption_lang":
            v = str(v).lower().strip()
            if v in ("hinglish", "hindi", "english"):
                out[k] = v
            continue
        if k == "speed":
            try:
                sp = float(v)
                if sp in (-2.0, -1.0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
                    out[k] = sp
            except (ValueError, TypeError):
                pass
            continue
        if k == "segments":
            try:
                raw = v
                if isinstance(raw, str):
                    raw = json.loads(raw)
                if isinstance(raw, list):
                    segs = []
                    for item in raw:
                        if isinstance(item, (list, tuple)) and len(item) == 2:
                            s0, s1 = round(float(item[0]), 2), round(float(item[1]), 2)
                            if math.isfinite(s0) and math.isfinite(s1) and s1 > s0:
                                segs.append([s0, s1])
                    if segs:
                        segs.sort(key=lambda x: x[0])
                        out[k] = segs
            except Exception:
                pass
            continue
        if k == "ref_prompt":              # the person's own words about the look: one line, kept even when empty
            out[k] = " ".join(str(v or "").split())[:500]
            continue
        if k in ("hook_text", "description_text"):     # kept even when empty: it clears the creator's own text for this video
            out[k] = creators.clean_hook(v) if k == "hook_text" else creators.clean_description(v)
            continue
        if k not in DEFAULTS or v in ("", None):
            continue
        if k in LIMITS:
            lo, hi = LIMITS[k]
            v = max(lo, min(hi, int(float(v))))
        elif k == "style":
            v = v if v in render.STYLES else DEFAULTS["style"]
        elif k == "layout":
            v = v if v in render.LAYOUTS else DEFAULTS["layout"]
        elif k == "lang":
            v = v if fetch_model.known(v) else DEFAULTS["lang"]
        elif k == "detail":
            v = v if v in vision.DETAIL else DEFAULTS["detail"]
        elif k == "reference":
            if not (isinstance(v, str) and references.ID.fullmatch(v)):
                raise ValueError("That reference isn't valid.")
        elif k == "voice":
            v = int(v) if tts.valid(v) else DEFAULTS["voice"]
        else:                                    # range_start, range_end: seconds into the video
            v = float(v)
            if not math.isfinite(v) or v < 0:
                raise ValueError("That part of the video isn't valid.")
        out[k] = v
    if out.get("range_end") is not None and out["range_end"] <= out.get("range_start", 0):
        raise ValueError("The end of the chosen part must be after its start.")
    return out


def job_dir(jid) -> Path:
    return Path(_jobs[jid]["dir"])


HIDDEN = ("dir", "user", "source_path", "recover")
CLIP_HIDDEN = ("src", "fixed_words", "fixed_for", "vo_file", "vo_words", "_prev")


def public(job) -> dict:
    """What the browser may see: no server paths, no owner id."""
    out = {k: v for k, v in job.items() if k not in HIDDEN}
    out["source"] = {k: v for k, v in job["source"].items() if k != "path"}
    out["clips"] = [{k: v for k, v in c.items() if k not in CLIP_HIDDEN} for c in job["clips"]]
    d = Path(job.get("dir") or "")
    out["has_source"] = bool((d / "source.mp4").is_file() or any(d.glob("source.*")) or (job.get("source_path") and Path(job["source_path"]).is_file()))
    ai = job.get("ai") or {}
    out["ai"] = {"tokens": ai.get("tokens", 0), "note": ai.get("note", ""), "used": bool(ai.get("model"))}   # never the model, never raw errors
    if job.get("status") == "queued":
        n = _q.ahead().get(job["id"])
        if n:
            out["stage"] = (f"High demand right now: {n} video{'s' if n != 1 else ''} ahead of yours" if _q.held else
                            f"Waiting in line: {n} video{'s' if n != 1 else ''} ahead of yours")
        elif n == 0 and _q.held:
            out["stage"] = "High demand right now: yours is next"
    return out


def save(job):
    ctx.bump(job.get("user"))
    with _lock:
        d = Path(job["dir"])
        for _ in range(5):
            try:
                text = json.dumps(job, indent=1)
                break
            except RuntimeError:  # changed mid-dump by another thread
                time.sleep(0.05)
        else:
            return
        tmp = d / "job.json.tmp"
        tmp.write_text(text, encoding="utf-8")
        _put_in_place(tmp, d / "job.json")


def _put_in_place(tmp: Path, target: Path):
    """Move the freshly written job.json over the old one.

    Windows refuses this ("Access is denied") for a moment whenever anything else has the old file open: a virus
    scanner, the search indexer, or another part of PeakCut reading it. A download saves many times a second, so it
    happens, and it used to stop the whole video. So it is tried again a few times, and if the file is still busy
    it is written in place; as a last resort this save is skipped, because the next one carries the same news."""
    for i in range(8):
        try:
            tmp.replace(target)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    try:
        target.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
        tmp.unlink(missing_ok=True)
    except OSError:
        pass


def _set(job, **kw):
    with _lock:
        job.update(kw)
    save(job)


# ---------------------------------------------------------------- cancelling
# Cancel is a flag that the work looks at between its steps, plus the running ffmpeg being stopped on the spot:
#   - a video: job["cancelled_at"] is set. Every clip that was queued before then is dropped, clips already finished are kept.
#   - a clip: clip["cancel"] is set. A clip that is only being made for the first time disappears; one that is being
#     made again (after Save and re-make) goes back to how it was.
Cancelled = media.Cancelled
UNDO = ("start", "end", "hook", "title", "description", "hashtags", "style", "layout", "captions", "big_text", "caption_lang", "speed", "segments")


def _cancelled(job, clip=None) -> bool:
    if clip is not None and clip.get("cancel"):
        return True
    at = job.get("cancelled_at")
    return bool(at and at > ((clip or {}).get("queued_at") or 0))


def _check(job):
    if _cancelled(job):
        raise Cancelled()


def _drop_clip(job, clip):
    """A cancelled clip stops existing, or, if it already had a finished video, goes back to that one."""
    d = Path(job["dir"]) / "clips"
    mine = any(c is clip for c in job["clips"])        # a clip already removed must not touch a newer clip's files
    if not mine:
        return
    had = bool(clip.get("file")) and (d / Path(str(clip["file"])).name).is_file()
    with _lock:
        if had:
            clip.update(clip.pop("_prev", None) or {})
            clip.pop("cancel", None)
            clip["status"] = "done"
        else:
            job["clips"][:] = [c for c in job["clips"] if c is not clip]
    if not had:
        for n in (f"{clip['id']}.ass", f"{clip['id']}.vo.wav", f"{clip['id']}.json"):
            (d / n).unlink(missing_ok=True)
        src = clip.get("src")
        if src and Path(src).name.startswith(f"{clip['id']}.src"):
            Path(src).unlink(missing_ok=True)
    save(job)


def _finish_cancel(job):
    """The video has stopped: clips that were not finished are gone, the rest stay, and it is no longer in progress."""
    for c in list(job["clips"]):
        if c.get("status") in ("queued", "rendering"):
            c["cancel"] = True
            _drop_clip(job, c)
    _set(job, status="done", stage="Cancelled by you", error=None)
    try:
        creators.log(job.get("creator"), f"Video cancelled: {job.get('title') or job['source'].get('name') or job['id']}")
    except Exception:  # noqa: BLE001
        pass


def cancel(job):
    """Stop a video that is waiting or being made. A waiting one stops at once; one being made stops within a moment."""
    if job.get("status") not in ("queued", "running") or job.get("kind") == "live":
        raise ValueError("This video isn't being made.")
    job["cancelled_at"] = time.time()
    if job["status"] == "queued":
        _finish_cancel(job)
    else:
        _set(job, stage="Cancelling…")


def cancel_clip(job, clip):
    """Stop one clip that is waiting or being made."""
    if clip.get("status") not in ("queued", "rendering"):
        raise ValueError("This clip isn't being made.")
    clip["cancel"] = True
    if clip["status"] == "queued" and not clip.get("file"):
        _drop_clip(job, clip)             # nothing started yet: gone at once
    else:
        save(job)


def _remap(path, folder: Path):
    """Paths saved before data moved into per-user folders (or on another machine): find the file by name."""
    if path and not Path(path).exists():
        cand = folder / Path(str(path).replace("\\", "/")).name
        if cand.exists():
            return str(cand)
    return path


def _load_file(f: Path, uid: str):
    try:
        job = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    job.update(dir=str(f.parent), user=uid)
    job.setdefault("kind", "video")
    job.setdefault("creator", None)
    job.setdefault("options", {}).setdefault("captions", True)
    job["options"].setdefault("big_text", True)
    if job.get("source_path"):
        job["source_path"] = _remap(job["source_path"], f.parent)
    if job.get("source", {}).get("path"):
        job["source"]["path"] = _remap(job["source"]["path"], f.parent)
    if job.get("cancelled_at") and job.get("status") in ("queued", "running") and job.get("kind") != "live":
        _jobs[job["id"]] = job                     # cancelled just before the server stopped: it stays cancelled
        _finish_cancel(job)
    if job.get("status") in ("queued", "running") and job.get("kind") != "live":
        # Picked up again as soon as the server is back (jobs.start), from the last finished part of the transcript.
        job.update(status="queued", stage="Picking up where it stopped", error=None, resume=True)
    if job.get("status") == "live":
        job.update(status="done", stage="Recording paused by a server update", was_live=True)
    buffered = job.get("kind") == "live" and any((f.parent / "buffer").glob("*.ts"))
    for c in job.get("clips", []):
        c.setdefault("captions", job["options"]["captions"])
        c.setdefault("big_text", job["options"]["big_text"])
        if c.get("src"):
            c["src"] = _remap(c["src"], f.parent / "clips")
        if c.get("status") in ("rendering", "queued"):
            if buffered and not c.get("src"):
                c["status"] = "queued"  # the stream is still on disk: live.recover() finishes these
                job["recover"] = True
            elif job.get("resume"):
                c["status"] = "queued"  # the video is being picked up again: this clip is made then
            elif job.get("kind") != "live" and (f.parent / "words.json").is_file():
                c["status"] = "queued"  # a single clip being re-made: jobs.start() makes it again
                job.setdefault("remake", []).append(c["id"])
            else:
                c.update(status="error", error="Interrupted by a restart. Open it and press Save and re-make.")
    _jobs[job["id"]] = job
    _by_user.setdefault(uid, {})[job["id"]] = job


def load_user(uid: str):
    d = config.USERS_DIR / uid
    for f in list((d / "jobs").glob("*/job.json")) + list((d / "creators").glob("*/*/*/job.json")):
        _load_file(f, uid)


def load_all():
    for d in config.USERS_DIR.iterdir():
        if d.is_dir():
            load_user(d.name)


def all_jobs() -> list:
    """Every user's jobs. Only for background work; never send this to a browser."""
    with _lock:
        return list(_jobs.values())


def list_jobs():
    u = ctx.uid()
    with _lock:
        return sorted((dict(j) for j in _by_user.get(u, {}).values()), key=lambda j: -j["created"])


def get(jid):
    """The job, only if it belongs to whoever this code is running for."""
    job = _jobs.get(jid)
    return job if job and job["user"] == ctx.uid() else None


def busy_count() -> int:
    u = ctx.uid()
    return sum(1 for j in list(_by_user.get(u, {}).values()) if j["kind"] == "video" and j["status"] in ("queued", "running"))


def waiting_total() -> int:
    return sum(1 for j in list(_jobs.values()) if j["kind"] == "video" and j["status"] == "queued")


def queue_epoch() -> int:
    return _q.epoch


def running_now() -> int:
    return _q.running


def create(source: dict, options: dict, enqueue=True, creator=None, kind="video") -> dict:
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]
    c = creators.get(creator) if creator else None
    if c:
        d = creators.root(c["id"]) / ("live" if kind == "live" else "videos") / jid
        base = {**DEFAULTS, **{k: c[k] for k in ("max_clips", "min_len", "max_len", "style", "layout", "captions", "lang") if k in c},
                "hook_text": creators.clean_hook(c.get("hook_text")),
                "description_text": creators.clean_description(c.get("description_text"))}
    else:
        d, base, creator = config.user_dir(ctx.uid()) / "jobs" / jid, dict(DEFAULTS), None
    opts = {**base, **clean_options(options)}
    if opts["min_len"] >= opts["max_len"]:
        opts["min_len"] = max(5, opts["max_len"] - 5)
    job = {"id": jid, "user": ctx.uid(), "kind": kind, "creator": creator, "dir": str(d), "created": time.time(), "source": source,
           "options": opts, "status": "queued", "stage": "Waiting", "progress": 0.0,
           "title": source.get("name", ""), "uploader": "", "duration": 0, "clips": [], "error": None, "ai": {}}
    d.mkdir(parents=True)
    with _lock:
        _jobs[jid] = job
        _by_user.setdefault(job["user"], {})[jid] = job
    save(job)
    if enqueue:
        _q.put(("job", jid, None), user=job["user"])
    return job


def enqueue(job):
    save(job)
    _q.put(("job", job["id"], None), user=job["user"])


def submit(fn):
    """Run any heavy task (a render, a cleanup) on the single worker, in order."""
    _q.put(("fn", ctx.bind(fn), None))


def submit_live(fn):
    """Live-stream renders and cleanups: their own worker, so a long video can never hold them up."""
    _live_q.put(ctx.bind(fn))


def _live_worker():
    while True:
        fn = _live_q.get()
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def delete(jid):
    with _lock:
        job = _jobs.pop(jid, None)
        if job:
            _by_user.get(job["user"], {}).pop(jid, None)
    if job:
        ctx.bump(job["user"])
        shutil.rmtree(job["dir"], ignore_errors=True)
    return bool(job)


def request_render(jid, cid, changes: dict):
    job = _jobs[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    new = dict(clip)
    for k in ("captions", "big_text", "speed", "segments", "caption_lang"):
        if k in changes:
            new.update(clean_options({k: changes[k]}))
    for k in ("start", "end"):
        if k in changes:
            new[k] = round(float(changes[k]), 2)
    if new.get("segments") and len(new["segments"]) > 1:
        new["start"] = new["segments"][0][0]
        new["end"] = new["segments"][-1][1]
        total_dur = sum(s1 - s0 for s0, s1 in new["segments"])
    else:
        if "segments" in new and len(new.get("segments", [])) <= 1:
            new.pop("segments", None)
        total_dur = new["end"] - new["start"]
    if "src_offset" in clip:  # live clip: can only move inside the saved piece of stream
        new["start"] = max(new["start"], clip["src_offset"])
        new["end"] = min(new["end"], clip["src_end"])
    if not (math.isfinite(new["start"]) and math.isfinite(new["end"])) or new["start"] < 0:
        raise ValueError("Those times aren't valid.")
    if not 3 <= total_dur <= 180:
        raise ValueError("A clip can be 3 to 180 seconds long.")
    for k in ("hook", "title", "description", "style", "layout"):
        if k in changes:
            new[k] = str(changes[k])[:{"hook": 60, "title": 100, "description": 2000}.get(k, 20)]
    if "hashtags" in changes:
        new["hashtags"] = clean_hashtags(changes["hashtags"])
    if new.get("style") not in render.STYLES:
        new["style"] = DEFAULTS["style"]
    if new.get("layout") not in render.LAYOUTS:
        new["layout"] = DEFAULTS["layout"]
    undo = {k: clip[k] for k in UNDO if k in clip}
    clip.update(new, status="queued", queued_at=time.time())
    clip["_prev"] = undo                    # what to go back to if the person cancels this re-make
    clip.pop("cancel", None)
    save(job)
    _q.put(("clip", jid, cid))


def clean_hashtags(v) -> list:
    """Hashtags typed by a person ("#funny gaming") or sent by the page as a list: plain words, at most 8."""
    if isinstance(v, str):
        v = v.split()
    if not isinstance(v, list):
        raise ValueError("Hashtags must be words separated by spaces.")
    tags = [re.sub(r"\W", "", str(h))[:30] for h in v[:50] if isinstance(h, (str, int))]
    return [t for t in tags if t][:8]


def save_post_text(job, clip, changes: dict):
    """Change a clip's title, description and hashtags without making the clip again. Posting reads them
    from the clip when it uploads, so the next post (or one still waiting in the queue) uses the new text."""
    text = {k: str(changes[k] or "")[:{"title": 100, "description": 2000}[k]] for k in ("title", "description") if k in changes}
    if "hashtags" in changes:
        text["hashtags"] = clean_hashtags(changes["hashtags"])
    if clip.get("hashtags_in_description") and not clip.get("fixed_description") and text:
        # This clip's description ends with its hashtags, and posting does not add them again. So the
        # description is put back together from the Hashtags box: the words, then one line of tags.
        desc = hashtags._TAG_IN_TEXT.sub("", text.get("description", clip.get("description") or "")).strip()
        line = " ".join("#" + t for t in text.get("hashtags", clip.get("hashtags") or []))
        text["description"] = ((desc + "\n\n" + line).strip() if desc else line)[:2000]
    with _lock:
        clip.update(text)
    save(job)
    side = Path(job["dir"]) / "clips" / f"{clip['id']}.json"   # the copy that sits next to the video file
    try:
        if side.is_file():
            side.write_text(json.dumps({**json.loads(side.read_text(encoding="utf-8")), **text}, indent=1), encoding="utf-8")
    except (OSError, ValueError):
        pass


def spoken_in(job, clip) -> str:
    """What is said during a clip, from the video's transcript (or, for a clip found by sight, its voice-over)."""
    if clip.get("voiceover"):
        return str(clip["voiceover"])
    try:
        words = json.loads((Path(job["dir"]) / "words.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return " ".join(str(w.get("w", "")) for w in words
                    if isinstance(w, dict) and clip["start"] - 0.5 <= _f(w.get("s")) <= clip["end"] + 0.5)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return -1.0


def render_one(job, clip, words):
    d = Path(job["dir"])
    if _cancelled(job, clip):
        return _drop_clip(job, clip)
    clip["status"] = "rendering"
    save(job)
    try:
        if not clip.get("src") and not job.get("source_path"):
            _piece_again(job, clip)
        off = clip.get("src_offset", 0.0)
        src = Path(clip["src"]) if clip.get("src") else Path(job["source_path"])
        if not clip.get("src"):
            clip["end"] = min(clip["end"], job["duration"])
        local = dict(clip, start=clip["start"] - off, end=clip["end"] - off, hook=roman.to_roman(clip.get("hook") or ""))
        vo = Path(clip["vo_file"]) if clip.get("vo_file") else None
        if clip.get("vo_words"):        # a voice-over: its words are already English and already timed, so no AI call
            near = [dict(w, s=w["s"] + local["start"], e=w["e"] + local["start"]) for w in clip["vo_words"]] if clip.get("captions", True) else []
        else:
            near = _caption_words(job, clip, words, off) if clip.get("captions", True) else []
        out = render.render_clip(src, d / "clips", clip["id"], local, near, job["width"], job["height"], voiceover=vo,
                                 cancel=lambda: _cancelled(job, clip))
        clip.update(out, status="done", version=clip.get("version", 0) + 1, error=None)
        clip.pop("_prev", None)
        post = {k: clip[k] for k in ("title", "description", "hashtags", "hook", "start", "end", "score")}
        (d / "clips" / f"{clip['id']}.json").write_text(json.dumps(post, indent=1), encoding="utf-8")
        if clip["version"] == 1:
            clip.setdefault("made_at", time.time())
            creators.log(job["creator"], f"Clip ready (score {clip['score']}): {clip['title'] or clip['hook']}")
            save(job)
            for fn in on_clip_ready:
                try:
                    fn(job, clip)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
    except Cancelled:
        return _drop_clip(job, clip)
    except Exception as e:  # noqa: BLE001
        if _cancelled(job, clip):           # it failed because it was stopped
            return _drop_clip(job, clip)
        clip.update(status="error", error=safe.friendly(e, 400))
        creators.log(job["creator"], f"Clip failed: {safe.friendly(e, 200)}")
    save(job)


def _caption_words(job, clip, words, off):
    cap_lang = (clip.get("caption_lang") or job.get("options", {}).get("caption_lang") or job.get("caption_lang") or "hinglish").lower().strip()
    if cap_lang not in ("hinglish", "hindi", "english"):
        cap_lang = "hinglish"
    span = [round(clip["start"], 2), round(clip["end"], 2), cap_lang]
    if clip.get("fixed_for") == span and clip.get("fixed_words") and (
            cap_lang == "hindi" or not any(roman.needs_roman(w.get("w", "")) for w in clip["fixed_words"])):
        raw = clip["fixed_words"]          # already tidied for this exact span: no second AI call
    else:
        raw = [w for w in words if clip["start"] - 1 <= w["s"] <= clip["end"] + 1]
        try:
            usage.set_context(job.get("creator"), job.get("title") or "")
            raw = brain.caption_words_for_lang(raw, cap_lang, job.get("lang") or "", job.get("title") or "",
                                               youtube_english=lambda: _english_part(job, clip))
            clip.update(fixed_words=raw, fixed_for=span)
        except Exception:  # noqa: BLE001 - captions still render with the words as heard
            pass
    if cap_lang == "hindi":
        return [dict(w, s=w["s"] - off, e=w["e"] - off) for w in raw]
    # the last word on it: whatever was said, nothing is drawn in Hindi or Urdu script
    return [dict(w, s=w["s"] - off, e=w["e"] - off) for w in roman.words(raw)]


CLIP_MINUTES = float(config.env("PEAKCUT_CLIP_MINUTES", "15"))     # the longest one clip may take to make


def _wait_for_clips(job, futures, began, workers):
    """Wait for a video's clips, but not for ever. A clip that hangs (a download that stalls, an encoder that
    freezes) used to keep the whole video "in progress", and no other clip could be re-made. Now it is marked as
    failed after CLIP_MINUTES and the video finishes with the clips that did work."""
    left, given_up = list(futures), 0
    late = "This clip took too long and was stopped. Open it and press Save and re-make to try again."
    while left:
        _check(job)
        still = []
        for f, clip in left:
            if f.done():
                f.result()
                continue
            t = began.get(clip["id"])
            if (t and time.time() - t > CLIP_MINUTES * 60) or (not t and given_up >= workers):
                given_up += 1
                clip.update(status="error", error=late)
                creators.log(job["creator"], f"Clip stopped after {CLIP_MINUTES:g} minutes: {clip.get('title') or clip.get('hook') or clip['id']}")
                save(job)
                continue
            still.append((f, clip))
        left = still
        if left:
            time.sleep(1)


def _english_part(job, clip):
    """YouTube's English captions for just this clip, for a video in another language. Fetched once per video."""
    if (job.get("source") or {}).get("type") != "url":
        return None
    f = Path(job["dir"]) / "words_en.json"
    if f.is_file():
        en = json.loads(f.read_text(encoding="utf-8"))
    else:
        try:
            en = media.youtube_english_words(job["source"]["url"]) or []
        except Exception:  # noqa: BLE001 - YouTube often refuses its translations: the AI translates instead
            en = []
        f.write_text(json.dumps(en), encoding="utf-8")       # asked once per video, refused or not
    return [w for w in en if clip["start"] - 0.5 <= w["s"] <= clip["end"] + 0.5]


LONG_VIDEO = 40 * 60   # above this, fetch only the pieces that become clips
PAD = 8                # seconds kept either side of a fetched piece, so clips can be re-trimmed


def _piece_again(job, clip):
    """Remaking a clip whose own piece of the video was never fetched (its first try failed) and whose whole video
    is not here: fetch the piece now, cut exactly at the asked time, so no sound matching is needed."""
    src = job.get("source") or {}
    if src.get("type") != "url" or not src.get("url"):
        raise RuntimeError("The original video is no longer here, so this clip can't be made again. Add the video again.")
    d = Path(job["dir"])
    want = max(0.0, clip["start"] - PAD)
    until = clip["end"] + PAD if not job.get("duration") else min(job["duration"], clip["end"] + PAD)
    try:
        piece = media.download_section(src["url"], d / "clips", f"{clip['id']}.src", want, until,
                                       int(job["options"]["quality"]), exact=True)
        meta = media.probe(piece)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Could not fetch this part of the video: {safe.friendly(e, 250)}") from e
    clip.update(src=str(piece), src_offset=round(want, 2), src_end=round(want + meta["duration"], 2))
    clip["end"] = min(clip["end"], clip["src_end"])
    job.setdefault("width", meta["width"])
    job.setdefault("height", meta["height"])


def _fetch_piece(job, clip, full_audio):
    """Long videos: download just this clip's stretch and pin down exactly where it starts."""
    d = Path(job["dir"])
    o = job["options"]
    try:
        want = max(0.0, clip["start"] - PAD)
        piece = media.download_section(job["source"]["url"], d / "clips", f"{clip['id']}.src", want,
                                       min(job["duration"], clip["end"] + PAD), int(o["quality"]))
        wav = d / "clips" / f"{clip['id']}.wav"
        media.extract_audio(piece, wav)
        part = media.load_audio(wav)
        wav.unlink(missing_ok=True)
        meta = media.probe(piece)
        off = media.find_offset(full_audio, part, want)
        clip.update(src=str(piece), src_offset=round(off, 2), src_end=round(off + meta["duration"], 2))
        clip["start"], clip["end"] = max(clip["start"], clip["src_offset"]), min(clip["end"], clip["src_end"])
        job.setdefault("width", meta["width"])
        job.setdefault("height", meta["height"])
    except Exception as e:  # noqa: BLE001
        clip.update(status="error", error=f"Could not fetch this part of the video: {safe.friendly(e, 250)}")


BLOCK = 10 * 60         # long videos are transcribed and clipped one block at a time
PROGRESSIVE_FROM = 20 * 60


def _covers(words: list, start: float, end: float) -> bool:
    """Is a borrowed transcript good enough to find clips with? It has to reach the end of the part being clipped
    and have a normal amount of talking in it; otherwise our own model writes the transcript."""
    inside = [w for w in words if start <= w["s"] <= end]
    minutes = max(0.5, (end - start) / 60)
    return len(inside) >= 25 * minutes and inside[-1]["s"] >= start + 0.85 * (end - start)


def _facecam(job) -> list | None:
    """Where the webcam is in the source: once for the whole video, for every clip of it.

    Only a sure answer is used. A box that is a scoreboard drawn over the game would
    put a picture of a scoreboard in the corner of the clip. One frame is also shown to
    the AI, which tells a person from a game character better than any measurement can;
    what it says is measured again on the picture, so it cannot do any harm."""
    path = job.get("source_path")
    if not path or not Path(path).is_file():
        return None
    try:
        box = facecam.detect(path, ask=brain.look_for_facecam)
    except Exception:  # noqa: BLE001 - a missing facecam is not a failure
        return None
    if not box or box.get("conf") != "high":
        return None
    job["facecam"] = [box["x"], box["y"], box["w"], box["h"]]
    return job["facecam"]


def _run_job(job):
    import math
    from concurrent.futures import ThreadPoolExecutor
    d = Path(job["dir"])
    o = job["options"]
    src = job["source"]
    _check(job)
    if o.get("vision"):
        return _run_vision_job(job)
    # Which game this is: what the creator set, or what the title says. It decides the extra rules the AI
    # reads and the tags every post carries.
    game = job.get("game") or ""
    if not game and job.get("creator"):
        try:
            game = (creators.get(job["creator"]) or {}).get("game") or ""
        except Exception:  # noqa: BLE001
            game = ""
    if not game:
        game = games.detect(job["title"], src.get("name"))[0] or ""
    if game and job.get("game") != game:
        job["game"] = game
    t0 = time.time()
    notes = []
    face = False       # where the webcam is in the source, found once; False means not looked for yet
    usage.set_context(job["creator"], job["title"] or src.get("name"))
    resuming = bool(job.pop("resume", False)) and (d / "words.json").is_file()
    if resuming:   # after a restart: keep the transcript parts, clips and AI answers already made, and carry on
        _set(job, status="running", stage="Picking up where it stopped", error=None)
    else:
        _set(job, status="running", stage="Getting the audio", progress=0.03, error=None, clips=[], ai={}, done_blocks=0)
    speech = o.get("lang") if fetch_model.known(o.get("lang")) else fetch_model.DEFAULT   # the language spoken
    if speech == fetch_model.DEFAULT and not transcribe.model_ready() and not transcribe.REMOTE:
        _set(job, stage="Getting the speech model (one time, about 480 MB)")
        fetch_model.ensure()
        _set(job, stage="Getting the audio")
    could_borrow = src["type"] == "url" and config.YT_TRANSCRIPT and media.is_youtube(src["url"])
    if speech != fetch_model.DEFAULT and not transcribe.REMOTE and not could_borrow and fetch_model.status(speech) == "missing":
        fetch_model.ensure(speech, start=False)      # says what to do, before any time is spent on the download
    fetch = ThreadPoolExecutor(1)
    video = None
    if src["type"] == "url":
        meta = media.info(src["url"])
        if meta["kind"] != "video":
            raise RuntimeError("That link is a channel or playlist, not a single video. Paste the link of one video.")
        if ((o["range_end"] or meta["duration"]) - (o["range_start"] or 0)) > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That is longer than {config.MAX_VIDEO_HOURS:g} hours. Choose a shorter part of it under Options.")
        if meta["is_live"]:
            raise RuntimeError("This link is a live stream, so it would never finish downloading. Open the creator, "
                               "put this link under Setup, and press Start recording instead.")
        _set(job, title=meta["title"], uploader=meta["uploader"], duration=meta["duration"])
        if not job.get("thumb") and media.save_thumb(d, url=src["url"]):
            _set(job, thumb=True)
        usage.set_context(job["creator"], meta["title"])
        span = (o["range_end"] or meta["duration"]) - (o["range_start"] or 0)
        pieces = span > LONG_VIDEO or meta["duration"] <= 0
        if not pieces:  # shorter video: fetch all of it in the background while the audio is worked on
            video = fetch.submit(media.download, src["url"], d, int(o["quality"]), cancel=lambda: _cancelled(job))
        seen = [-1.0]

        def got(p):                       # the bar moves in whole percents: no need to save the file a hundred times a second
            if p - seen[0] >= 0.01 or p >= 1:
                seen[0] = p
                _set(job, progress=0.03 + 0.1 * p)

        audio_file = media.download_audio(src["url"], d, got, cancel=lambda: _cancelled(job))
    else:
        pieces, audio_file = False, Path(src["path"])
        info = media.probe(audio_file)
        if info["duration"] > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That video is longer than {config.MAX_VIDEO_HOURS:g} hours, which is the limit.")
        _set(job, source_path=str(audio_file), width=info["width"], height=info["height"], duration=info["duration"])
        if not job.get("thumb") and media.save_thumb(d, video=audio_file):
            _set(job, thumb=True)
    wav = d / "audio.wav"
    media.extract_audio(audio_file, wav)
    audio = media.load_audio(wav)
    wav.unlink(missing_ok=True)
    if src["type"] == "url":
        audio_file.unlink(missing_ok=True)
    if not job["duration"]:
        job["duration"] = len(audio) / 16000
    lo = int((o["range_start"] or 0) * 16000)
    hi = min(len(audio), int(o["range_end"] * 16000)) if o["range_end"] else len(audio)
    span = (hi - lo) / 16000
    db = media.loudness_per_second(audio[lo:hi])

    vlock, vstate = threading.Lock(), {}

    def whole_video():
        """Wait for the background download once; retry it, or give up and use pieces."""
        with vlock:
            if "path" not in vstate:
                path = None
                try:
                    path = video.result()
                except Exception as e:  # noqa: BLE001 - sites sometimes refuse a download
                    notes.append(f"Video download refused once ({str(e)[-80:]}), retrying")
                    try:
                        path = media.download(src["url"], d, int(o["quality"]))
                    except Exception as e2:  # noqa: BLE001
                        notes.append(f"Whole-video download failed ({str(e2)[-80:]}), fetching clip parts instead")
                if path:
                    info = media.probe(path)
                    job.update(source_path=str(path), width=info["width"], height=info["height"])
                vstate["path"] = path
            return vstate["path"]

    exact = {}      # clip id -> (from, to, words): our own model's words for just that clip

    began = {}      # clip id -> when work on it started

    def make(clip, words_now):
        if _cancelled(job, clip):
            return _drop_clip(job, clip)
        began[clip["id"]] = time.time()
        if src["type"] == "url" and (pieces or not whole_video()):
            _fetch_piece(job, clip, audio)
        if clip["status"] != "error" and clip.get("captions", True) and yt_model:
            # The clip was found with YouTube's transcript. What is shown on screen comes from our own model,
            # run on just this clip: full punctuation, nothing blanked out, timings to the word.
            a0, b0 = max(lo, int((clip["start"] - 1.5) * 16000)), min(hi, int((clip["end"] + 1.5) * 16000))
            try:
                tw = [dict(w, s=round(w["s"] + a0 / 16000, 2), e=round(w["e"] + a0 / 16000, 2))
                      for w in transcribe.transcribe(audio[a0:b0], lang=yt_model)]
            except Exception:  # noqa: BLE001 - the clip is still made, with YouTube's words
                tw = []
            if len(tw) >= 3:
                exact[clip["id"]] = (a0 / 16000, b0 / 16000, tw)
                words_now = tw
        if clip["status"] != "error":
            render_one(job, clip, words_now)

    cpus = os.cpu_count() or 4
    renders = ThreadPoolExecutor(1 if cpus <= 4 else 2 if cpus < 8 else min(6, cpus // 6))  # more clips made at once on a bigger machine
    futures = []
    step = BLOCK * 16000 if span > PROGRESSIVE_FROM else hi - lo
    blocks = [(x, min(hi, x + step)) for x in range(lo, hi, step)] or [(lo, hi)]
    words, tokens, model = [], 0, None
    done_blocks = 0
    yt = None
    if src["type"] == "url" and config.YT_TRANSCRIPT and media.is_youtube(src["url"]):
        try:
            yt = media.youtube_words(src["url"])
        except Exception:  # noqa: BLE001 - no transcript to borrow: ours is written as usual
            yt = None
        if yt and not _covers(yt[0], lo / 16000, hi / 16000):
            yt = None
        job["transcript_from"] = "youtube" if yt else "own"
    # The language spoken decides the captions: YouTube's word for it when its transcript is used, else the choice made.
    job["lang"] = yt[1] if yt else speech
    # A clip found with YouTube's transcript gets its on-screen words from our own model, when there is one here
    # for the language YouTube says was spoken (English always; others once they are downloaded).
    yt_code = (yt[1].lower().split("-")[0] if yt else "")
    yt_model = yt_code if fetch_model.known(yt_code) and (yt_code == fetch_model.DEFAULT or fetch_model.ready(yt_code)) else ""
    if not yt and speech != fetch_model.DEFAULT and not transcribe.REMOTE and not fetch_model.ready(speech):
        if fetch_model.state[speech]["doing"]:
            _set(job, stage=f"Waiting for the {fetch_model.label(speech)} speech model to finish downloading")
        fetch_model.ensure(speech, start=False)      # waits for a download already running, else says what to do
    if resuming:
        try:
            words = json.loads((d / "words.json").read_text(encoding="utf-8"))
        except ValueError:
            words = []
        done_blocks = min(int(job.get("done_blocks", 0)), len(blocks)) if words else 0
        tokens, model = int((job.get("ai") or {}).get("tokens", 0)), (job.get("ai") or {}).get("model")
        if done_blocks < len(blocks):     # words past the last finished part are transcribed again
            cut = blocks[done_blocks][0] / 16000
            words = [w for w in words if w["s"] < cut]
        for c in job["clips"]:            # clips found before the restart that were not finished yet
            if c.get("status") not in ("done", "error"):
                c["status"] = "queued"
                futures.append((renders.submit(ctx.bind(make), c, list(words)), c))
        save(job)
    # The transcript of the next part is written while the AI is still reading this one (the AI is a wait on the
    # network, the transcript is work on this machine), so neither waits for the other.
    ahead = ThreadPoolExecutor(2, thread_name_prefix="ahead")
    heard, now_on = {}, [done_blocks]

    def hear(i):
        if i < len(blocks) and i >= done_blocks and i not in heard:
            a_, b_ = blocks[i]
            last = [0.0]

            def tp(p, i=i, a_=a_, b_=b_):
                _check(job)
                if now_on[0] == i and p - last[0] > 0.05:      # only the part being shown moves the bar
                    last[0] = p
                    _set(job, progress=max(job["progress"], 0.14 + 0.78 * ((a_ - lo) + p * (b_ - a_)) / max(1, hi - lo)))
            heard[i] = ahead.submit(ctx.bind(transcribe.transcribe), audio[a_:b_], tp, speech)

    # The ad breaks and static screens of the whole video, found once. It reads the video end to end,
    # so it is done here rather than for each clip, and only when asked for.
    statics = quality.static_ranges(job.get("source_path") or "") if config.SKIP_STATIC else []
    for bi, (a, b) in enumerate(blocks):
        _check(job)
        if bi < done_blocks:
            continue                      # transcribed and checked by the AI before the restart
        shift = a / 16000
        where = f" (minute {round((a - lo) / 16000 / 60)} of {round(span / 60)})" if len(blocks) > 1 else ""
        if yt:
            _set(job, stage="Reading the transcript" + where)
            bw = [dict(w, s=round(w["s"] - shift, 2), e=round(w["e"] - shift, 2)) for w in yt[0] if a / 16000 <= w["s"] < b / 16000]
        else:
            _set(job, stage="Writing the transcript" + where)
            now_on[0] = bi
            hear(bi)
            hear(bi + 1)
            bw = heard.pop(bi).result()
        _set(job, progress=max(job["progress"], 0.14 + 0.78 * (b - lo) / max(1, hi - lo)))
        words += [dict(w, s=round(w["s"] + shift, 2), e=round(w["e"] + shift, 2)) for w in bw]
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
        _set(job, stage="Finding the best moments" + where, word_count=len(words))
        left = int(o["max_clips"]) - len(job["clips"])
        if left <= 0 or len(bw) < 8:
            job["done_blocks"] = bi + 1
            save(job)
            continue
        quota = max(1, math.ceil(left / (len(blocks) - bi)))
        s0 = int((a - lo) / 16000)
        found, ai = brain.find_clips(bw, db[s0: s0 + int((b - a) / 16000) + 1], (b - a) / 16000, job["title"],
                                     job["uploader"], quota, int(o["min_len"]), int(o["max_len"]), notes.append, game=game)
        _check(job)
        tokens += ai.get("tokens", 0)
        model = ai.get("model") or model
        if ai.get("note"):
            notes.append(ai["note"])
        if found and statics:
            # A commercial or a "be right back" slate is not a clip. Any clip that sits mostly inside one
            # is dropped.
            keep = []
            for c in found:
                a_s, b_s = c["start"] + shift, c["end"] + shift
                inside = quality.static_overlap(a_s, b_s, statics)
                if inside > 0.5 * max(1.0, b_s - a_s):
                    notes.append(f"A clip from {round(a_s)}s was left out: it sat in an ad break or a static screen.")
                else:
                    keep.append(c)
            found = keep
        for c in found:
            c.update(id=f"clip{len(job['clips']) + 1:02d}", start=round(c["start"] + shift, 2),
                     end=round(c["end"] + shift, 2), style=o["style"], layout=o["layout"],
                     captions=o.get("captions", True), big_text=o.get("big_text", True), status="queued", version=0, queued_at=time.time())
            if o.get("hook_text"):        # the person's own big text, on every clip, in place of the AI's
                c["hook"] = o["hook_text"]
            if o.get("description_text"):  # ...and their own description, exactly as written
                c["description"], c["fixed_description"] = o["description_text"], True
            else:                          # no description of their own: 3 to 5 fitting hashtags go at the end of the AI's
                hashtags.apply(c, words, job["title"], job["uploader"], game)
            # Only the gameplay layout uses a webcam, so it is only looked for then, and only once: a
            # stream with no webcam is not looked for again on every clip of it.
            if o["layout"] == "gameplay" and face is False:
                face = _facecam(job)
            if face:
                c["facecam"] = face
            job["clips"].append(c)
            futures.append((renders.submit(ctx.bind(make), c, list(words)), c))
        job["ai"] = {"model": model, "tokens": tokens, "note": "", "log": notes[-5:]}
        job["done_blocks"] = bi + 1       # a restart from here on carries on with the next part
        save(job)
    if futures:
        _set(job, stage="Finishing the clips", progress=max(job["progress"], 0.92))
    _wait_for_clips(job, futures, began, renders._max_workers)
    if exact:           # keep our own words for the clips in the saved transcript, so editing a clip later uses them
        for a_s, b_s, tw in exact.values():
            words = [w for w in words if not a_s <= w["s"] < b_s] + tw
        words.sort(key=lambda w: w["s"])
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
    renders.shutdown(wait=False)
    ahead.shutdown(wait=False)
    fetch.shutdown(wait=False)
    del audio
    clips = job["clips"]
    job["ai"] = {"model": model, "tokens": tokens, "log": notes[-5:],
                 "note": "The AI was unavailable, so clips were picked by loudness only." if clips and not model else ""}
    stage = "Done" if clips else "Done, but nothing clip-worthy was found"
    _set(job, status="done", stage=stage, progress=1.0, took=round(time.time() - t0))
    try:
        from . import notices
        name = job.get("title") or job["source"].get("name") or "your video"
        notices.add(job.get("user"), "clips", f"{len(clips)} clip{'s' if len(clips) != 1 else ''} ready" if clips else "Nothing clip-worthy found",
                    name, f"/app/creators/{job['creator']}" if job.get("creator") else "/app/videos")
    except Exception:  # noqa: BLE001
        pass
    creators.log(job["creator"], f"Video finished with {len(clips)} clips in {round(time.time() - t0)}s: {job['title']}")


def _voice_over(job, clip, sid):
    """Read a clip's voice-over aloud into a sound file beside the clip, and keep its word times for the captions."""
    out = Path(job["dir"]) / "clips" / f"{clip['id']}.vo.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    res = tts.narrate(clip["voiceover"], sid, out, max_seconds=max(1.0, clip["end"] - clip["start"]))
    clip.update(vo_file=str(out), vo_words=res["words"], vo_seconds=res["seconds"])


def _run_vision_job(job):
    """Finding clips by what is on screen: no transcript. The video is chopped into parts; each part is sampled
    into a few small frames here, and one request per part asks the AI for the best moments (and, when asked, the
    words of a voice-over). A voice-over is read by the local Kokoro voice and laid over the clip."""
    import math
    from concurrent.futures import ThreadPoolExecutor
    d = Path(job["dir"])
    o = job["options"]
    src = job["source"]
    t0, notes = time.time(), []
    _check(job)
    usage.set_context(job["creator"], job["title"] or src.get("name"))
    path = Path(job["source_path"]) if job.get("source_path") else None
    resuming = bool(job.pop("resume", False)) and bool(path and path.is_file())
    if resuming:
        _set(job, status="running", stage="Picking up where it stopped", error=None)
    else:
        _set(job, status="running", stage="Getting the video", progress=0.03, error=None, clips=[], ai={}, done_blocks=0)
    if not resuming:
        if src["type"] == "url":
            meta = media.info(src["url"])
            if meta["kind"] != "video":
                raise RuntimeError("That link is a channel or playlist, not a single video. Paste the link of one video.")
            if meta["is_live"]:
                raise RuntimeError("This link is a live stream, so it would never finish downloading. Open the creator, "
                                   "put this link under Setup, and press Start recording instead.")
            if ((o["range_end"] or meta["duration"]) - (o["range_start"] or 0)) > config.MAX_VIDEO_HOURS * 3600:
                raise RuntimeError(f"That is longer than {config.MAX_VIDEO_HOURS:g} hours. Choose a shorter part of it under Options.")
            _set(job, title=meta["title"], uploader=meta["uploader"], duration=meta["duration"])
            if not job.get("thumb") and media.save_thumb(d, url=src["url"]):
                _set(job, thumb=True)
            usage.set_context(job["creator"], meta["title"])
            path = media.download(src["url"], d, int(o["quality"]), cancel=lambda: _cancelled(job))
            _check(job)
        else:
            path = Path(src["path"])
        info = media.probe(path, require_audio=False)
        if info["duration"] > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That video is longer than {config.MAX_VIDEO_HOURS:g} hours, which is the limit.")
        _set(job, source_path=str(path), width=info["width"], height=info["height"], duration=info["duration"], has_audio=info["audio"])
        if not job.get("thumb") and media.save_thumb(d, video=path):
            _set(job, thumb=True)
    lo = float(o["range_start"] or 0)
    hi = min(float(job["duration"]), float(o["range_end"] or job["duration"]))
    if hi - lo < 5:
        raise RuntimeError("There is less than five seconds of video to look at.")
    game = job.get("game") or games.detect(job["title"], src.get("name"))[0] or ""
    prof = None
    if o.get("reference"):
        prof = references.profile(o["reference"])
        if not prof:
            notes.append("The reference was removed or is not ready, so the clips were found without it.")
    voice = bool(o.get("voiceover"))
    sid = int(o.get("voice", tts.DEFAULT_VOICE))
    parts = vision.split(lo, hi, o.get("parts"))
    done = min(int(job.get("done_blocks") or 0), len(parts)) if resuming else 0
    ai_ = job.get("ai") or {}
    tokens, model = int(ai_.get("tokens", 0)), ai_.get("model")
    cpus = os.cpu_count() or 4
    renders = ThreadPoolExecutor(1 if cpus <= 4 else 2 if cpus < 8 else min(6, cpus // 6))
    futures, began = [], {}

    def make(clip):
        if _cancelled(job, clip):
            return _drop_clip(job, clip)
        began[clip["id"]] = time.time()
        try:
            if voice and clip.get("voiceover") and not clip.get("vo_file"):
                _voice_over(job, clip, sid)
        except Exception as e:  # noqa: BLE001 - say what is wrong; Save and re-make tries again once it is fixed
            clip.update(status="error", error=f"The voice-over could not be made: {safe.friendly(e, 250)}")
            save(job)
            return
        render_one(job, clip, [])

    for c in job["clips"]:            # clips found before a restart that were not finished
        if c.get("status") not in ("done", "error"):
            c["status"] = "queued"
            futures.append((renders.submit(ctx.bind(make), c), c))
    ok_parts, last_error = done, ""
    for bi in range(done, len(parts)):
        _check(job)
        a, b = parts[bi]
        where = f" (part {bi + 1} of {len(parts)})" if len(parts) > 1 else ""
        left = int(o["max_clips"]) - len(job["clips"])
        if left <= 0:
            break
        _set(job, stage="Taking frames" + where, progress=max(job["progress"], 0.08 + 0.84 * bi / len(parts)))
        step = vision.step_for(b - a, o.get("detail"))
        work = d / "frames"
        shutil.rmtree(work, ignore_errors=True)
        try:
            frames, cuts = vision.sample(path, a, b, step, work, cancel=lambda: _cancelled(job))
            if not frames:
                raise RuntimeError("No frames could be taken from this part.")
            _set(job, stage="Asking the AI what to clip" + where)
            found, m, spent = vision.find_part(frames, cuts, a, b, float(job["duration"]), max(1, math.ceil(left / (len(parts) - bi))),
                                               int(o["min_len"]), int(o["max_len"]), prof, o.get("ref_prompt", ""), voice, step)
            tokens, model = tokens + spent, m or model
            ok_parts += 1
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - one part failing does not lose the others
            last_error = safe.friendly(e, 300)
            notes.append(f"Part {bi + 1} failed: {last_error}")
            found = []
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _check(job)
        for c in found:
            if brain._overlaps(c["start"], c["end"], job["clips"]):
                continue
            c.update(id=f"clip{len(job['clips']) + 1:02d}", style=o["style"], layout=o["layout"], captions=o.get("captions", True),
                     big_text=o.get("big_text", True), status="queued", version=0, queued_at=time.time())
            if o.get("hook_text"):
                c["hook"] = o["hook_text"]
            if o.get("description_text"):
                c["description"], c["fixed_description"] = o["description_text"], True
            else:
                if voice and c.get("voiceover"):
                    c["description"] = c["voiceover"][:300]
                hashtags.apply(c, [], job["title"], job["uploader"], game)
            job["clips"].append(c)
            futures.append((renders.submit(ctx.bind(make), c), c))
        job["ai"] = {"model": model, "tokens": tokens, "note": "", "log": notes[-5:]}
        job["done_blocks"] = bi + 1
        save(job)
    if ok_parts == done and done < len(parts) and not job["clips"]:
        raise RuntimeError(last_error or "The AI did not answer.")
    if futures:
        _set(job, stage="Finishing the clips", progress=max(job["progress"], 0.92))
    _wait_for_clips(job, futures, began, renders._max_workers)
    renders.shutdown(wait=False)
    clips = job["clips"]
    job["ai"] = {"model": model, "tokens": tokens, "log": notes[-5:], "note": ""}
    _set(job, status="done", stage="Done" if clips else "Done, but nothing clip-worthy was found", progress=1.0, took=round(time.time() - t0))
    try:
        from . import notices
        name = job.get("title") or job["source"].get("name") or "your video"
        notices.add(job.get("user"), "clips", f"{len(clips)} clip{'s' if len(clips) != 1 else ''} ready" if clips else "Nothing clip-worthy found",
                    name, f"/app/creators/{job['creator']}" if job.get("creator") else "/app/videos")
    except Exception:  # noqa: BLE001
        pass
    creators.log(job["creator"], f"Video finished with {len(clips)} clips by sight in {round(time.time() - t0)}s: {job['title']}")


def _worker(quick_only=False):
    while True:
        kind, a, cid = _q.get(quick_only)
        if kind == "fn":
            try:
                a()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            continue
        job = _jobs.get(a)
        if not job:
            if kind == "job":
                _q.done()
            continue
        if kind == "job" and job.get("cancelled_at") and job.get("status") == "done":
            _q.done()                      # cancelled while it waited its turn
            continue
        token = ctx._uid.set(job["user"])  # everything this job does runs as its owner
        try:
            if kind == "job":
                _run_job(job)
            else:
                clip = next(c for c in job["clips"] if c["id"] == cid)
                words = []
                if clip.get("captions", True) and not clip.get("vo_words"):
                    words = json.loads((Path(job["dir"]) / "words.json").read_text(encoding="utf-8"))
                render_one(job, clip, words)
        except Cancelled:
            if kind == "job":
                _finish_cancel(job)
        except Exception as e:  # noqa: BLE001
            (Path(job["dir"]) / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            if kind == "job":
                for c in job["clips"]:
                    if c.get("status") in ("queued", "rendering"):
                        c.update(status="error", error="The video stopped before this clip was made.")
                _set(job, status="error", error=safe.friendly(e), stage="Stopped")
                creators.log(job["creator"], f"Video failed: {safe.friendly(e)[:200]}")
        finally:
            ctx._uid.reset(token)
            if kind == "job":
                _q.done()


def _pick_up_after_restart():
    """Videos and single clips that were being made when the server stopped: queued again, oldest first."""
    waiting = sorted((j for j in all_jobs() if j.get("status") == "queued" and j.get("resume")), key=lambda j: j["created"])
    for j in waiting:
        _q.put(("job", j["id"], None), user=j["user"])
    for j in all_jobs():
        for cid in j.pop("remake", []):
            _q.put(("clip", j["id"], cid))
        if j.get("status") == "done":
            save(j)


def start():
    load.start()
    load_all()
    _pick_up_after_restart()
    for _ in range(max(1, config.JOB_WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_worker, args=(True,), daemon=True).start()   # re-making a single clip never waits behind whole videos
    threading.Thread(target=_live_worker, daemon=True).start()
