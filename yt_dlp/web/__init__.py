"""
Web-based GUI for yt-dlp.

Local:   python -m yt_dlp --web-gui
Render:  set start command to  python -m yt_dlp --web-gui
         Render injects PORT automatically; files are served for in-browser download.

Zero extra dependencies -- uses only Python stdlib + yt-dlp's own YoutubeDL class.
"""

import json
import mimetypes
import os
import pathlib
import queue
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from ..YoutubeDL import YoutubeDL

_STATIC_DIR = pathlib.Path(__file__).parent / 'static'

_FORMATS = {
    'Best (default)': 'bestvideo+bestaudio/best',
    '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
    '720p': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
    '480p': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
    'Audio only (MP3)': 'bestaudio/best',
    'Audio only (M4A)': 'bestaudio[ext=m4a]/bestaudio/best',
}

# Files land here; override with DOWNLOAD_DIR env var
_DOWNLOADS_DIR = pathlib.Path(os.environ.get('DOWNLOAD_DIR', 'downloads'))

# ── shared state (all access under _lock) ────────────────────────────────────
_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_subscribers: list[queue.Queue] = []


def _broadcast(job: dict) -> None:
    with _lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(dict(job))
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def _add_job(url: str, fmt_label: str) -> str:
    jid = uuid.uuid4().hex[:8]
    job = {
        'id': jid, 'url': url, 'format': fmt_label,
        'status': 'Waiting', 'progress': 0,
        'speed': '', 'eta': '', 'error': '', 'filename': '',
    }
    with _lock:
        _jobs[jid] = job
    _broadcast(job)
    return jid


def _update_job(jid: str, **kw) -> None:
    with _lock:
        job = _jobs.get(jid)
        if job:
            job.update(kw)
            snapshot = dict(job)
    if job:
        _broadcast(snapshot)


# ── download logic ────────────────────────────────────────────────────────────
class _NullLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


_active_jid: str | None = None


def _run_job(jid: str) -> None:
    global _active_jid
    _active_jid = jid

    with _lock:
        job = dict(_jobs[jid])

    fmt_label = job['format']
    fmt = _FORMATS.get(fmt_label, 'bestvideo+bestaudio/best')
    _DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    outtmpl = str(_DOWNLOADS_DIR / '%(title)s.%(ext)s')

    params: dict = {
        'format': fmt,
        'outtmpl': outtmpl,
        'progress_hooks': [lambda d, j=jid: _progress_hook(j, d)],
        # post_hooks receives the final filepath after all postprocessing,
        # covering both fresh downloads and already-existing files
        'post_hooks': [lambda fp, j=jid: _update_job(j, filename=pathlib.Path(fp).name)],
        'quiet': True,
        'no_warnings': True,
        'logger': _NullLogger(),
    }

    if fmt_label == 'Audio only (MP3)':
        params['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]

    _update_job(jid, status='Downloading', progress=0)
    try:
        with YoutubeDL(params) as ydl:
            ydl.download([job['url']])

        _update_job(jid, status='Done', progress=100)
    except Exception as exc:
        _update_job(jid, status='Error', error=str(exc)[:300])

    _active_jid = None


def _progress_hook(jid: str, d: dict) -> None:
    status = d.get('status')
    if status == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        downloaded = d.get('downloaded_bytes') or 0
        pct = round(downloaded / total * 100, 1) if total else 0
        speed = d.get('speed')
        eta = d.get('eta')
        _update_job(
            jid,
            progress=pct,
            speed=f'{speed / 1_048_576:.1f} MB/s' if speed else '',
            eta=f'{eta}s' if eta is not None else '',
        )


def _start_pending() -> None:
    def runner():
        with _lock:
            pending = [jid for jid, j in _jobs.items() if j['status'] == 'Waiting']
        for jid in pending:
            _update_job(jid, status='Queued')
        for jid in pending:
            _run_job(jid)

    threading.Thread(target=runner, daemon=True).start()


# ── HTTP request handler ──────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence per-request logging

    def _send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_send(self, data: dict) -> None:
        line = f'data: {json.dumps(data)}\n\n'.encode()
        self.wfile.write(line)
        self.wfile.flush()

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == '/':
            body = (_STATIC_DIR / 'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == '/api/jobs':
            with _lock:
                self._send_json(list(_jobs.values()))

        elif path == '/api/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()

            q: queue.Queue = queue.Queue(maxsize=256)
            with _lock:
                _subscribers.append(q)
                snapshot = list(_jobs.values())

            for job in snapshot:
                self._sse_send(job)

            try:
                while True:
                    try:
                        self._sse_send(q.get(timeout=20))
                    except queue.Empty:
                        self.wfile.write(b': ping\n\n')
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _lock:
                    try:
                        _subscribers.remove(q)
                    except ValueError:
                        pass

        elif path.startswith('/files/'):
            # Serve a completed download; prevent path traversal
            safe_name = pathlib.Path(unquote(path[len('/files/'):])).name
            fpath = _DOWNLOADS_DIR / safe_name
            if not fpath.exists() or not fpath.is_file():
                self.send_error(404)
                return
            mime = mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(fpath.stat().st_size))
            self.send_header('Content-Disposition', f'attachment; filename="{safe_name}"')
            self.end_headers()
            with open(fpath, 'rb') as f:
                while chunk := f.read(65536):
                    self.wfile.write(chunk)

        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}

        if path == '/api/queue':
            url = (payload.get('url') or '').strip()
            if not url:
                return self._send_json({'error': 'url is required'}, 400)
            jid = _add_job(
                url=url,
                fmt_label=payload.get('format', 'Best (default)'),
            )
            self._send_json({'id': jid})

        elif path == '/api/start':
            _start_pending()
            self._send_json({'ok': True})

        elif path == '/api/clear':
            with _lock:
                done_ids = [jid for jid, j in _jobs.items() if j['status'] in ('Done', 'Error')]
                for jid in done_ids:
                    del _jobs[jid]
            self._send_json({'ok': True})

        else:
            self.send_error(404)


# ── entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    port = int(os.environ.get('PORT', 8080))
    is_local = 'PORT' not in os.environ

    _DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(('0.0.0.0', port), _Handler)
    print(f'yt-dlp web GUI running at http://localhost:{port}  (Ctrl+C to stop)')

    if is_local:
        webbrowser.open(f'http://localhost:{port}')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
