#!/usr/bin/env python3
"""
cryptoV1.py - Scrolling crypto currency ticker tape (tkinter + yfinance)

Sibling of tickerV3.py (stocks), adapted for crypto pairs quoted by Yahoo Finance
(e.g. BTC-USD, ETH-USD). Same architecture: borderless always-on-top tape, background
fetch, efficient canvas scroll, YAML config, add/remove, dock top/bottom.

Key behaviours (aligned with tickerV3):
- % change vs previous close (period="5d" history)
- Quote dataclass, snapshot+reconcile on fetch, stale-data resilience
- Instant add/remove with N/A placeholders + force refresh
- Listbox chooser for remove, separate manual/hover pause
- Batch yf.download + parallel individual fallback
- Config: crypto.yaml (list or dict with "tickers")

Usage:
  python cryptoV1.py
  python cryptoV1.py --dock bottom --speed 1.5 --config crypto.yaml --interval 45

Requirements: yfinance pandas pyyaml (tkinter stdlib)
  pip install yfinance pyyaml pandas
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from tkinter import simpledialog, messagebox
import tkinter as tk
from typing import Optional

import pandas as pd
import yaml
import yfinance as yf


# ----------------------------- Logging -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s cryptoV1] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


# ----------------------------- Tunable Constants -----------------------------
DEFAULT_YAML = "crypto.yaml"
DEFAULT_DOCK = "top"
DEFAULT_TICKERS = ["BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD", "XRP-USD"]
WINDOW_HEIGHT = 30
FONT = ("Consolas", 11)
BG_COLOR = "#0a0a0a"
SCROLL_SPEED = 1.8          # pixels per animation frame
FRAME_INTERVAL_MS = 22      # ~45 fps
FETCH_INTERVAL_SEC = 60
SPACER_PX = 32
GREEN = "#00ff9f"
RED = "#ff6666"
GRAY = "#888888"
WHITE = "#cccccc"
LOADING_COLOR = "#ffcc66"
SEPARATOR = "•"
BOTTOM_TASKBAR_MARGIN = 40  # helps avoid Windows taskbar on bottom dock


def _normalize_symbol(raw: str) -> str:
    """Uppercase and light-normalize common crypto inputs to Yahoo symbols."""
    s = str(raw).strip().upper().replace(" ", "")
    if not s:
        return s
    # Bare bases (BTC, eth) → BTC-USD, ETH-USD
    if "-" not in s and "/" not in s and s.isalpha() and 2 <= len(s) <= 10:
        return f"{s}-USD"
    # BTC/USD or BTCUSDT-style → BTC-USD when clearly a pair
    if "/" in s:
        left, _, right = s.partition("/")
        if left and right:
            return f"{left}-{right}"
    if s.endswith("USDT") and len(s) > 4 and "-" not in s:
        return f"{s[:-4]}-USD"
    if s.endswith("USD") and "-" not in s and len(s) > 3:
        base = s[:-3]
        if base.isalpha():
            return f"{base}-USD"
    return s


def _display_symbol(symbol: str) -> str:
    """Shorter tape label: BTC-USD → BTC (keep full symbol if not *-USD)."""
    if symbol.endswith("-USD") and len(symbol) > 4:
        return symbol[:-4]
    return symbol


def _format_price(price: float) -> str:
    """Readable crypto pricing: more decimals for sub-dollar coins."""
    if price >= 1000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:.2f}"
    if price >= 0.01:
        return f"${price:.4f}"
    return f"${price:.6f}"


@dataclass
class Quote:
    """Quote for one crypto symbol (price + % change from prev close)."""
    symbol: str
    price: Optional[float]
    change_pct: Optional[float]


class CryptoTape:
    """Borderless always-on-top scrolling crypto ticker tape."""

    def __init__(
        self,
        root: tk.Tk,
        yaml_file: str = DEFAULT_YAML,
        dock_position: str | None = None,
        scroll_speed: float = SCROLL_SPEED,
        fetch_interval: int = FETCH_INTERVAL_SEC,
        window_height: int = WINDOW_HEIGHT,
    ):
        self.root = root
        self.yaml_file = yaml_file
        self._cli_dock = dock_position
        self.dock_position = dock_position or DEFAULT_DOCK
        self.scroll_speed = float(scroll_speed)
        self.fetch_interval = int(fetch_interval)
        self.window_height = int(window_height)

        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth() or 1920
        self.screen_width = max(800, int(sw))
        self.screen_height = self.root.winfo_screenheight() or 1080

        self.tickers: list[str] = list(DEFAULT_TICKERS)
        self.ticker_data: list[Quote] = []
        self.content_width: float = 0.0
        self.offset: float = 0.0
        self.manual_paused: bool = False
        self.hover_paused: bool = False
        self.last_update_ts: float | None = None
        self.pause_idx: int | None = None

        self.stop_event = threading.Event()
        self.force_refresh = threading.Event()
        self.data_queue: queue.Queue[list[Quote]] = queue.Queue(maxsize=8)
        self.data_lock = threading.RLock()

        self._setup_window()
        self._build_canvas()
        self._build_menu()
        self._bind_events()

        self.load_config()
        with self.data_lock:
            self.ticker_data = [Quote(s, None, None) for s in self.tickers]

        self.update_geometry()
        self._render_ticker_display()

        self._start_background_thread()
        self._schedule_queue_drain()
        self.animate()

    # --------------------------- Window & UI ---------------------------
    def _setup_window(self):
        self.root.title("Crypto Ticker V1")
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.configure(bg=BG_COLOR)
        self.root.protocol("WM_DELETE_WINDOW", self.exit_app)

    def _build_canvas(self):
        self.canvas = tk.Canvas(
            self.root,
            bg=BG_COLOR,
            highlightthickness=0,
            height=self.window_height,
        )
        self.canvas.pack(fill="both", expand=True)

    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Add Crypto", command=self.add_ticker)
        self.menu.add_command(label="Remove Crypto...", command=self.remove_ticker)
        self.menu.add_separator()
        self.menu.add_command(label="Refresh Now", command=self._force_refresh)
        self.menu.add_command(label="Pause Scroll", command=self._toggle_pause)
        self.menu.add_command(label="List Cryptos", command=self._show_ticker_list)
        self.menu.add_separator()
        self.menu.add_command(label="Dock to Top", command=lambda: self.set_dock_position("top"))
        self.menu.add_command(label="Dock to Bottom", command=lambda: self.set_dock_position("bottom"))
        self.menu.add_separator()
        self.menu.add_command(label="Reset to Defaults", command=self._reset_tickers)
        self.menu.add_separator()
        self.menu.add_command(label="Exit", command=self.exit_app)

        try:
            for i in range(self.menu.index("end") + 1):
                try:
                    lbl = self.menu.entrycget(i, "label")
                    if "Pause" in lbl or "Resume" in lbl:
                        self.pause_idx = i
                        break
                except Exception:
                    continue
        except Exception:
            self.pause_idx = 4

    def _bind_events(self):
        self.canvas.bind("<Button-3>", self.show_context_menu)
        self.root.bind("<Button-3>", self.show_context_menu)
        self.canvas.bind("<Enter>", lambda e: setattr(self, "hover_paused", True))
        self.canvas.bind("<Leave>", lambda e: setattr(self, "hover_paused", False))
        self.root.bind("<Escape>", lambda e: self.exit_app())
        self.root.bind("<space>", lambda e: self._toggle_pause())
        self.root.bind("<F5>", lambda e: self._force_refresh())

    # --------------------------- Config ---------------------------
    def load_config(self):
        """Load cryptos + dock from YAML. CLI dock takes precedence."""
        if not os.path.exists(self.yaml_file):
            return
        try:
            with open(self.yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            tickers_src: list | None = None
            if isinstance(data, list):
                tickers_src = data
            elif isinstance(data, dict):
                if isinstance(data.get("tickers"), list):
                    tickers_src = data["tickers"]
            if tickers_src:
                loaded: list[str] = []
                for t in tickers_src:
                    if t:
                        s = _normalize_symbol(str(t))
                        if s and s not in loaded:
                            loaded.append(s)
                if loaded:
                    self.tickers = loaded
            if isinstance(data, dict) and "dock_position" in data and not self._cli_dock:
                pos = str(data["dock_position"]).lower()
                if pos in ("top", "bottom"):
                    self.dock_position = pos
        except Exception as e:
            logging.warning(f"Config load warning ({self.yaml_file}): {e}")

    def save_config(self):
        try:
            with open(self.yaml_file, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    {
                        "tickers": self.tickers,
                        "dock_position": self.dock_position,
                    },
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                )
        except Exception as e:
            logging.error(f"Config save failed: {e}")

    def update_geometry(self):
        if self.dock_position == "top":
            y = 0
        else:
            y = max(0, self.screen_height - self.window_height - BOTTOM_TASKBAR_MARGIN)
        self.root.geometry(f"{self.screen_width}x{self.window_height}+0+{y}")
        self.root.update_idletasks()

    def set_dock_position(self, position: str):
        if position not in ("top", "bottom") or position == self.dock_position:
            return
        self.dock_position = position
        self.update_geometry()
        self.save_config()
        messagebox.showinfo("Crypto", f"Docked to {position}.", parent=self.root)

    # --------------------------- Data Fetch (background) ---------------------------
    def _compute_quote_from_history(self, hist: pd.DataFrame) -> tuple[Optional[float], Optional[float]]:
        """Latest price and % change vs previous close (or open fallback)."""
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None, None
        try:
            closes = hist["Close"].dropna()
            if len(closes) < 1:
                return None, None
            price = float(closes.iloc[-1])
            if len(closes) >= 2:
                prev = float(closes.iloc[-2])
                if prev != 0.0:
                    ch = round((price - prev) / prev * 100.0, 2)
                    return price, ch
            if "Open" in hist.columns:
                o = hist["Open"].iloc[0]
                if pd.notna(o) and float(o) != 0:
                    ch = round((price - float(o)) / float(o) * 100.0, 2)
                    return price, ch
            return price, 0.0
        except Exception as e:
            logging.debug(f"_compute_quote_from_history error: {e}")
            return None, None

    def _perform_fetch(self, tickers: list[str] | None = None) -> list[Quote]:
        """Return list of Quote in requested order (batch + parallel fallback + stale)."""
        if tickers is None:
            tickers = list(self.tickers)
        if not tickers:
            return []

        with self.data_lock:
            prev_map = {q.symbol: (q.price, q.change_pct) for q in self.ticker_data if q.price is not None}

        batch_df = None
        try:
            batch_df = yf.download(
                tickers=tickers,
                period="5d",
                progress=False,
                group_by="ticker",
                timeout=30,
                auto_adjust=False,
            )
        except Exception as e:
            logging.warning(f"yfinance batch download error: {e}")

        results: dict[str, tuple[Optional[float], Optional[float]]] = {}
        batch_success = 0
        still_missing: list[str] = []

        if batch_df is not None and not batch_df.empty:
            is_multi = isinstance(getattr(batch_df, "columns", None), pd.MultiIndex)
            for symbol in tickers:
                price = change = None
                try:
                    if is_multi:
                        if symbol in batch_df.columns.get_level_values(0):
                            sym_df = batch_df[symbol].dropna(how="all")
                        else:
                            sym_df = pd.DataFrame()
                    else:
                        sym_df = batch_df.dropna(how="all") if len(tickers) <= 1 else pd.DataFrame()

                    if not sym_df.empty:
                        price, change = self._compute_quote_from_history(sym_df)
                except Exception:
                    pass

                if price is not None:
                    results[symbol] = (price, change)
                    batch_success += 1
                else:
                    still_missing.append(symbol)

        individual_success = 0
        if still_missing:
            logging.info(
                f"Batch gave good data for {batch_success}/{len(tickers)} symbols. "
                f"Retrying {len(still_missing)} individually..."
            )
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _fetch_one(sym: str):
                try:
                    h = yf.Ticker(sym).history(period="5d")
                    p, c = self._compute_quote_from_history(h)
                    return sym, p, c
                except Exception:
                    return sym, None, None

            with ThreadPoolExecutor(max_workers=8) as ex:
                futures = {ex.submit(_fetch_one, s): s for s in still_missing}
                for fut in as_completed(futures):
                    sym, price, ch = fut.result()
                    if price is not None:
                        results[sym] = (price, ch)
                        individual_success += 1

        new_data: list[Quote] = []
        for symbol in tickers:
            if symbol in results:
                price, change = results[symbol]
            elif symbol in prev_map:
                price, change = prev_map[symbol]
            else:
                price = change = None
            new_data.append(Quote(symbol, price, change))

        still_na = [s for s in tickers if s not in results and s not in prev_map]
        if individual_success:
            logging.info(f"Individual retries succeeded for {individual_success} more symbols.")
        if still_na:
            logging.info(f"{len(still_na)} symbols still have no data this cycle (N/A or stale shown).")

        return new_data

    def fetch_data(self):
        """Background worker with interruptible sleep for force-refresh / exit."""
        while not self.stop_event.is_set():
            tickers_snapshot = list(self.tickers)
            data = self._perform_fetch(tickers_snapshot)
            try:
                self.data_queue.put_nowait(data)
            except queue.Full:
                pass

            waited = 0.0
            while waited < self.fetch_interval and not self.stop_event.is_set():
                time.sleep(0.2)
                waited += 0.2
                if self.force_refresh.is_set():
                    self.force_refresh.clear()
                    break

    def _start_background_thread(self):
        t = threading.Thread(target=self.fetch_data, daemon=True, name="CryptoFetchV1")
        t.start()

    # --------------------------- UI Update (main thread only) ---------------------------
    def _schedule_queue_drain(self):
        self._drain_queue()
        self.root.after(120, self._schedule_queue_drain)

    def _drain_queue(self):
        updated = False
        last_data: list[Quote] | None = None
        while True:
            try:
                last_data = self.data_queue.get_nowait()
                updated = True
            except queue.Empty:
                break
        if updated and last_data is not None:
            with self.data_lock:
                sym_to_q = {q.symbol: q for q in last_data}
                self.ticker_data = [
                    sym_to_q.get(s, Quote(s, None, None)) for s in self.tickers
                ]
                self.last_update_ts = time.time()
            self._render_ticker_display()

    def _render_ticker_display(self):
        """(Re)build tape items on data change or add/remove."""
        self.canvas.delete("all")
        self.content_width = 0.0
        self.offset = 0.0

        if not self.ticker_data:
            self.canvas.create_text(
                self.screen_width // 2,
                self.window_height // 2,
                text="No cryptos. Right-click → Add Crypto",
                font=FONT,
                fill=LOADING_COLOR,
                anchor="c",
            )
            return

        entries: list[tuple[str, str]] = []
        for q in self.ticker_data:
            label = _display_symbol(q.symbol)
            if q.price is None or q.change_pct is None:
                txt = f"{label}: N/A"
                fg = GRAY
            else:
                txt = f"{label}: {_format_price(q.price)} ({q.change_pct:+.2f}%)"
                fg = GREEN if q.change_pct >= 0 else RED
            entries.append((txt, fg))

        if not entries:
            return

        x = 0.0
        for txt, fg in entries:
            tid = self.canvas.create_text(
                x,
                self.window_height // 2,
                text=txt,
                font=FONT,
                fill=fg,
                anchor="w",
                tags=("ticker",),
            )
            b = self.canvas.bbox(tid)
            w = float(b[2] - b[0] + 1) if b else 70.0
            x += w + SPACER_PX

            sid = self.canvas.create_text(
                x,
                self.window_height // 2,
                text=SEPARATOR,
                font=FONT,
                fill=WHITE,
                anchor="w",
                tags=("ticker",),
            )
            b = self.canvas.bbox(sid)
            sw = float(b[2] - b[0] + 1) if b else 14.0
            x += sw + SPACER_PX

        # One copy of the tape. Restart only after it has fully left the left edge.
        self.content_width = x
        self.canvas.move("ticker", float(self.screen_width), 0.0)
        self.offset = 0.0

    # --------------------------- Animation ---------------------------
    def animate(self):
        if (
            self.manual_paused or self.hover_paused
            or self.content_width < 10.0
            or not self.ticker_data
            or not self.canvas.find_withtag("ticker")
        ):
            self.root.after(FRAME_INTERVAL_MS * 3, self.animate)
            return

        self.canvas.move("ticker", -self.scroll_speed, 0.0)
        self.offset -= self.scroll_speed

        # Reset only once the last glyph has gone off the left edge; then
        # place the tape just off the right edge again (no mid-screen jump).
        cycle = self.content_width + float(self.screen_width)
        if cycle > 0 and self.offset <= -cycle:
            self.canvas.move("ticker", cycle, 0.0)
            self.offset += cycle

        self.root.after(FRAME_INTERVAL_MS, self.animate)

    # --------------------------- User Actions ---------------------------
    def show_context_menu(self, event):
        try:
            self.menu.post(event.x_root, event.y_root)
        except Exception as e:
            logging.debug(f"Menu post error: {e}")

    def add_ticker(self):
        symbol = simpledialog.askstring(
            "Add Crypto",
            "Enter symbol (e.g. BTC, ETH-USD, SOL, DOGE-USD):",
            parent=self.root,
        )
        if not symbol:
            return
        symbol = _normalize_symbol(symbol)
        if not symbol:
            return
        if symbol in self.tickers:
            messagebox.showwarning("Crypto", f"{symbol} already tracked.", parent=self.root)
            return

        self.tickers.append(symbol)
        with self.data_lock:
            sym_to_q = {q.symbol: q for q in self.ticker_data}
            self.ticker_data = [sym_to_q.get(s, Quote(s, None, None)) for s in self.tickers]
        self.save_config()
        self._render_ticker_display()
        self.force_refresh.set()
        messagebox.showinfo("Crypto", f"Added {symbol}. Fetching price...", parent=self.root)

    def remove_ticker(self):
        if not self.tickers:
            messagebox.showinfo("Crypto", "No cryptos configured.", parent=self.root)
            return

        top = tk.Toplevel(self.root)
        top.title("Remove Crypto")
        top.resizable(False, False)
        top.transient(self.root)
        try:
            top.grab_set()
        except Exception:
            pass

        frm = tk.Frame(top, padx=12, pady=10)
        frm.pack(fill="both", expand=True)

        tk.Label(frm, text="Select a crypto to remove:").pack(anchor="w", pady=(0, 4))

        lb = tk.Listbox(
            frm,
            height=min(14, max(4, len(self.tickers))),
            width=28,
            exportselection=False,
            activestyle="dotbox",
        )
        for t in self.tickers:
            lb.insert(tk.END, t)
        lb.pack(fill="both", expand=True, pady=4)
        if self.tickers:
            lb.selection_set(0)
            lb.focus_set()

        def do_remove():
            sel = lb.curselection()
            if not sel:
                top.destroy()
                return
            sym = str(lb.get(sel[0]))
            if sym in self.tickers:
                self.tickers.remove(sym)
                with self.data_lock:
                    sym_to_q = {q.symbol: q for q in self.ticker_data}
                    self.ticker_data = [sym_to_q.get(s, Quote(s, None, None)) for s in self.tickers]
                self.save_config()
                self._render_ticker_display()
                self.force_refresh.set()
                messagebox.showinfo("Crypto", f"Removed {sym}.", parent=self.root)
            top.destroy()

        btn_row = tk.Frame(frm)
        btn_row.pack(pady=(6, 0))
        tk.Button(btn_row, text="Remove Selected", command=do_remove, fg="#a00").pack(side="left", padx=4)
        tk.Button(btn_row, text="Cancel", command=top.destroy).pack(side="left", padx=4)

        try:
            top.update_idletasks()
            x = self.root.winfo_x() + 120
            y = self.root.winfo_y() + 60
            top.geometry(f"+{x}+{y}")
        except Exception:
            pass

        top.wait_window()

    def _force_refresh(self):
        self.force_refresh.set()
        self.canvas.delete("all")
        self.canvas.create_text(
            self.screen_width // 2,
            self.window_height // 2,
            text="Refreshing...",
            font=FONT,
            fill=LOADING_COLOR,
            anchor="c",
        )

    def _toggle_pause(self):
        self.manual_paused = not self.manual_paused
        status = "PAUSED" if self.manual_paused else "RESUMED"
        self._update_pause_menu_label()

        self.canvas.delete("pause_hint")
        hint = self.canvas.create_text(
            12,
            self.window_height // 2,
            text=f"[{status}]",
            font=FONT,
            fill="#ffaa00",
            anchor="w",
            tags=("pause_hint", "ticker"),
        )
        self.root.after(1200, lambda: self.canvas.delete(hint) if self.canvas.winfo_exists() else None)

    def _update_pause_menu_label(self):
        if self.pause_idx is None:
            return
        try:
            label = "Resume Scroll" if self.manual_paused else "Pause Scroll"
            self.menu.entryconfig(self.pause_idx, label=label)
        except Exception as e:
            logging.debug(f"Could not update pause menu label: {e}")

    def _reset_tickers(self):
        defaults = list(DEFAULT_TICKERS)
        if self.tickers == defaults:
            messagebox.showinfo("Crypto", "Already at defaults.", parent=self.root)
            return
        self.tickers = defaults[:]
        with self.data_lock:
            self.ticker_data = [Quote(s, None, None) for s in self.tickers]
        self.save_config()
        self._render_ticker_display()
        self.force_refresh.set()
        messagebox.showinfo(
            "Crypto",
            "Reset to default cryptos (BTC, ETH, BNB, SOL, XRP).",
            parent=self.root,
        )

    def _show_ticker_list(self):
        if not self.tickers:
            msg = "No cryptos configured."
        else:
            lines = "\n".join(f"  • {t}" for t in self.tickers)
            msg = f"Tracking {len(self.tickers)} symbols:\n{lines}"
        if self.last_update_ts:
            import datetime as _dt
            dt = _dt.datetime.fromtimestamp(self.last_update_ts).strftime("%Y-%m-%d %H:%M:%S")
            msg += f"\n\nLast data update: {dt}"
        messagebox.showinfo("Current Cryptos", msg, parent=self.root)

    def exit_app(self):
        self.stop_event.set()
        self.force_refresh.set()
        try:
            self.root.destroy()
        except Exception:
            pass


# --------------------------- Entry Point ---------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Crypto Ticker Tape V1 - scrolling display for cryptocurrency prices"
    )
    parser.add_argument(
        "-c", "--config", default=DEFAULT_YAML, help="Path to crypto YAML config file"
    )
    parser.add_argument(
        "--dock", choices=["top", "bottom"], help="Force dock position (overrides saved config)"
    )
    parser.add_argument(
        "--speed", type=float, default=SCROLL_SPEED, help=f"Scroll speed px/frame (default {SCROLL_SPEED})"
    )
    parser.add_argument(
        "--interval", type=int, default=FETCH_INTERVAL_SEC, help=f"Fetch interval seconds (default {FETCH_INTERVAL_SEC})"
    )
    parser.add_argument(
        "--height", type=int, default=WINDOW_HEIGHT, help=f"Window height px (default {WINDOW_HEIGHT})"
    )
    args = parser.parse_args()

    root = tk.Tk()
    CryptoTape(
        root,
        yaml_file=args.config,
        dock_position=args.dock,
        scroll_speed=args.speed,
        fetch_interval=args.interval,
        window_height=args.height,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
