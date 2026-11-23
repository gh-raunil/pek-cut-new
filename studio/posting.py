"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import secrets
import threading
import time
from pathlib import Path

import json

from . import accounts, alerts, config, creators, ctx, jobs, live

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def forget(uid: str):
    """Drop everything queued or recorded for a deleted account, so nothing is posted or retried for it any more."""
    with _lock:
        for p in _posts:
            if p.get("user") == uid:
                p["status"] = "deleted"          # the posting loop may be holding this one already: it then skips it
        _posts[:] = [p for p in _posts if p.get("user") != uid]
        _save()


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _when(p):
    """When the post is (or goes) public: YouTube's scheduled time for one uploaded ahead, else when it was posted
    or is due. Slots and daily counts are about this, not about when the file was sent."""
    return p.get("publish_at") or p.get("posted_at") or p["due_at"]


AHEAD = 20 * 3600       # uploads for YouTube's own scheduling go out up to this long before their time
_blocked: dict = {}     # "yt" (the server's Google project) or an account id -> no YouTube uploads until then


def _after_limit_reset():
    """YouTube's daily limits reset at midnight Pacific time, which is 07:00 or 08:00 UTC. A few minutes past 08:00
    UTC is after the reset all year, without needing time zone data."""
    now = time.time()
    t = (now // 86400) * 86400 + 8 * 3600 + 300
    return t if t > now + 60 else t + 86400


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "publish_at", "error")}
        jobs.save(job)


def _slots(times, start, days=400):
    """Every upload time from `start` on, soonest first, as seconds. Built with mktime(..., -1) so a clock change
    (daylight saving) moves nothing by an hour."""
    lt = time.localtime(start)
    for d in range(days + 1):
        for hhmm in times:
            h, m = int(hhmm[:2]), int(hhmm[3:])
            t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + d, h, m, 0, 0, 0, -1))
            if t > start:
                yield t


def _next_slot(account_id, gap_min, cap, exclude=(), start=None):
    """Earliest time that respects the gap between posts and the daily cap for this account. With an upload
    schedule (the channel's own, or the default one), that is the next scheduled time nobody else on this account
    already holds. A channel's own schedule is its daily limit too: 8 times means 8 a day."""
    # failed and cancelled posts hold nothing; `exclude` holds the ids of posts being moved, which must not collide with themselves
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",) and p["id"] not in exclude]
    times, own = accounts.schedule_for(account_id)
    if times:
        if own:
            cap = len(times)
        taken = {int(_when(p) // 60) for p in mine}
        per_day: dict = {}
        for p in mine:
            per_day[_day(_when(p))] = per_day.get(_day(_when(p)), 0) + 1
        for t in _slots(times, start or time.time()):
            if int(t // 60) not in taken and per_day.get(_day(t), 0) < cap:
                return t
    t = max(time.time(), max((_when(p) for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(_when(p)) == _day(t)) < cap:
            break
        lt = time.localtime(t + 86400)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day
    return t


def _place(p, exclude=()):
    """Give a waiting post its time. On a YouTube channel that uploads ahead, the slot becomes `publish_at`
    (YouTube makes the video public then) and the upload itself goes out up to a day before it."""
    c = creators.get(p["creator"]) or creators.DEFAULTS
    times, _own = accounts.schedule_for(p["account"])
    ahead = bool(times) and accounts.uploads_ahead(p["account"]) and c["privacy"] == "public"
    # YouTube needs a scheduled time comfortably ahead of the upload: a slot minutes away is left for the next one
    slot = _next_slot(p["account"], c["post_gap_min"], c["daily_cap"], exclude, time.time() + 600 if ahead else None)
    if ahead:
        p.update(publish_at=slot, due_at=max(time.time(), slot - AHEAD))
    else:
        p.pop("publish_at", None)
        p["due_at"] = slot


def _add(job, clip, aid, now=False, repost=False):
    """One new post in the queue (inside _lock), or None when this clip already went, or is going, to that account."""
    acc = accounts.get(aid)
    if not acc:
        return None
    if not repost and any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
           and p["status"] in OPEN + ("posted",) for p in _mine()):
        return None  # never post the same clip to the same account twice unless repost is confirmed
    p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
         "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
         "due_at": time.time(), "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
         "score": clip.get("score", 0), "manual": bool(now), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None}
    if not now:
        _place(p)
    _posts.append(p)
    return p


def enqueue(job, clip, account_ids, now=False, repost=False) -> list:
    with _lock:
        made = [p for p in (_add(job, clip, aid, now, repost=repost) for aid in account_ids) if p]
        _save()
    for p in made:
        _mirror(p)
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator):
    # A description the person wrote themselves is posted as it is, without the AI's hashtags added to it, and
    # one that already ends with its hashtags does not get them twice.
    tags = "" if clip.get("fixed_description") or clip.get("hashtags_in_description") else " ".join("#" + h for h in clip.get("hashtags", []))
    return "\n\n".join(x for x in (clip.get("description", ""), tags, creator.get("post_footer", "")) if x)


def _post(p):
    if p.get("publish_at") and not p.get("manual") and p["publish_at"] <= time.time() + 300:
        # its time slipped by while it waited (YouTube's daily limit, a retry): take the channel's next free time
        # instead of going public off schedule. Post now and Retry still upload straight away.
        with _lock:
            _place(p, {p["id"]})
            p["status"] = "scheduled"
        _save()
        _mirror(p)
        return
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)
        if acc["platform"] == "youtube":
            # YouTube's scheduling needs a time still ahead; an upload that ran late is simply posted now
            at = p.get("publish_at") if (p.get("publish_at") or 0) > time.time() + 300 else None
            if not at:
                p.pop("publish_at", None)
            rid, url = accounts.youtube_upload(acc, path, title, cap + "\n\n#Shorts", clip.get("hashtags", []), creator["privacy"], at)
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Uploaded to {acc['name']}, goes live {time.strftime('%d %b %H:%M', time.localtime(p['publish_at']))}: {title}"
                     if p.get("publish_at") else f"Posted to {acc['name']}: {title}")
    except accounts.LimitReached as e:
        # not a failure: wait for the limit to reset, without using up a try
        until = _after_limit_reset()
        _blocked[p["account"] if e.channel else "yt"] = until
        p.update(status="scheduled", due_at=until, error=f"{e} Waiting until it resets (midnight Pacific time), then it goes by itself.")
    except Exception as e:  # noqa: BLE001
        p["tries"] += 1
        p["error"] = str(e)[-300:]
        if p["tries"] < 3 and clip and acc:
            p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def _run():
    while True:
        time.sleep(15)
        now = time.time()
        due = [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= now and p.get("user")
               and not (p["platform"] == "youtube" and max(_blocked.get("yt", 0), _blocked.get(p["account"], 0)) > now)]
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None, manual=True)
        _save()
    _mirror(p)


def retry_failed() -> int:
    """The manual tap for the whole failed list: everything that failed goes out now."""
    with _lock:
        failed = [p for p in _mine() if p["status"] == "failed"]
        for p in failed:
            p.update(status="scheduled", due_at=time.time(), tries=0, error=None, manual=True)
        _save()
    for p in failed:
        _mirror(p)
    return len(failed)


def dismiss(pid):
    """Forget a failed post for good (the clip itself stays)."""
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "failed":
            raise ValueError("Only failed posts can be removed.")
        _posts.remove(p)
        _save()
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None and (clip.get("posts") or {}).pop(p["id"], None) is not None:
        jobs.save(job)


def queue_video(job, account_ids, repost=False) -> int:
    """Put every finished clip of a video in the queue, each in the next free upload slot, best clip first."""
    clips = sorted((c for c in job["clips"] if c.get("status") == "done"), key=lambda c: -c.get("score", 0))
    return sum(len(enqueue(job, c, account_ids, repost=repost)) for c in clips)


def plan(account_ids, creator_id=None, min_score=0, dry=False) -> dict:
    """The one-click upload: every finished clip that has not gone to a channel yet goes into that channel's next
    free upload times, best clip first. Each channel takes the clips of the creators that post to it (or, with
    `creator_id`, only that creator's). With `dry`, nothing is kept: the answer says what would happen."""
    cs = {c["id"]: c for c in creators.all_creators()}
    if creator_id and creator_id not in cs:
        raise ValueError("No such creator.")
    known = {a["id"] for a in accounts.list_accounts()}
    account_ids = [a for a in dict.fromkeys(account_ids) if a in known]
    if creator_id:
        account_ids = account_ids or [a for a in cs[creator_id]["accounts"] if a in known]
    if not account_ids:
        raise ValueError("Pick at least one channel.")
    all_js = jobs.list_jobs()
    out, made = [], []
    with _lock:
        for aid in account_ids:
            feeds = [creator_id] if creator_id else [cid for cid, c in cs.items() if aid in c["accounts"]]
            ready = sorted(((j, k) for j in all_js if j["creator"] in feeds for k in j["clips"]
                            if k.get("status") == "done" and k.get("score", 0) >= min_score),
                           key=lambda x: (-x[1].get("score", 0), x[1].get("made_at") or x[0]["created"]))
            mine = [p for p in (_add(j, k, aid) for j, k in ready) if p]
            made += mine
            name = next((a["name"] for a in accounts.list_accounts() if a["id"] == aid), aid)
            out.append({"id": aid, "name": name, "queued": len(mine), "feeds": [cs[x]["name"] for x in feeds if x in cs],
                        "first": min((_when(p) for p in mine), default=None), "last": max((_when(p) for p in mine), default=None),
                        "ahead": any(p.get("publish_at") for p in mine)})
        if dry:
            for p in made:
                p["status"] = "deleted"           # the posting loop may have glimpsed it: it then skips it
            ids = {p["id"] for p in made}
            _posts[:] = [p for p in _posts if p["id"] not in ids]
        else:
            _save()
    if not dry:
        for p in made:
            _mirror(p)
    return {"queued": len(made), "channels": out, "dry": dry}


def projected_schedule(creator_id=None) -> dict:
    """Projected upload times for all unscheduled ready clips, calculated sequentially
    according to channel upload times or creator post gap and daily cap."""
    cs = {c["id"]: c for c in creators.all_creators()}
    accs = accounts.list_accounts()
    if not accs:
        return {}
    known_accounts = {a["id"]: a for a in accs}

    with _lock:
        mine = [p for p in _mine() if p["status"] in OPEN + ("posted",)]
        taken_by_acc = {}
        per_day_by_acc = {}
        for a in accs:
            aid = a["id"]
            taken_by_acc[aid] = {int(_when(p) // 60) for p in mine if p.get("account") == aid}
            per_day = {}
            for p in mine:
                if p.get("account") == aid:
                    dk = _day(_when(p))
                    per_day[dk] = per_day.get(dk, 0) + 1
            per_day_by_acc[aid] = per_day

        all_js = jobs.list_jobs()
        if creator_id:
            all_js = [j for j in all_js if j.get("creator") == creator_id]

        out = {}

        def target_account_for(j):
            for c in j.get("clips", []):
                for p in (c.get("posts") or {}).values():
                    if p.get("status") not in ("cancelled", "failed") and p.get("account") in known_accounts:
                        return known_accounts[p["account"]]
                    if p.get("status") not in ("cancelled", "failed"):
                        for a in accs:
                            if a["name"] == p.get("account_name"):
                                return a
            if j.get("creator"):
                cr = cs.get(j["creator"])
                if cr:
                    for aid in cr.get("accounts", []):
                        if aid in known_accounts:
                            return known_accounts[aid]
                    for a in accs:
                        if a["name"].lower() == cr.get("name", "").lower():
                            return a
            return accs[0]

        for j in all_js:
            acc = target_account_for(j)
            if not acc:
                continue
            aid = acc["id"]
            cr = cs.get(j.get("creator")) or creators.DEFAULTS
            times, own = accounts.schedule_for(aid)
            ahead = bool(times) and accounts.uploads_ahead(aid) and cr.get("privacy", "public") == "public"
            gap_min = cr.get("post_gap_min", 45)
            cap = len(times) if (times and own) else (cr.get("daily_cap", 8))

            for c in j.get("clips", []):
                if c.get("status") != "done":
                    continue
                has_active_post = any(
                    p.get("status") not in ("cancelled", "failed")
                    for p in (c.get("posts") or {}).values()
                )
                if has_active_post:
                    continue

                now = time.time()
                min_start = now + 600 if ahead else now
                taken = taken_by_acc[aid]
                per_day = per_day_by_acc[aid]
                slot = None
                if times:
                    for t in _slots(times, min_start):
                        dk = _day(t)
                        if int(t // 60) not in taken and per_day.get(dk, 0) < cap:
                            slot = t
                            break
                if slot is None:
                    max_taken = max((m * 60 for m in taken), default=now)
                    t = max(now, max_taken + gap_min * 60)
                    for _ in range(30):
                        dk = _day(t)
                        if per_day.get(dk, 0) < cap:
                            slot = t
                            break
                        lt = time.localtime(t + 86400)
                        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))
                    if slot is None:
                        slot = t

                taken.add(int(slot // 60))
                dk = _day(slot)
                per_day[dk] = per_day.get(dk, 0) + 1
                out[f"{j['id']}:{c['id']}"] = {
                    "time": slot,
                    "account_id": aid,
                    "account_name": acc["name"],
                }

        return out


def reschedule(account_id=None) -> int:
    """After the upload times change: lay the waiting posts out again, in their current order, on the new times.
    Posts you sent with Post now or Retry, and ones already tried, are left alone. With `account_id`, only that
    channel's posts move."""
    with _lock:
        move = sorted((p for p in _mine() if p["status"] == "scheduled" and not p.get("tries") and not p.get("manual")
                       and (account_id is None or p["account"] == account_id)),
                      key=_when)
        ids = {p["id"] for p in move}
        for p in move:
            _place(p, ids)
            ids.discard(p["id"])          # from here on this post holds its new time
        _save()
    for p in move:
        _mirror(p)
    return len(move)


def _live(p, now):
    """Posted and public by now (one uploaded ahead to YouTube is public only from its scheduled time)."""
    return p["status"] == "posted" and _when(p) <= now


def _upcoming(p, now):
    return p["status"] in OPEN or (p["status"] == "posted" and _when(p) > now)


def channels_overview(posts=None) -> dict:
    """Each connected channel with its schedule and queue, and the groups, for the Channels page."""
    now, today = time.time(), _day(time.time())
    posts = _mine() if posts is None else posts
    cs = creators.all_creators()
    rows = []
    for a in accounts.list_accounts():
        ps = [p for p in posts if p["account"] == a["id"]]
        up = sorted((p for p in ps if _upcoming(p, now)), key=_when)
        times = a["schedule"]["times"]
        rows.append({**a, "feeds": [{"id": c["id"], "name": c["name"]} for c in cs if a["id"] in c["accounts"]],
                     "waiting": sum(1 for p in ps if p["status"] in OPEN),
                     "on_youtube": sum(1 for p in ps if p["status"] == "posted" and _when(p) > now),
                     "today": sum(1 for p in ps if p["status"] in OPEN + ("posted",) and _day(_when(p)) == today),
                     "posted_today": sum(1 for p in ps if _live(p, now) and _day(_when(p)) == today),
                     "posted_total": sum(1 for p in ps if _live(p, now)),
                     "failed": sum(1 for p in ps if p["status"] == "failed"),
                     "views": sum(p["views"] or 0 for p in ps),
                     "next": [{"at": _when(p), "title": p["title"], "status": p["status"]} for p in up[:4]],
                     "booked_until": _when(up[-1]) if up else None,
                     "free_today": max(0, len(times) - sum(1 for p in ps if p["status"] in OPEN + ("posted",) and _day(_when(p)) == today
                                                          )) if times else None})
    return {"channels": rows, "groups": accounts.groups()}


def overview() -> dict:
    today = _day(time.time())
    now = time.time()
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if _live(p, now) and _day(_when(p)) == today),
            "posted_total": sum(1 for p in ps if _live(p, now)),
            "queued": sum(1 for p in ps if _upcoming(p, now)), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if _upcoming(p, now)), key=_when)
    done = sorted((p for p in _posts if _live(p, now)), key=lambda p: -_when(p))[:15]
    failed = sorted((p for p in _posts if p["status"] == "failed"), key=lambda p: -p["due_at"])
    row = lambda p: {**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")}   # noqa: E731
    show, failed_rows = [row(p) for p in queue + done], [row(p) for p in failed]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if _live(p, now) and _day(_when(p)) in posted:
            posted[_day(_when(p))] += 1
            views[_day(_when(p))] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    sch = accounts.settings()
    return {"paused": sch["paused"], "schedule": {"on": sch["schedule_on"], "times": sch["schedule_times"]}, "creators": rows, "posts": show, "failed": failed_rows, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine()), **channels_overview(_posts)}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
