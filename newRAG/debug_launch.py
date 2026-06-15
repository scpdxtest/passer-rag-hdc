"""
Instrumented launcher for ingest_corpus.py.

Captures stdout + stderr to a timestamped log file, installs hooks that
expose silent thread deaths (the daemon-thread crash class), and dumps
all thread stacks on demand (SIGUSR1) — so the "app silently dies" pattern
turns into a real, debuggable traceback.

Usage::

    python3 debug_launch.py                  # server mode (Flask on $INGEST_PORT)
    python3 debug_launch.py --cli ...        # passes through to ingest_corpus CLI

Live tail::

    tail -F ingestion_debug_*.log

On-demand thread dump (run from a second shell while ingestion is going)::

    kill -USR1 $(pgrep -f debug_launch.py)
"""
import faulthandler
import os
import runpy
import signal
import sys
import threading
import time
import traceback
from pathlib import Path


HERE = Path(__file__).resolve().parent
TS   = time.strftime("%Y%m%d-%H%M%S")
LOG  = HERE / f"ingestion_debug_{TS}.log"


class _Tee:
    """Mirror writes to the real stream AND a log file. Line-buffered so
    `tail -F` shows progress in real time."""
    def __init__(self, *streams):
        self.streams = streams
    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass
    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass
    def fileno(self):
        # Some libraries (subprocess, logging) require a real fd; expose
        # the underlying terminal fd, not the log file.
        return self.streams[0].fileno()


def _install_hooks(log_f):
    # faulthandler: dump Python tracebacks on C-level crashes (segfault,
    # SIGABRT, SIGFPE, SIGBUS). Also registers SIGUSR1 to dump *all*
    # thread stacks on demand — useful when ingestion appears to hang.
    faulthandler.enable(file=log_f)
    try:
        faulthandler.register(signal.SIGUSR1, file=log_f,
                              all_threads=True, chain=False)
    except (ValueError, RuntimeError):
        pass   # SIGUSR1 unavailable on this OS

    # threading.excepthook: surface unhandled exceptions from any thread.
    # Without this, a crashed daemon thread leaves the main process happy
    # while the worker is dead — exactly the "silently dies" symptom.
    def _thread_crash(args):
        msg = (f"\n*** UNCAUGHT EXCEPTION IN THREAD "
               f"{args.thread.name!r} ({args.exc_type.__name__}) ***\n")
        sys.stderr.write(msg)
        traceback.print_exception(args.exc_type, args.exc_value,
                                  args.exc_traceback, file=sys.stderr)
        sys.stderr.flush()
    threading.excepthook = _thread_crash

    # sys.excepthook: matching coverage for the main thread.
    def _main_crash(exc_type, exc_value, exc_tb):
        sys.stderr.write(f"\n*** UNCAUGHT EXCEPTION IN MAIN "
                         f"({exc_type.__name__}) ***\n")
        traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
        sys.stderr.flush()
    sys.excepthook = _main_crash


def _rss_mb():
    """Resident set size of THIS process, in MB. Portable across mac/linux
    via /proc when available, falling back to resource.getrusage."""
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) / 1024
    except FileNotFoundError:
        pass
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS: bytes. Linux: kilobytes.
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


def _watchdog(stop_evt, log_f):
    """Every 30 s, print alive + thread count + RSS. If RSS grows
    monotonically by hundreds of MB per tick, you have a leak and the
    process will eventually be OOM-killed by the kernel — which leaves
    no Python traceback. Catch that pattern by watching the log timestamps."""
    rss_peak = 0.0
    while not stop_evt.wait(30):
        names = [t.name for t in threading.enumerate() if t.is_alive()]
        thread_list = ', '.join(n for n in names if n != 'MainThread')[:80]
        try:
            rss = _rss_mb()
            rss_peak = max(rss_peak, rss)
            rss_str = f"RSS={rss:>6.1f} MB (peak {rss_peak:.1f})"
        except Exception as e:
            rss_str = f"RSS=? ({e})"
        msg = (f"[watchdog {time.strftime('%H:%M:%S')}] "
               f"main + {len(names)-1} threads | {rss_str} | "
               f"{thread_list}\n")
        log_f.write(msg)
        log_f.flush()
        sys.__stdout__.write(msg)
        sys.__stdout__.flush()


def main():
    log_f = open(LOG, "w", buffering=1, encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log_f)
    sys.stderr = _Tee(sys.__stderr__, log_f)

    _install_hooks(log_f)

    print(f"[debug_launch] log file : {LOG}")
    print(f"[debug_launch] python   : {sys.version.split()[0]}")
    print(f"[debug_launch] argv     : {sys.argv}")
    print(f"[debug_launch] cwd      : {os.getcwd()}")
    print(f"[debug_launch] PID      : {os.getpid()}")
    print(f"[debug_launch] env keys : "
          f"OLLAMA_URL={os.environ.get('OLLAMA_URL','')!r}, "
          f"CHROMA_URL={os.environ.get('CHROMA_URL','')!r}, "
          f"INGEST_PORT={os.environ.get('INGEST_PORT','')!r}")
    print(f"[debug_launch] dump all thread stacks any time with: "
          f"kill -USR1 {os.getpid()}")
    print()

    stop_evt = threading.Event()
    wd = threading.Thread(target=_watchdog, args=(stop_evt, log_f),
                          name="debug_watchdog", daemon=True)
    wd.start()

    try:
        # runpy executes ingest_corpus.py exactly as `python ingest_corpus.py`
        # would, but with our hooks already in place.
        runpy.run_path(str(HERE / "ingest_corpus.py"), run_name="__main__")
    except SystemExit as e:
        print(f"\n[debug_launch] ingest_corpus exited cleanly: code={e.code}")
        raise
    except KeyboardInterrupt:
        print(f"\n[debug_launch] interrupted by user (^C)")
        raise
    except Exception as e:
        print(f"\n[debug_launch] ingest_corpus crashed: "
              f"{type(e).__name__}: {e}")
        traceback.print_exc()
        raise
    finally:
        stop_evt.set()
        log_f.flush()
        print(f"[debug_launch] log saved at {LOG}")


if __name__ == "__main__":
    main()
