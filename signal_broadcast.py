"""
signal_broadcast.py
===================
Multi-user signal distribution (Model A).

- Public channel: max FREE_SIGNALS_PER_DAY posts (default 5)
- Premium channel: all signals (invite-only after admin approval)
- Users DM /subscribe → admin approves via /approve USER_ID

Env (.env):
  TELEGRAM_CHANNEL_ID          — public free channel (-100...)
  TELEGRAM_PREMIUM_CHANNEL_ID  — premium channel (-100...)
  TELEGRAM_PREMIUM_INVITE_LINK — t.me/+xxx sent on premium approval
  TELEGRAM_ADMIN_CHAT_ID       — defaults to TELEGRAM_CHAT_ID
  FREE_SIGNALS_PER_DAY=5
"""

import json
import logging
import os
import requests
from datetime import datetime, timezone

log = logging.getLogger(__name__)

STATE_FILE = "subscribers.json"
DEFAULT_STATE = {
    "pending": [],
    "approved": {},
    "daily_public_count": {},
    "activity": {},
}


def _load_env():
    env = {}
    try:
        for line in open(".env"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


_ENV = _load_env()
BOT_TOKEN = _ENV.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = _ENV.get("TELEGRAM_ADMIN_CHAT_ID") or _ENV.get("TELEGRAM_CHAT_ID", "")
PUBLIC_CHANNEL = _ENV.get("TELEGRAM_CHANNEL_ID", "")
PREMIUM_CHANNEL = _ENV.get("TELEGRAM_PREMIUM_CHANNEL_ID", "")
PREMIUM_INVITE = _ENV.get("TELEGRAM_PREMIUM_INVITE_LINK", "")
PUBLIC_INVITE  = _ENV.get("TELEGRAM_PUBLIC_INVITE_LINK", "")
FREE_LIMIT = int(_ENV.get("FREE_SIGNALS_PER_DAY", "5") or "5")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_STATE.items():
            data.setdefault(k, v if not isinstance(v, dict) else {})
        return data
    except Exception:
        return json.loads(json.dumps(DEFAULT_STATE))


def _save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def is_admin(chat_id: str) -> bool:
    return bool(ADMIN_ID) and str(chat_id) == str(ADMIN_ID)


def _post(chat_id: str, text: str) -> bool:
    if not BOT_TOKEN or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
        return r.json().get("ok", False)
    except Exception as e:
        log.warning(f"[BROADCAST] send failed: {e}")
        return False


def _public_count_today(state: dict) -> int:
    return int(state.get("daily_public_count", {}).get(_today(), 0))


def _increment_public(state: dict):
    day = _today()
    counts = state.setdefault("daily_public_count", {})
    counts[day] = int(counts.get(day, 0)) + 1
    # prune old days
    for k in list(counts.keys()):
        if k < day:
            del counts[k]


def broadcast_signal(text: str, tag: str = "crypto") -> dict:
    """
    Post signal to channels.
    Returns summary dict for logging.
    """
    state = _load_state()
    result = {"premium": False, "public": False, "public_skipped": False}

    if PREMIUM_CHANNEL:
        result["premium"] = _post(PREMIUM_CHANNEL, text)

    count = _public_count_today(state)
    if PUBLIC_CHANNEL:
        if count < FREE_LIMIT:
            result["public"] = _post(PUBLIC_CHANNEL, text)
            if result["public"]:
                _increment_public(state)
                _save_state(state)
        else:
            result["public_skipped"] = True
            log.info(f"[BROADCAST] Public cap reached ({FREE_LIMIT}/day). Premium only.")

    log.info(f"[BROADCAST] {tag} premium={result['premium']} public={result['public']}")
    return result


def notify_admin(text: str):
    if ADMIN_ID:
        _post(ADMIN_ID, text)


def is_approved_subscriber(chat_id: str) -> bool:
    state = _load_state()
    return str(chat_id) in state.get("approved", {})


def subscriber_status_message(chat_id: str) -> str:
    """Reply for /mystatus or casual messages from subscribers."""
    state = _load_state()
    uid = str(chat_id)
    if uid in state.get("approved", {}):
        info = state["approved"][uid]
        tier = info.get("tier", "premium")
        lines = [f"✅ <b>Approved</b> — tier: <b>{tier}</b>"]
        if tier == "premium" and PREMIUM_INVITE:
            lines.append(f"\n📡 Premium channel:\n{PREMIUM_INVITE}")
        elif tier == "free" and PUBLIC_INVITE:
            lines.append(f"\n📡 Free channel (max {FREE_LIMIT} signals/day):\n{PUBLIC_INVITE}")
        elif tier == "premium":
            lines.append("\n⚠️ Premium invite link not configured yet. Ask admin.")
        else:
            lines.append(f"\n📡 Free tier — up to {FREE_LIMIT} signals/day. Ask admin for channel link.")
        lines.append("\nSignals are posted in the channel. Use /channel to see your link again.")
        return "\n".join(lines)
    pending = any(p.get("user_id") == uid for p in state.get("pending", []))
    if pending:
        return "⏳ Pending approval. You'll be notified when approved."
    return (
        "👋 Welcome! This bot distributes trading signals.\n\n"
        "/subscribe — request premium access\n"
        "/subscribe free — free tier (5 signals/day)\n"
        "/paid TXN_ID — submit payment reference\n"
        "/mystatus — check approval status"
    )


def resend_channel_link(chat_id: str) -> str:
    return subscriber_status_message(chat_id)


def request_subscribe(chat_id: str, username: str = "", tier: str = "premium") -> str:
    touch_activity(chat_id, f"/subscribe {tier}", username)
    state = _load_state()
    uid = str(chat_id)

    if uid in state.get("approved", {}):
        tier_name = state["approved"][uid].get("tier", "premium")
        return subscriber_status_message(chat_id)

    for p in state.get("pending", []):
        if p.get("user_id") == uid:
            return "⏳ Your request is already pending. You'll be notified once approved."

    entry = {
        "user_id": uid,
        "username": username or "unknown",
        "tier": tier,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "payment_ref": "",
    }
    state.setdefault("pending", []).append(entry)
    _save_state(state)

    notify_admin(
        f"📥 <b>New subscribe request</b>\n"
        f"User: {username or uid}\n"
        f"ID: <code>{uid}</code>\n"
        f"Tier: {tier}\n\n"
        f"/approve {uid} — grant premium\n"
        f"/approve {uid} free — grant free channel only\n"
        f"/reject {uid}"
    )
    return (
        "✅ <b>Request submitted!</b>\n\n"
        f"An admin will review your request shortly.\n"
        f"Free tier: up to <b>{FREE_LIMIT} signals/day</b> on the public channel.\n"
        f"Premium: unlimited signals + full access.\n\n"
        f"After payment, send: /paid YOUR_TXN_ID"
    )


def submit_payment_ref(chat_id: str, ref: str) -> str:
    touch_activity(chat_id, f"/paid {ref[:40]}")
    state = _load_state()
    uid = str(chat_id)
    for p in state.get("pending", []):
        if p.get("user_id") == uid:
            p["payment_ref"] = ref
            _save_state(state)
            notify_admin(
                f"💳 <b>Payment ref submitted</b>\n"
                f"User: {p.get('username')} (<code>{uid}</code>)\n"
                f"Ref: <code>{ref}</code>\n\n"
                f"/approve {uid}"
            )
            return "💳 Payment reference received. Admin will verify and approve soon."
    return "No pending request found. Send /subscribe first."


def _parse_cmd(text: str) -> tuple[str, list]:
    """Split command; strip @BotName suffix from group chats."""
    parts = text.strip().split()
    if not parts:
        return "", parts
    cmd = parts[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    return cmd, parts


def approve_user(user_id: str, tier: str = None) -> str:
    state = _load_state()
    uid = str(user_id)
    pending = state.get("pending", [])
    match = next((p for p in pending if p.get("user_id") == uid), None)
    state["pending"] = [p for p in pending if p.get("user_id") != uid]

    existing = state.get("approved", {}).get(uid)

    if not match and not existing:
        return f"❌ User {uid} not in pending or approved lists."

    # Determine username
    username = ""
    if match:
        username = match.get("username", "")
    elif existing:
        username = existing.get("username", "")

    # Determine tier
    if not tier:
        if match:
            tier = match.get("tier", "premium")
        elif existing:
            tier = existing.get("tier", "premium")
        else:
            tier = "premium"

    state.setdefault("approved", {})[uid] = {
        "tier": tier,
        "username": username,
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_state(state)

    if tier == "premium" and PREMIUM_INVITE:
        msg = (
            f"🎉 <b>Approved — Premium access</b>\n\n"
            f"Join the premium signals channel:\n{PREMIUM_INVITE}\n\n"
            f"You get <b>unlimited</b> signals."
        )
    elif tier == "free":
        if PUBLIC_INVITE:
            msg = (
                f"✅ <b>Approved — Free tier</b>\n\n"
                f"Join the free signals channel:\n{PUBLIC_INVITE}\n\n"
                f"Up to <b>{FREE_LIMIT} signals/day</b>."
            )
        else:
            msg = (
                f"✅ <b>Approved — Free tier</b>\n\n"
                f"Up to <b>{FREE_LIMIT} signals/day</b> on the public channel.\n"
                f"Ask admin for the channel link."
            )
    else:
        msg = f"✅ <b>Approved</b> ({tier}). Contact admin for channel access."

    _post(uid, msg)
    return f"✅ Approved {uid} as <b>{tier}</b>."


def reject_user(user_id: str, reason: str = "") -> str:
    state = _load_state()
    uid = str(user_id)
    pending = state.get("pending", [])

    in_pending = any(p.get("user_id") == uid for p in pending)
    in_approved = uid in state.get("approved", {})

    if not in_pending and not in_approved:
        return f"❌ User {uid} not found in pending or approved lists."

    # Remove from pending if there
    state["pending"] = [p for p in pending if p.get("user_id") != uid]

    # Remove from approved if there
    if in_approved:
        state.setdefault("approved", {}).pop(uid, None)

    _save_state(state)
    _post(uid, f"❌ Your subscribe request was declined/revoked.{(' Reason: ' + reason) if reason else ''}")
    return f"❌ Rejected/Revoked {uid}."


def list_pending() -> str:
    state = _load_state()
    pending = state.get("pending", [])
    if not pending:
        return "📭 No pending subscribe requests."
    lines = ["📥 <b>Pending requests</b>\n"]
    for p in pending:
        ref = p.get("payment_ref") or "—"
        lines.append(
            f"• {p.get('username', '?')} | <code>{p['user_id']}</code>\n"
            f"  tier={p.get('tier','?')} | paid_ref={ref}\n"
            f"  /approve {p['user_id']}\n"
        )
    return "\n".join(lines)


def touch_activity(user_id: str, action: str, username: str = ""):
    """Record what a subscriber did and when."""
    state = _load_state()
    uid = str(user_id)
    activity = state.setdefault("activity", {})
    entry = activity.setdefault(uid, {"username": "", "last_seen": "", "last_action": "", "history": []})
    if username:
        entry["username"] = username
    now = datetime.now(timezone.utc).isoformat()
    entry["last_seen"] = now
    entry["last_action"] = action[:120]
    hist = entry.setdefault("history", [])
    hist.append({"at": now, "action": action[:120]})
    entry["history"] = hist[-8:]
    _save_state(state)


def _fmt_time(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d %b %H:%M UTC")
    except Exception:
        return iso[:16]


def user_detail(user_id: str) -> str:
    state = _load_state()
    uid = str(user_id)
    lines = [f"👤 <b>User</b> <code>{uid}</code>\n"]

    approved = state.get("approved", {}).get(uid)
    pending = next((p for p in state.get("pending", []) if p.get("user_id") == uid), None)
    activity = state.get("activity", {}).get(uid, {})

    if approved:
        lines.append(f"Status: ✅ <b>approved</b> ({approved.get('tier', '?')})")
        lines.append(f"Approved: {_fmt_time(approved.get('approved_at', ''))}")
    elif pending:
        lines.append("Status: ⏳ <b>pending</b>")
        lines.append(f"Requested tier: {pending.get('tier', '?')}")
        lines.append(f"Requested: {_fmt_time(pending.get('requested_at', ''))}")
        if pending.get("payment_ref"):
            lines.append(f"Payment ref: <code>{pending['payment_ref']}</code>")
    else:
        lines.append("Status: ❌ not subscribed")

    if activity:
        lines.append(f"Username: @{activity.get('username') or '—'}")
        lines.append(f"Last seen: {_fmt_time(activity.get('last_seen', ''))}")
        lines.append(f"Last action: {activity.get('last_action') or '—'}")
        hist = activity.get("history", [])
        if hist:
            lines.append("\n<b>Recent activity</b>")
            for h in reversed(hist[-5:]):
                lines.append(f"  • {_fmt_time(h.get('at', ''))} — {h.get('action', '?')}")

    if not approved and not pending:
        lines.append("\n/approve {uid} — approve premium".format(uid=uid))

    return "\n".join(lines)


def users_dashboard() -> str:
    state = _load_state()
    pending = state.get("pending", [])
    approved = state.get("approved", {})
    activity = state.get("activity", {})

    premium = sum(1 for u in approved.values() if u.get("tier") == "premium")
    free = sum(1 for u in approved.values() if u.get("tier") == "free")
    pub_count = _public_count_today(state)

    lines = [
        "👥 <b>SUBSCRIBER DASHBOARD</b>",
        "",
        "<b>Summary</b>",
        f"Pending: {len(pending)}",
        f"Approved: {len(approved)} (premium {premium} | free {free})",
        f"Public signals today: {pub_count}/{FREE_LIMIT}",
        f"Channels: public={'✓' if PUBLIC_CHANNEL else '✗'} premium={'✓' if PREMIUM_CHANNEL else '✗'}",
        "",
    ]

    if pending:
        lines.append("<b>⏳ Pending</b>")
        for p in pending:
            uid = p.get("user_id", "?")
            act = activity.get(uid, {})
            uname = p.get("username") or act.get("username") or "—"
            ref = p.get("payment_ref") or "—"
            lines.append(
                f"• @{uname} | <code>{uid}</code>\n"
                f"  tier={p.get('tier','?')} | paid={ref}\n"
                f"  asked {_fmt_time(p.get('requested_at',''))}\n"
                f"  /approve {uid} | /reject {uid}"
            )
        lines.append("")

    if approved:
        lines.append("<b>✅ Approved</b>")
        for uid, info in sorted(approved.items(), key=lambda x: x[1].get("approved_at", ""), reverse=True):
            act = activity.get(uid, {})
            uname = info.get("username") or act.get("username") or "—"
            last = act.get("last_action") or "—"
            lines.append(
                f"• @{uname} | <code>{uid}</code>\n"
                f"  tier={info.get('tier','?')} | approved {_fmt_time(info.get('approved_at',''))}\n"
                f"  last: {_fmt_time(act.get('last_seen',''))} — {last}\n"
                f"  /user {uid}"
            )
        lines.append("")

    # Recent activity from non-approved visitors
    orphans = [
        (uid, act) for uid, act in activity.items()
        if uid not in approved and not any(p.get("user_id") == uid for p in pending)
    ]
    if orphans:
        lines.append("<b>👀 Recent visitors (not subscribed)</b>")
        for uid, act in sorted(orphans, key=lambda x: x[1].get("last_seen", ""), reverse=True)[:5]:
            uname = act.get("username") or "—"
            lines.append(
                f"• @{uname} | <code>{uid}</code>\n"
                f"  {_fmt_time(act.get('last_seen',''))} — {act.get('last_action','?')}"
            )

    if not pending and not approved and not orphans:
        lines.append("No subscribers yet. Users can DM /subscribe")

    return "\n".join(lines)


def _send_long(send_fn, chat_id: str, text: str, chunk_size: int = 3800):
    if len(text) <= chunk_size:
        send_fn(chat_id, text)
        return
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > chunk_size:
            send_fn(chat_id, chunk)
            chunk = line + "\n"
        else:
            chunk += line + "\n"
    if chunk.strip():
        send_fn(chat_id, chunk)


def broadcast_stats() -> str:
    state = _load_state()
    count = _public_count_today(state)
    approved = len(state.get("approved", {}))
    pending = len(state.get("pending", []))
    return (
        f"📡 <b>Broadcast stats</b>\n"
        f"Public signals today: {count}/{FREE_LIMIT}\n"
        f"Approved users: {approved}\n"
        f"Pending requests: {pending}\n"
        f"Public channel: {'set' if PUBLIC_CHANNEL else 'not set'}\n"
        f"Premium channel: {'set' if PREMIUM_CHANNEL else 'not set'}"
    )


def handle_public_command(chat_id: str, text: str, send_fn, username: str = "") -> bool:
    """Commands available to non-admin users. Returns True if handled."""
    cmd, parts = _parse_cmd(text)

    if cmd in ("/start", "/subscribe"):
        tier = "premium"
        if len(parts) > 1 and parts[1].lower() == "free":
            tier = "free"
        send_fn(chat_id, request_subscribe(chat_id, username, tier))
        return True

    if cmd == "/paid" and len(parts) > 1:
        send_fn(chat_id, submit_payment_ref(chat_id, " ".join(parts[1:])))
        return True

    if cmd == "/mystatus":
        touch_activity(chat_id, "/mystatus", username)
        send_fn(chat_id, subscriber_status_message(chat_id))
        return True

    if cmd == "/channel":
        touch_activity(chat_id, "/channel", username)
        send_fn(chat_id, resend_channel_link(chat_id))
        return True

    return False


def handle_admin_command(chat_id: str, text: str, send_fn) -> bool:
    """Admin-only broadcast commands. Returns True if handled."""
    if not is_admin(chat_id):
        return False

    cmd, parts = _parse_cmd(text)

    if cmd == "/pending":
        send_fn(chat_id, list_pending())
        return True

    if cmd == "/broadcaststats":
        send_fn(chat_id, broadcast_stats())
        return True

    if cmd == "/users" or cmd == "/subs":
        _send_long(send_fn, chat_id, users_dashboard())
        return True

    if cmd == "/user":
        if len(parts) >= 2:
            send_fn(chat_id, user_detail(parts[1]))
            return True
        # /user alone → show all subscribers (not LLM account summary)
        _send_long(send_fn, chat_id, users_dashboard())
        return True

    if cmd == "/approve":
        tier = None
        if len(parts) > 2:
            if parts[2].lower() == "free":
                tier = "free"
            elif parts[2].lower() == "premium":
                tier = "premium"
        if len(parts) >= 2:
            send_fn(chat_id, approve_user(parts[1], tier))
            return True
        # /approve with no ID — auto-approve if exactly one pending
        state = _load_state()
        pending = state.get("pending", [])
        if len(pending) == 1:
            uid = pending[0]["user_id"]
            send_fn(chat_id, approve_user(uid, tier or pending[0]["tier"]))
            return True
        send_fn(chat_id,
            "❌ Usage: <code>/approve USER_ID [free/premium]</code>\n"
            "Example: <code>/approve 8871581172 free</code>\n\n"
            + list_pending()
        )
        return True

    if cmd == "/reject":
        if len(parts) >= 2:
            reason = " ".join(parts[2:]) if len(parts) > 2 else ""
            send_fn(chat_id, reject_user(parts[1], reason))
            return True
        state = _load_state()
        pending = state.get("pending", [])
        if len(pending) == 1:
            send_fn(chat_id, reject_user(pending[0]["user_id"]))
            return True
        send_fn(chat_id,
            "❌ Usage: <code>/reject USER_ID</code>\n\n" + list_pending()
        )
        return True

    return False
