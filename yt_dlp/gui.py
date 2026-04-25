import os
import pathlib
import threading
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox, ttk

from .YoutubeDL import YoutubeDL

_FORMATS = {
    'Best (default)': 'bestvideo+bestaudio/best',
    '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
    '720p': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
    '480p': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
    'Audio only (MP3)': 'bestaudio/best',
    'Audio only (M4A)': 'bestaudio[ext=m4a]/bestaudio/best',
}


@dataclass
class QueueItem:
    url: str
    status: str = 'Waiting'
    progress: float = 0.0
    speed: str = ''
    eta: str = ''
    error: str = ''


class YtDlpGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title('yt-dlp GUI')
        self.root.resizable(True, True)
        self.root.minsize(600, 400)

        self._items: list[QueueItem] = []
        self._active_item: QueueItem | None = None

        self._url_var = tk.StringVar()
        self._format_var = tk.StringVar(value='Best (default)')
        self._output_var = tk.StringVar(value=str(pathlib.Path.home() / 'Downloads'))
        self._progress_var = tk.DoubleVar(value=0.0)
        self._status_var = tk.StringVar(value='Ready')

        self._build_ui()

    def _build_ui(self) -> None:
        pad = {'padx': 8, 'pady': 4}

        # URL row
        url_frame = ttk.Frame(self.root)
        url_frame.pack(fill='x', **pad)
        ttk.Label(url_frame, text='URL:').pack(side='left')
        ttk.Entry(url_frame, textvariable=self._url_var).pack(side='left', fill='x', expand=True, padx=(4, 4))
        ttk.Button(url_frame, text='Add to Queue', command=self._add_to_queue).pack(side='left')

        # Options row
        opt_frame = ttk.Frame(self.root)
        opt_frame.pack(fill='x', **pad)
        ttk.Label(opt_frame, text='Format:').pack(side='left')
        ttk.Combobox(
            opt_frame, textvariable=self._format_var,
            values=list(_FORMATS.keys()), state='readonly', width=20,
        ).pack(side='left', padx=(4, 16))
        ttk.Label(opt_frame, text='Output:').pack(side='left')
        ttk.Entry(opt_frame, textvariable=self._output_var).pack(side='left', fill='x', expand=True, padx=(4, 4))
        ttk.Button(opt_frame, text='Browse…', command=self._browse_output).pack(side='left')

        # Queue
        queue_frame = ttk.LabelFrame(self.root, text='Queue')
        queue_frame.pack(fill='both', expand=True, **pad)

        cols = ('URL', 'Status', 'Progress')
        self._tree = ttk.Treeview(queue_frame, columns=cols, show='headings', height=8)
        self._tree.heading('URL', text='URL')
        self._tree.heading('Status', text='Status')
        self._tree.heading('Progress', text='Progress')
        self._tree.column('URL', stretch=True)
        self._tree.column('Status', width=110, stretch=False)
        self._tree.column('Progress', width=90, stretch=False)

        vsb = ttk.Scrollbar(queue_frame, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        # Progress row
        prog_frame = ttk.Frame(self.root)
        prog_frame.pack(fill='x', **pad)
        self._pbar = ttk.Progressbar(prog_frame, variable=self._progress_var, maximum=100)
        self._pbar.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self._speed_label = ttk.Label(prog_frame, textvariable=self._status_var, width=28, anchor='w')
        self._speed_label.pack(side='left')

        # Action row
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill='x', **pad)
        ttk.Button(btn_frame, text='Download All', command=self._start_downloads).pack(side='left')
        ttk.Button(btn_frame, text='Clear Completed', command=self._clear_completed).pack(side='left', padx=8)

        # Bind Enter key to add-to-queue
        self.root.bind('<Return>', lambda _e: self._add_to_queue())

    # ------------------------------------------------------------------ actions

    def _add_to_queue(self) -> None:
        url = self._url_var.get().strip()
        if not url:
            return
        item = QueueItem(url=url)
        self._items.append(item)
        self._tree.insert('', 'end', iid=id(item), values=(url, 'Waiting', ''))
        self._url_var.set('')

    def _browse_output(self) -> None:
        d = filedialog.askdirectory(initialdir=self._output_var.get())
        if d:
            self._output_var.set(d)

    def _start_downloads(self) -> None:
        pending = [it for it in self._items if it.status == 'Waiting']
        if not pending:
            messagebox.showinfo('yt-dlp', 'No waiting items in the queue.')
            return
        for item in pending:
            item.status = 'Queued'
            self._tree_update(item)
        threading.Thread(target=self._run_queue, args=(pending,), daemon=True).start()

    def _clear_completed(self) -> None:
        for item in list(self._items):
            if item.status in ('Done', 'Error'):
                self._items.remove(item)
                try:
                    self._tree.delete(id(item))
                except tk.TclError:
                    pass

    # ------------------------------------------------------------------ download loop

    def _run_queue(self, items: list[QueueItem]) -> None:
        for item in items:
            self._active_item = item
            self._download_one(item)
        self._active_item = None
        self.root.after(0, lambda: self._status_var.set('Done'))

    def _download_one(self, item: QueueItem) -> None:
        fmt_label = self._format_var.get()
        fmt = _FORMATS.get(fmt_label, 'bestvideo+bestaudio/best')
        out_dir = self._output_var.get()
        outtmpl = os.path.join(out_dir, '%(title)s.%(ext)s')

        params: dict = {
            'format': fmt,
            'outtmpl': outtmpl,
            'progress_hooks': [self._progress_hook],
            'quiet': True,
            'no_warnings': True,
        }

        if fmt_label == 'Audio only (MP3)':
            params['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]

        item.status = 'Downloading'
        item.progress = 0.0
        self.root.after(0, lambda: self._tree_update(item))

        try:
            with YoutubeDL(params) as ydl:
                ydl.download([item.url])
            item.status = 'Done'
            item.progress = 100.0
        except Exception as exc:
            item.status = 'Error'
            item.error = str(exc)

        self.root.after(0, lambda: self._finalize_item(item))

    def _progress_hook(self, d: dict) -> None:
        item = self._active_item
        if item is None:
            return

        status = d.get('status')
        if status == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes') or 0
            pct = (downloaded / total * 100) if total else 0.0
            speed = d.get('speed')
            eta = d.get('eta')

            speed_str = f'{speed / 1024 / 1024:.1f} MB/s' if speed else ''
            eta_str = f'ETA {eta}s' if eta is not None else ''
            label = '  '.join(filter(None, [speed_str, eta_str]))

            item.progress = pct
            item.speed = speed_str
            item.eta = eta_str

            self.root.after(0, lambda p=pct, l=label: self._update_progress_ui(item, p, l))

    def _update_progress_ui(self, item: QueueItem, pct: float, label: str) -> None:
        self._progress_var.set(pct)
        self._status_var.set(label)
        self._tree_update(item)

    def _finalize_item(self, item: QueueItem) -> None:
        self._tree_update(item)
        if item.status == 'Done':
            self._progress_var.set(100)
            self._status_var.set('Done')
        elif item.status == 'Error':
            self._progress_var.set(0)
            self._status_var.set(f'Error: {item.error[:40]}')

    def _tree_update(self, item: QueueItem) -> None:
        pct_str = f'{item.progress:.0f}%' if item.progress > 0 else ''
        try:
            self._tree.item(id(item), values=(item.url, item.status, pct_str))
        except tk.TclError:
            pass


def main() -> None:
    root = tk.Tk()
    YtDlpGUI(root)
    root.mainloop()
