#!/usr/bin/env python3
# =============================================================
#  bot.py  —  Telegram Re-Upload Bot  |  Max Speed Edition
#             Optimised for Render.com (~1-2 Gbps, AWS)
#
#  SETUP:
#  1. Push this file + login.py + requirements.txt to GitHub
#  2. Go to render.com → New → Background Worker
#  3. Set environment variables (see below)
#  4. Deploy
# =============================================================

import os, re, json, time, asyncio, logging, warnings, signal

logging.getLogger('telethon').setLevel(logging.CRITICAL)
logging.getLogger('asyncio').setLevel(logging.CRITICAL)
warnings.filterwarnings('ignore')

from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError
from telethon.network import ConnectionTcpFull
from telethon.tl.types import (
    MessageMediaDocument, MessageMediaPhoto,
    DocumentAttributeVideo, DocumentAttributeFilename,
    PhotoSize, PhotoStrippedSize, PhotoCachedSize,
)

# ── Credentials from Environment Variables ────────────────────
# Set these in Render Dashboard → Environment
# DO NOT hardcode credentials here
API_ID    = int(os.environ.get('API_ID', '0'))
API_HASH  = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
# ─────────────────────────────────────────────────────────────

DL_CONNECTIONS  = 4
PARALLEL_DL_MIN = 5 * 1024 * 1024

# ── Paths ─────────────────────────────────────────────────────
# Render provides /tmp for ephemeral storage (fast SSD)
BASE_DIR      = '/tmp/TelegramBot'
SESSION_DIR   = os.path.join(BASE_DIR, 'sessions')
SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')
DOWNLOAD_DIR  = os.path.join(BASE_DIR, 'downloads')
BOT_SESSION   = os.path.join(SESSION_DIR, 'bot_session')
USER_SESSION  = os.path.join(SESSION_DIR, 'user_session')

os.makedirs(SESSION_DIR,  exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ── State ─────────────────────────────────────────────────────
user_client  = None
user_states  = {}
cancel_flags = {}

DEFAULT_SETTINGS = {
    'source_chat'   : '',
    'target_chat'   : '',
    'caption'       : '',
    'rename_tag'    : '',
    'replace_words' : {},
    'remove_words'  : [],
    'thumbnail'     : None,
    'dl_connections': DL_CONNECTIONS,
}


# =============================================================
#  SETTINGS
# =============================================================

def load_s():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        for k, v in DEFAULT_SETTINGS.items():
            data.setdefault(k, v)
        return data
    data = DEFAULT_SETTINGS.copy()
    save_s(data)
    return data

def save_s(data):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

def cap_label(s):
    c = s.get('caption', '')
    if c.strip():  return c[:40] + ('...' if len(c) > 40 else '')
    if c == '':    return '(use original)'
    return '(no caption)'

def settings_text():
    s  = load_s()
    ok = 'Logged in' if (user_client and user_client.is_connected()) else 'Not logged in'
    rw = '\n'.join('  ' + k + ' -> ' + v for k, v in s['replace_words'].items()) or '  none'
    rm = ', '.join(s['remove_words']) if s['remove_words'] else 'none'
    return '\n'.join([
        '**Bot Settings** (Render.com)', '',
        'Login    : ' + ok,
        'Source   : `' + str(s['source_chat'] or 'not set') + '`',
        'Target   : `' + str(s['target_chat'] or 'not set') + '`', '',
        'Caption  : `' + cap_label(s) + '`',
        'Rename   : `' + (s['rename_tag'] or 'none') + '`',
        'Replace  :', rw,
        'Remove   : ' + rm,
        'Thumb    : ' + ('set' if s['thumbnail'] else 'none'), '',
        'DL conns : `' + str(s['dl_connections']) + '` (parallel streams)',
        'UL chunk : `512 KB` (max, fixed)',
    ])


# =============================================================
#  HELPERS
# =============================================================

def parse_link(raw):
    t = raw.strip()
    m = re.search(r't[.]me/c/(\d+)/(\d+)/(\d+)', t)
    if m: return int('-100' + m.group(1)), int(m.group(3))
    m = re.search(r't[.]me/c/(\d+)/(\d+)', t)
    if m: return int('-100' + m.group(1)), int(m.group(2))
    m = re.search(r't[.]me/([a-zA-Z0-9_]+)/(\d+)/(\d+)', t)
    if m: return m.group(1), int(m.group(3))
    m = re.search(r't[.]me/([a-zA-Z0-9_]+)/(\d+)', t)
    if m: return m.group(1), int(m.group(2))
    return None, None

def apply_filters(text, s):
    if not text: return text
    for w in s.get('remove_words', []):
        if w: text = text.replace(w, '')
    for old, new in s.get('replace_words', {}).items():
        if old: text = text.replace(old, new)
    return text.strip()

def make_filename(original, s):
    name, ext = os.path.splitext(original)
    name = apply_filters(name, s)
    tag  = s.get('rename_tag', '').strip()
    if tag: name = name + ' ' + tag
    return (name.strip() + ext).strip() or original

def make_caption(s, msg, fname, size_mb):
    c = s.get('caption', '')
    if c == ' ':  return ''
    if c.strip():
        out = c.replace('{filename}', fname).replace('{size}', str(round(size_mb, 1)) + ' MB')
        return apply_filters(out, s)
    return apply_filters(msg.message or '', s)

def safe_del(path):
    try:
        if path and os.path.exists(str(path)): os.remove(str(path))
    except Exception: pass

def doc_size_bytes(msg):
    try:
        if isinstance(msg.media, MessageMediaDocument) and msg.media.document:
            return msg.media.document.size or 0
    except Exception: pass
    return 0

async def fetch_thumb(msg):
    try:
        if not isinstance(msg.media, MessageMediaDocument): return None
        doc = msg.media.document
        if not doc or not doc.thumbs: return None
        best = None; bs = 0
        for t in doc.thumbs:
            if isinstance(t, PhotoSize) and t.size > bs:
                bs = t.size; best = t
            elif isinstance(t, PhotoCachedSize) and len(t.bytes or b'') > bs:
                bs = len(t.bytes); best = t
        if best is None:
            for t in doc.thumbs:
                if isinstance(t, PhotoStrippedSize): best = t; break
        if best is None: return None
        p = os.path.join(DOWNLOAD_DIR, 'thumb_' + str(doc.id) + '.jpg')
        await user_client.download_media(msg.media.document, file=p, thumb=best)
        return p if os.path.exists(p) and os.path.getsize(p) > 0 else None
    except Exception: return None


# =============================================================
#  PROGRESS UI
# =============================================================

_spd = {}

def smooth_spd(key, instant):
    prev = _spd.get(key, instant)
    v = 0.25 * instant + 0.75 * prev
    _spd[key] = v
    return v

def fmt_size(b):
    mb = b / 1048576
    if mb >= 1024: return str(round(mb / 1024, 2)) + ' GB'
    if mb >= 1:    return str(round(mb, 1)) + ' MB'
    return str(round(mb * 1024)) + ' KB'

def fmt_eta(s):
    if s <= 0 or s > 86400: return '--'
    if s < 60:   return str(int(s)) + 's'
    if s < 3600: return str(int(s // 60)) + 'm' + str(int(s % 60)) + 's'
    return str(int(s // 3600)) + 'h' + str(int((s % 3600) // 60)) + 'm'

def bar(pct, w=20):
    f = int(pct * w)
    return '[' + '#' * f + '-' * (w - f) + ']'

def prog_txt(mode, idx, total, cur, tot, t0, key, ok, skip, fname='', streams=1):
    pct  = cur / tot if tot > 0 else 0
    mb_c = cur / 1048576
    mb_t = tot / 1048576
    ela  = max(time.time() - t0, 0.1)
    spd  = smooth_spd(key, mb_c / ela)
    eta  = (mb_t - mb_c) / spd if spd > 0.01 else 0
    arrow = 'DL' if mode == 'dl' else 'UL'
    sinfo = (' x' + str(streams) + ' streams') if streams > 1 else ''
    fname_s = (fname[:28] + '..') if len(fname) > 30 else fname
    lines = [
        '[' + arrow + sinfo + '] [' + str(idx) + '/' + str(total) + ']  ' + str(round(pct * 100, 1)) + '%',
        '`' + bar(pct) + '`',
    ]
    if fname_s:
        lines.append('`' + fname_s + '`')
    lines += [
        fmt_size(cur) + ' / ' + fmt_size(tot),
        str(round(spd, 2)) + ' MB/s   ETA ' + fmt_eta(eta),
        'done:' + str(ok) + '  skip:' + str(skip),
    ]
    return '\n'.join(lines)

def done_txt(idx, total, fname, sz_b, dl_spd, ul_spd, dl_t, ul_t, ok, skip, caption):
    sz_s = fmt_size(sz_b)
    cap_line = ('\n`' + caption[:50] + ('...' if len(caption) > 50 else '') + '`') if caption else ''
    fname_s = (fname[:32] + '..') if len(fname) > 34 else fname
    return '\n'.join([
        'DONE [' + str(idx) + '/' + str(total) + ']',
        '`' + fname_s + '`',
        sz_s + '   DL ' + str(round(dl_spd, 1)) + ' MB/s   UL ' + str(round(ul_spd, 1)) + ' MB/s',
        'Total ' + fmt_eta(dl_t + ul_t) + cap_line,
        'done:' + str(ok) + '  skip:' + str(skip),
    ])

def skip_txt(idx, total, mid, reason, ok, skip):
    return ('SKIP [' + str(idx) + '/' + str(total) + '] id:' + str(mid) + '\n'
            '`' + reason[:100] + '`\n'
            'done:' + str(ok) + '  skip:' + str(skip))

def final_txt(total, ok, skip, elapsed):
    return '\n'.join(['ALL DONE!', '',
        'Total  : ' + str(total),
        'Success: ' + str(ok),
        'Skipped: ' + str(skip),
        'Time   : ' + fmt_eta(elapsed),
    ])


# =============================================================
#  CLIENT FACTORY
# =============================================================

def make_client(session_path, receive_updates=False):
    return TelegramClient(
        session_path, API_ID, API_HASH,
        connection=ConnectionTcpFull,
        connection_retries=3,
        retry_delay=1,
        auto_reconnect=True,
        receive_updates=receive_updates,
        flood_sleep_threshold=0,
        request_retries=1,
        timeout=30,
    )

async def check_connected():
    global user_client
    if not user_client: return False
    try:
        if not user_client.is_connected():
            await user_client.connect()
        return await user_client.is_user_authorized()
    except Exception: return False


# =============================================================
#  PARALLEL DOWNLOAD
# =============================================================

CHUNK = 512 * 1024

async def _dl_stream(client, media, file_size, stream_idx, n_streams,
                     out_path, written, lock, t0, progress_cb):
    offset = stream_idx * CHUNK
    stride = n_streams * CHUNK
    pos    = offset
    async for chunk in client.iter_download(
        media, offset=offset, stride=stride,
        request_size=CHUNK, file_size=file_size,
    ):
        async with lock:
            with open(out_path, 'r+b') as fh:
                fh.seek(pos); fh.write(chunk)
            written[0] += len(chunk)
            await progress_cb(written[0], file_size)
        pos += stride

async def parallel_download(msg, out_path, file_size, n_streams,
                             status_msg, idx, total, ok, skip, fname):
    t0 = time.time(); last_t = [0.0]
    key = 'dl' + str(idx); written = [0]; lock = asyncio.Lock()

    async def cb(cur, tot):
        now = time.time()
        if now - last_t[0] < 3.5 and cur < tot: return
        last_t[0] = now
        try:
            await status_msg.edit(
                prog_txt('dl', idx, total, cur, tot, t0, key, ok, skip, fname, n_streams),
                buttons=cancel_kb(), parse_mode='markdown')
        except Exception: pass

    with open(out_path, 'wb') as fh:
        fh.seek(file_size - 1); fh.write(b'\x00')

    media = msg.media.document if isinstance(msg.media, MessageMediaDocument) else msg.media.photo
    clients = []
    for i in range(n_streams):
        c = make_client(USER_SESSION); await c.connect(); clients.append(c)
    try:
        await asyncio.gather(*[
            _dl_stream(clients[i], media, file_size, i, n_streams,
                       out_path, written, lock, t0, cb)
            for i in range(n_streams)
        ])
    finally:
        for c in clients:
            try: await c.disconnect()
            except Exception: pass
    return out_path

async def simple_download(msg, out_path, file_size, status_msg, idx, total, ok, skip, fname):
    t0 = time.time(); last_t = [0.0]; key = 'dl' + str(idx)
    async def cb(cur, tot):
        now = time.time()
        if now - last_t[0] < 3.5 and cur < tot: return
        last_t[0] = now
        try:
            await status_msg.edit(
                prog_txt('dl', idx, total, cur, tot, t0, key, ok, skip, fname),
                buttons=cancel_kb(), parse_mode='markdown')
        except Exception: pass
    result = await user_client.download_media(msg, file=out_path, progress_callback=cb)
    return str(result) if result else None

async def download_file(msg, status_msg, idx, total, ok, skip, fname, file_size):
    s = load_s(); n_streams = int(s.get('dl_connections', DL_CONNECTIONS))
    use_parallel = (
        file_size >= PARALLEL_DL_MIN
        and isinstance(msg.media, MessageMediaDocument)
        and n_streams > 1
    )
    ext = os.path.splitext(fname)[1] or '.bin'
    out_path = os.path.join(DOWNLOAD_DIR, 'dl_' + str(idx) + '_' + str(int(time.time())) + ext)
    last_err = ''
    for attempt in range(1, 4):
        try:
            if use_parallel:
                result = await parallel_download(msg, out_path, file_size, n_streams,
                                                  status_msg, idx, total, ok, skip, fname)
            else:
                result = await simple_download(msg, out_path, file_size,
                                               status_msg, idx, total, ok, skip, fname)
            if not result:                             last_err = 'no output path'; continue
            path = str(result)
            if not os.path.exists(path):               last_err = 'file missing';   continue
            actual = os.path.getsize(path)
            if actual == 0: safe_del(path);            last_err = 'empty file';     continue
            if use_parallel and file_size > 0 and actual != file_size:
                with open(path, 'r+b') as fh: fh.truncate(file_size)
            return path, None
        except FloodWaitError as fe:
            last_err = 'FloodWait ' + str(fe.seconds) + 's'
            await asyncio.sleep(fe.seconds + 2); await check_connected()
        except asyncio.CancelledError: raise
        except Exception as ex:
            last_err = str(ex)
            if attempt < 3:
                use_parallel = False
                try:
                    await status_msg.edit('DL retry ' + str(attempt) + '/3\n`' + last_err[:100] + '`',
                                          buttons=cancel_kb(), parse_mode='markdown')
                except Exception: pass
                await asyncio.sleep(attempt * 3); await check_connected()
    return None, last_err


# =============================================================
#  UPLOAD
# =============================================================

async def upload_file(path, target, caption, thumb, is_photo, vw, vh, vd,
                      status_msg, idx, total, ok, skip):
    import mimetypes as _mt
    sz_b = os.path.getsize(path); sz_mb = sz_b / 1048576
    t0 = time.time(); last_t = [0.0]; key = 'ul' + str(idx)
    fname = os.path.basename(path)
    _mime = _mt.guess_type(fname)[0] or 'application/octet-stream'
    send_as_photo = is_photo and fname.lower().endswith(('.jpg','.jpeg','.png','.gif','.webp','.bmp'))

    async def cb(cur, tot):
        now = time.time()
        if now - last_t[0] < 3.5 and cur < tot: return
        last_t[0] = now
        try:
            await status_msg.edit(prog_txt('ul', idx, total, cur, tot, t0, key, ok, skip, fname),
                                  buttons=cancel_kb(), parse_mode='markdown')
        except Exception: pass

    file_handle = await user_client.upload_file(path, part_size_kb=512,
                                                 file_name=fname, progress_callback=cb)
    attrs = [DocumentAttributeFilename(file_name=fname)]
    if not send_as_photo and vd > 0:
        attrs.append(DocumentAttributeVideo(duration=vd, w=vw, h=vh, supports_streaming=True))

    thumb_handle = None
    if thumb and not send_as_photo:
        try: thumb_handle = await user_client.upload_file(thumb, part_size_kb=64)
        except Exception: pass

    await user_client.send_file(target, file=file_handle, caption=caption,
                                 thumb=thumb_handle,
                                 supports_streaming=(not send_as_photo and vd > 0),
                                 force_document=(not send_as_photo),
                                 mime_type=_mime, attributes=attrs)
    return sz_mb / max(time.time() - t0, 0.1)


# =============================================================
#  TRANSFER ENGINE
# =============================================================

async def run_transfer(uid, source, msg_ids, status_msg):
    s = load_s(); total = len(msg_ids)
    ok_count = 0; skip_count = 0; run_start = time.time()
    try:    target = int(s['target_chat'])
    except: await status_msg.edit('Target not set.', buttons=main_kb(), parse_mode='markdown'); return
    if not await check_connected():
        await status_msg.edit('Not connected.', buttons=main_kb(), parse_mode='markdown'); return

    for idx, mid in enumerate(msg_ids, 1):
        if cancel_flags.get(uid):
            cancel_flags.pop(uid, None)
            await status_msg.edit(final_txt(total, ok_count, skip_count, time.time()-run_start)+'\n(Cancelled)',
                                  buttons=main_kb(), parse_mode='markdown'); return
        try:
            await status_msg.edit('Fetching ['+str(idx)+'/'+str(total)+'] id:'+str(mid)+'\n'
                'done:'+str(ok_count)+'  skip:'+str(skip_count), buttons=cancel_kb(), parse_mode='markdown')
        except Exception: pass

        msg = None; fetch_err = ''
        for attempt in range(1, 4):
            try:
                msg = await asyncio.wait_for(user_client.get_messages(source, ids=mid), timeout=20.0); break
            except asyncio.TimeoutError: fetch_err = 'timeout'; await check_connected()
            except FloodWaitError as fe:
                fetch_err = 'FloodWait '+str(fe.seconds)+'s'
                await asyncio.sleep(fe.seconds+2); await check_connected()
            except Exception as ex: fetch_err = str(ex)[:80]; await asyncio.sleep(3); await check_connected()

        if msg is None:
            skip_count += 1
            try: await status_msg.edit(skip_txt(idx,total,mid,'Fetch failed: '+fetch_err,ok_count,skip_count),
                                       buttons=cancel_kb(), parse_mode='markdown')
            except Exception: pass
            continue

        is_photo = isinstance(msg.media, MessageMediaPhoto)
        is_doc   = isinstance(msg.media, MessageMediaDocument)

        if not is_photo and not is_doc:
            if msg.message and msg.message.strip():
                cap = apply_filters(msg.message, s)
                custom = s.get('caption','')
                if custom and custom != ' ':
                    cap = apply_filters(custom.replace('{filename}','').replace('{size}',''), s)
                try: await user_client.send_message(target, cap); ok_count += 1
                except Exception: skip_count += 1
            else: skip_count += 1
            continue

        vw = vh = vd = 0; orig_name = None
        if is_doc:
            for a in msg.media.document.attributes:
                if isinstance(a, DocumentAttributeVideo): vw, vh, vd = a.w, a.h, a.duration
                if isinstance(a, DocumentAttributeFilename) and orig_name is None: orig_name = a.file_name

        if is_photo:
            orig_name = 'photo_'+str(mid)+'.jpg'
            try: file_size = max((getattr(sz,'size',0) for sz in msg.media.photo.sizes), default=0)
            except: file_size = 0
        else: file_size = doc_size_bytes(msg)

        raw_fname = orig_name or ('file_'+str(mid)+'.bin')
        dl_start = time.time()
        fpath, err = await download_file(msg, status_msg, idx, total, ok_count, skip_count, raw_fname, file_size)

        if not fpath:
            skip_count += 1
            try: await status_msg.edit(skip_txt(idx,total,mid,'DL failed: '+str(err),ok_count,skip_count),
                                       buttons=cancel_kb(), parse_mode='markdown')
            except Exception: pass
            continue

        dl_time = max(time.time()-dl_start, 0.1)
        sz_b = os.path.getsize(fpath); sz_mb = sz_b/1048576; dl_spd = sz_mb/dl_time

        if cancel_flags.get(uid):
            safe_del(fpath); cancel_flags.pop(uid, None)
            await status_msg.edit(final_txt(total,ok_count,skip_count,time.time()-run_start)+'\n(Cancelled)',
                                  buttons=main_kb(), parse_mode='markdown'); return

        raw_clean = re.sub(r'[\\/:<>|*?"]+','_',(orig_name or os.path.basename(fpath))).strip()
        new_name  = make_filename(raw_clean, s)
        new_path  = os.path.join(DOWNLOAD_DIR, new_name)
        if os.path.abspath(fpath) != os.path.abspath(new_path):
            try: safe_del(new_path); os.rename(fpath, new_path); fpath = new_path
            except Exception: new_name = os.path.basename(fpath)

        caption = make_caption(s, msg, new_name, sz_mb)
        thumb = None; thumb_temp = False
        ct = s.get('thumbnail','') or ''
        if ct and os.path.exists(ct): thumb = ct
        elif not is_photo:
            thumb = await fetch_thumb(msg)
            if thumb: thumb_temp = True

        ul_start = time.time(); upload_ok = False; upload_err = ''
        for attempt in range(1, 4):
            try:
                await upload_file(fpath, target, caption, thumb, is_photo, vw, vh, vd,
                                  status_msg, idx, total, ok_count, skip_count)
                upload_ok = True; break
            except FloodWaitError as fe:
                upload_err = 'FloodWait '+str(fe.seconds)+'s'
                await asyncio.sleep(fe.seconds+2); await check_connected()
            except asyncio.CancelledError:
                safe_del(fpath)
                if thumb_temp: safe_del(thumb)
                raise
            except Exception as ex:
                upload_err = str(ex)
                if attempt < 3:
                    try: await status_msg.edit('UL retry '+str(attempt)+'/3\n`'+upload_err[:100]+'`',
                                               buttons=cancel_kb(), parse_mode='markdown')
                    except Exception: pass
                    await asyncio.sleep(attempt*3); await check_connected()

        safe_del(fpath)
        if thumb_temp: safe_del(thumb)
        if upload_ok:
            ul_time = max(time.time()-ul_start, 0.1); ok_count += 1
            try: await status_msg.edit(done_txt(idx,total,new_name,sz_b,dl_spd,sz_mb/ul_time,
                                                dl_time,ul_time,ok_count,skip_count,caption),
                                       buttons=cancel_kb(), parse_mode='markdown')
            except Exception: pass
        else:
            skip_count += 1
            try: await status_msg.edit(skip_txt(idx,total,mid,'UL failed: '+upload_err,ok_count,skip_count),
                                       buttons=cancel_kb(), parse_mode='markdown')
            except Exception: pass

    await status_msg.edit(final_txt(total,ok_count,skip_count,time.time()-run_start),
                          buttons=main_kb(), parse_mode='markdown')


# =============================================================
#  DELETE ENGINE
# =============================================================

async def run_delete(uid, source, msg_ids, status_msg):
    total = len(msg_ids); deleted = 0; failed = 0
    if not await check_connected():
        await status_msg.edit('Not connected.', buttons=main_kb(), parse_mode='markdown'); return
    for idx, mid in enumerate(msg_ids, 1):
        if cancel_flags.get(uid):
            cancel_flags.pop(uid, None)
            await status_msg.edit('Cancelled.\nDeleted:'+str(deleted)+'  Failed:'+str(failed),
                                  buttons=main_kb(), parse_mode='markdown'); return
        try:
            await status_msg.edit('Deleting ['+str(idx)+'/'+str(total)+'] id:'+str(mid)+'\n'
                'Deleted:'+str(deleted)+'  Failed:'+str(failed), buttons=cancel_kb(), parse_mode='markdown')
        except Exception: pass
        try:
            await user_client.delete_messages(source, mid); deleted += 1
        except FloodWaitError as fe:
            await asyncio.sleep(fe.seconds+2)
            try: await user_client.delete_messages(source, mid); deleted += 1
            except Exception: failed += 1
        except Exception: failed += 1
        await asyncio.sleep(0.3)
    await status_msg.edit('Delete done!\nTotal:'+str(total)+'  Deleted:'+str(deleted)+'  Failed:'+str(failed),
                          buttons=main_kb(), parse_mode='markdown')


# =============================================================
#  KEYBOARDS
# =============================================================

def main_kb():
    return [
        [Button.inline('Login',         b'login'),  Button.inline('Settings',      b'show')],
        [Button.inline('Set Source',    b'src'),    Button.inline('Set Target',    b'tgt')],
        [Button.inline('Caption',       b'cap'),    Button.inline('Rename Tag',    b'ren')],
        [Button.inline('Replace Words', b'rep'),    Button.inline('Remove Words',  b'rem')],
        [Button.inline('Thumbnail',     b'thu'),    Button.inline('Del Thumb',     b'thu_del')],
        [Button.inline('DL Streams',    b'dlc'),    Button.inline('Reset All',     b'rst')],
        [Button.inline('Upload 1',      b'up1'),    Button.inline('Upload Batch',  b'upN')],
        [Button.inline('Delete 1',      b'del1'),   Button.inline('Delete Batch',  b'delN')],
        [Button.inline('Send Message',  b'sendmsg')],
    ]

def cancel_kb(): return [[Button.inline('Cancel', b'cancel')]]
def back_kb():   return [[Button.inline('Back',   b'back')]]

bot = make_client(BOT_SESSION, receive_updates=True)


# =============================================================
#  COMMANDS & HANDLERS (same logic, all platforms)
# =============================================================

@bot.on(events.NewMessage(pattern='/start'))
@bot.on(events.NewMessage(pattern='/menu'))
async def cmd_start(e):
    if not e.is_private: return
    await e.respond(settings_text(), buttons=main_kb(), parse_mode='markdown')

@bot.on(events.CallbackQuery())
async def on_cb(e):
    uid = e.sender_id; data = e.data.decode(); s = load_s()
    if data in ('back','show'): await e.edit(settings_text(), buttons=main_kb(), parse_mode='markdown'); return
    if data == 'cancel':
        cancel_flags[uid] = True; user_states.pop(uid,None)
        try: await e.edit('Cancelling after current file...')
        except Exception: pass
        return
    if data == 'login':
        if user_client and user_client.is_connected():
            try:
                me = await user_client.get_me()
                txt = 'Logged in: '+me.first_name+' (@'+(me.username or 'N/A')+')\nReady.'
            except Exception: txt = 'Session error.'
        else: txt = 'Not connected.'
        await e.edit(txt, buttons=back_kb(), parse_mode='markdown'); return
    if data == 'src':
        user_states[uid] = 'set_src'
        await e.edit('Set Source\n\nSend a t.me link or numeric chat ID:', buttons=back_kb()); return
    if data == 'tgt':
        user_states[uid] = 'set_tgt'
        await e.edit('Set Target\n\nSend numeric chat ID (e.g. -1001234567890):', buttons=back_kb()); return
    if data == 'cap':
        user_states[uid] = 'set_cap'
        await e.edit('Caption\n\nCurrent: `'+cap_label(s)+'`\n\nSend caption. Supports {filename} {size}.\nNONE=original | EMPTY=no caption',
                     buttons=back_kb(), parse_mode='markdown'); return
    if data == 'ren':
        user_states[uid] = 'set_ren'
        await e.edit('Rename Tag\n\nCurrent: `'+(s['rename_tag'] or 'none')+'`\n\nSend tag or NONE:',
                     buttons=back_kb(), parse_mode='markdown'); return
    if data == 'rep':
        user_states[uid] = 'set_rep'
        rw = '\n'.join('  '+k+' -> '+v for k,v in s['replace_words'].items()) or '  none'
        await e.edit('Replace Words\n\n'+rw+'\n\nSend old : new | RESET | DONE', buttons=back_kb(), parse_mode='markdown'); return
    if data == 'rem':
        user_states[uid] = 'set_rem'
        cur = ', '.join(s['remove_words']) if s['remove_words'] else 'none'
        await e.edit('Remove Words\n\nCurrent: '+cur+'\n\nComma-separated | RESET | DONE', buttons=back_kb()); return
    if data == 'thu': user_states[uid] = 'set_thu'; await e.edit('Set Thumbnail\n\nSend a photo:', buttons=back_kb()); return
    if data == 'thu_del': s['thumbnail'] = None; save_s(s); await e.edit('Thumbnail removed.', buttons=main_kb()); return
    if data == 'dlc':
        user_states[uid] = 'set_dlc'
        await e.edit('DL Streams\n\nCurrent: `'+str(s['dl_connections'])+'`\n\nSend 1-8:', buttons=back_kb(), parse_mode='markdown'); return
    if data == 'rst': save_s(DEFAULT_SETTINGS.copy()); await e.edit('Settings reset.', buttons=main_kb()); return
    if data == 'up1':
        if not await check_connected(): await e.edit('Not connected.', buttons=main_kb()); return
        if not s['target_chat']:        await e.edit('Target not set.', buttons=main_kb()); return
        user_states[uid] = {'step':'link1'}; await e.edit('Upload Single\n\nSend the media link:', buttons=cancel_kb()); return
    if data == 'upN':
        if not await check_connected(): await e.edit('Not connected.', buttons=main_kb()); return
        if not s['target_chat']:        await e.edit('Target not set.', buttons=main_kb()); return
        user_states[uid] = {'step':'linkN'}; await e.edit('Upload Batch\n\nSend the FIRST media link:', buttons=cancel_kb()); return
    if data == 'del1':
        if not await check_connected(): await e.edit('Not connected.', buttons=main_kb()); return
        user_states[uid] = {'step':'del1'}; await e.edit('Delete Single\n\nSend the message link:', buttons=back_kb()); return
    if data == 'delN':
        if not await check_connected(): await e.edit('Not connected.', buttons=main_kb()); return
        user_states[uid] = {'step':'delN'}; await e.edit('Delete Batch\n\nSend the FIRST message link:', buttons=back_kb()); return
    if data == 'sendmsg':
        if not await check_connected(): await e.edit('Not connected.', buttons=main_kb()); return
        if not s['target_chat']:        await e.edit('Target not set.', buttons=main_kb()); return
        user_states[uid] = {'step':'sendmsg'}
        await e.edit('Send Message\n\nTarget: `'+str(s['target_chat'])+'`\n\nSend your message:',
                     buttons=back_kb(), parse_mode='markdown'); return

@bot.on(events.NewMessage())
async def on_msg(e):
    if not e.is_private: return
    uid = e.sender_id; state = user_states.get(uid); text = (e.raw_text or '').strip(); s = load_s()
    if state is None and 't.me/' in text:
        cid, mid = parse_link(text)
        if cid and mid:
            if not await check_connected(): await e.respond('Not connected.'); return
            if not s['target_chat']:        await e.respond('Target not set. Use /start.'); return
            cancel_flags[uid] = False
            st = await e.respond('Fetching id:'+str(mid)+'...', buttons=cancel_kb())
            await run_transfer(uid, cid, [mid], st)
        return
    if state is None: return
    if state == 'set_src':
        if 't.me/' in text:
            cid, _ = parse_link(text)
            if not cid: await e.respond('Bad link.'); return
            s['source_chat'] = cid
        else:
            try: s['source_chat'] = int(text)
            except ValueError: await e.respond('Must be a number.'); return
        save_s(s); user_states.pop(uid,None)
        await e.respond('Source: `'+str(s['source_chat'])+'`', buttons=main_kb(), parse_mode='markdown')
    elif state == 'set_tgt':
        try: s['target_chat'] = int(text)
        except ValueError: await e.respond('Must be numeric, e.g. -1001234567890'); return
        save_s(s); user_states.pop(uid,None)
        await e.respond('Target: `'+str(s['target_chat'])+'`', buttons=main_kb(), parse_mode='markdown')
    elif state == 'set_cap':
        if text.upper() == 'NONE': s['caption'] = ''
        elif text.upper() == 'EMPTY': s['caption'] = ' '
        else: s['caption'] = text
        save_s(s); user_states.pop(uid,None)
        await e.respond('Caption: `'+cap_label(s)+'`', buttons=main_kb(), parse_mode='markdown')
    elif state == 'set_ren':
        s['rename_tag'] = '' if text.upper() == 'NONE' else text
        save_s(s); user_states.pop(uid,None)
        await e.respond('Tag: `'+(s['rename_tag'] or 'removed')+'`', buttons=main_kb(), parse_mode='markdown')
    elif state == 'set_rep':
        t = text.upper()
        if t == 'RESET': s['replace_words'] = {}; save_s(s); user_states.pop(uid,None); await e.respond('Cleared.', buttons=main_kb())
        elif t == 'DONE': user_states.pop(uid,None); await e.respond('Saved.', buttons=main_kb())
        elif ':' in text:
            old, new = text.split(':',1); old,new = old.strip(),new.strip()
            if old:
                s['replace_words'][old] = new; save_s(s)
                cur = '\n'.join('  '+k+' -> '+v for k,v in s['replace_words'].items())
                await e.respond('Added.\n'+cur+'\n\nMore, DONE, or RESET')
        else: await e.respond('Format: old : new')
    elif state == 'set_rem':
        t = text.upper()
        if t == 'RESET': s['remove_words'] = []; save_s(s); user_states.pop(uid,None); await e.respond('Cleared.', buttons=main_kb())
        elif t == 'DONE': user_states.pop(uid,None); await e.respond('Saved.', buttons=main_kb())
        else:
            words = [w.strip() for w in text.split(',') if w.strip()]
            s['remove_words'] = list(set(s['remove_words']+words)); save_s(s)
            await e.respond('Added: '+', '.join(s['remove_words'])+'\n\nMore, DONE, or RESET')
    elif state == 'set_thu':
        if e.photo:
            p = await e.download_media(file=os.path.join(DOWNLOAD_DIR,'thumbnail.jpg'))
            s['thumbnail'] = p; save_s(s); user_states.pop(uid,None)
            await e.respond('Thumbnail saved.', buttons=main_kb())
        else: await e.respond('Send a photo.')
    elif state == 'set_dlc':
        try:
            n = max(1,min(8,int(text))); s['dl_connections'] = n; save_s(s); user_states.pop(uid,None)
            await e.respond('DL streams set to '+str(n)+'.', buttons=main_kb())
        except ValueError: await e.respond('Send a number 1-8.')
    elif isinstance(state, dict):
        step = state.get('step')
        if step == 'link1':
            cid,mid = parse_link(text)
            if not cid or not mid: await e.respond('Bad link.'); return
            if not s['source_chat']: s['source_chat'] = cid; save_s(s)
            user_states.pop(uid,None); cancel_flags[uid] = False
            st = await e.respond('Fetching id:'+str(mid)+'...', buttons=cancel_kb())
            await run_transfer(uid, cid, [mid], st)
        elif step == 'linkN':
            cid,mid = parse_link(text)
            if not cid or not mid: await e.respond('Bad link.'); return
            if not s['source_chat']: s['source_chat'] = cid; save_s(s)
            user_states[uid] = {'step':'countN','cid':cid,'start':mid}
            await e.respond('Start id:'+str(mid)+'\nHow many messages?')
        elif step == 'countN':
            try:
                count = int(text)
                if count < 1 or count > 500: raise ValueError
            except ValueError: await e.respond('Send a number 1-500.'); return
            cid = state['cid']; start = state['start']
            user_states.pop(uid,None); cancel_flags[uid] = False
            st = await e.respond('Batch: '+str(count)+' files\nIDs: '+str(start)+' to '+str(start+count-1), buttons=cancel_kb())
            await run_transfer(uid, cid, list(range(start, start+count)), st)
        elif step == 'del1':
            cid,mid = parse_link(text)
            if not cid or not mid: await e.respond('Bad link.'); return
            user_states.pop(uid,None); cancel_flags[uid] = False
            st = await e.respond('Deleting '+str(mid)+'...', buttons=cancel_kb())
            await run_delete(uid, cid, [mid], st)
        elif step == 'delN':
            cid,mid = parse_link(text)
            if not cid or not mid: await e.respond('Bad link.'); return
            user_states[uid] = {'step':'delCount','cid':cid,'start':mid}
            await e.respond('Start id:'+str(mid)+'\nHow many to delete?')
        elif step == 'delCount':
            try:
                count = int(text)
                if count < 1 or count > 500: raise ValueError
            except ValueError: await e.respond('Send a number 1-500.'); return
            cid = state['cid']; start = state['start']
            user_states.pop(uid,None); cancel_flags[uid] = False
            st = await e.respond('Deleting '+str(count)+'...', buttons=cancel_kb())
            await run_delete(uid, cid, list(range(start, start+count)), st)
        elif step == 'sendmsg':
            try: target = int(s['target_chat'])
            except Exception: await e.respond('Invalid target.'); return
            if not text: await e.respond('Empty message.'); return
            user_states.pop(uid,None)
            try:
                await user_client.send_message(target, text, parse_mode='markdown')
                await e.respond('Sent!', buttons=main_kb())
            except Exception as ex:
                await e.respond('Failed: '+str(ex)[:150], buttons=main_kb())


# =============================================================
#  STARTUP
# =============================================================

async def main():
    global user_client
    print('=' * 55)
    print('  TELEGRAM RE-UPLOAD BOT  —  Render.com')
    print('=' * 55)

    if not API_ID or not API_HASH or not BOT_TOKEN:
        print('[ERROR] Set API_ID, API_HASH, BOT_TOKEN as environment variables in Render Dashboard.')
        return

    # On Render, session is stored as base64 env var SESSION_STRING
    # and written to file on startup
    session_b64 = os.environ.get('SESSION_STRING', '')
    if session_b64:
        import base64
        sf = USER_SESSION + '.session'
        with open(sf, 'wb') as fh:
            fh.write(base64.b64decode(session_b64))
        print('[OK] Session loaded from SESSION_STRING env var')
    elif not os.path.exists(USER_SESSION + '.session'):
        print('[ERROR] No session found.')
        print('→ Run login.py locally, then encode session:')
        print('  python encode_session.py')
        print('→ Paste the output as SESSION_STRING in Render env vars')
        return

    try:
        uc = make_client(USER_SESSION)
        await uc.connect()
        if not await uc.is_user_authorized():
            await uc.disconnect()
            print('[ERROR] Session expired. Re-run login.py locally.'); return
        user_client = uc
        me = await uc.get_me()
        print(f'[OK] User  : {me.first_name} (@{me.username or "N/A"})')
        print(f'[OK] DL    : {DL_CONNECTIONS} parallel streams × 512 KB chunks')
        print(f'[OK] UL    : 512 KB chunks (forced)')
    except Exception as ex:
        print(f'[ERROR] {ex}'); return

    try:
        await bot.start(bot_token=BOT_TOKEN)
        me = await bot.get_me()
        print(f'[OK] Bot   : @{me.username}')
        print(f'\n✅ Bot is running on Render.com!')
        print(f'   Open Telegram → @{me.username} → /start\n')
    except Exception as ex:
        print(f'[ERROR] Bot: {ex}'); return

    try:
        await bot.run_until_disconnected()
    except Exception as ex: print(f'[ERROR] {ex}')
    finally:
        for c in (bot, user_client):
            try:
                if c: await c.disconnect()
            except Exception: pass

if __name__ == '__main__':
    asyncio.run(main())
