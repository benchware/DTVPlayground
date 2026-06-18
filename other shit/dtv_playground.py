#!/usr/bin/env python3
"""
DTV SDR Modulation Playground  —  MPEG-2 Transport Stream Edition
Accurate · Realistic · Stable

TX:  Python test-pattern / file  →  ffmpeg MPEG-2 TS encoder  →  channel relay (5005)
CH:  PythonChannelRelay  →  TS-aware byte corruption based on real link budget
RX:  corrupted TS datagrams  →  ffmpeg MPEG-2 decoder  →  authentic error concealment
     - '-ec deblock+favor_inter' produces real temporal smear & ghost artifacts
     - NO fake Python datamosh  —  the codec does it for real

Hardware: QSV → VAAPI → software fallback (auto-detected at launch)
Audio:    MP2 inside MPEG-TS stream, decoded by ffmpeg → aplay 48kHz stereo
"""
import sys, os, time, socket, threading, subprocess, io, math, random, queue, signal
import numpy as np
from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
    QComboBox, QPushButton, QFileDialog, QGroupBox, QCheckBox,
    QProgressBar, QLineEdit, QTabWidget, QScrollArea, QSplitter, QListWidget
)
from PyQt5.QtGui import QImage, QPixmap, QFont
from PyQt5.QtCore import QTimer, Qt, QThread, pyqtSignal
from PIL import Image, ImageDraw, ImageFont

sys.path.append('/home/hunter')
try:
    from dtv_simulation import dtv_simulation
    GRC_AVAILABLE = True
except Exception as e:
    print(f"[WARN] GRC unavailable: {e}")
    GRC_AVAILABLE = False


# ─────────────────────────────────────────────────────────────
#  HARDWARE ACCELERATION PROBE
#  Tries QSV → VAAPI → software, caches result at startup.
# ─────────────────────────────────────────────────────────────
HW_ENC        = 'mpeg2video'   # ffmpeg -vcodec value for encoder
HW_DEC_FLAGS  = []             # ffmpeg flags prepended to decoder cmd
HW_ENC_FLAGS  = []             # extra flags for encoder (VAAPI device etc.)
VAAPI_VF      = []             # VAAPI pixel-format upload filter flags

NULL_TS_PKT   = bytes([0x47, 0x1F, 0xFF, 0x10]) + bytes(184)  # null TS padding
TS_CHUNK_PKTS = 7
TS_CHUNK      = TS_CHUNK_PKTS * 188   # 1316 bytes per UDP datagram


def probe_hw():
    global HW_ENC, HW_DEC_FLAGS, HW_ENC_FLAGS, VAAPI_VF

    def _try(cmd):
        try:
            r = subprocess.run(cmd, timeout=8, capture_output=True)
            return r.returncode == 0
        except Exception:
            return False

    # Intel QSV
    if _try(['ffmpeg', '-y', '-loglevel', 'quiet',
             '-f', 'lavfi', '-i', 'color=black:s=320x240:r=25:d=0.1',
             '-vcodec', 'mpeg2_qsv', '-frames:v', '1', '-f', 'null', '-']):
        HW_ENC       = 'mpeg2_qsv'
        HW_DEC_FLAGS = []  # Keep decoder on stable software decoding
        print("[HW] Intel QSV  (mpeg2_qsv)")
        return

    # VAAPI
    dev = '/dev/dri/renderD128'
    if os.path.exists(dev):
        if _try(['ffmpeg', '-y', '-loglevel', 'quiet',
                 '-vaapi_device', dev,
                 '-f', 'lavfi', '-i', 'color=black:s=320x240:r=25:d=0.1',
                 '-vf', 'format=nv12,hwupload',
                 '-vcodec', 'mpeg2_vaapi', '-frames:v', '1', '-f', 'null', '-']):
            HW_ENC       = 'mpeg2_vaapi'
            HW_ENC_FLAGS = ['-vaapi_device', dev]
            VAAPI_VF     = ['format=nv12,hwupload,']   # prepend to -vf
            HW_DEC_FLAGS = []  # Keep decoder on stable software decoding
            print("[HW] VAAPI  (mpeg2_vaapi)")
            return

    print("[HW] Software mpeg2video")


probe_hw()   # run at module load


# ─────────────────────────────────────────────────────────────
#  DTV RESOLUTION TABLE
#  (label, width, height, interlaced_default, fps_default)
# ─────────────────────────────────────────────────────────────
DTV_RESOLUTIONS = [
    ("480i  SDTV NTSC  (720x480)",    720,  480,  True,  29),
    ("576i  SDTV PAL   (720x576)",    720,  576,  True,  25),
    ("720p  HDTV       (1280x720)",  1280,  720,  False, 60),
    ("1080i HDTV       (1920x1080)", 1920, 1080,  True,  30),
    ("1080p Full HD    (1920x1080)", 1920, 1080,  False, 25),
    ("240p  Low Bitrate (426x240)",   426,  240,  False, 30),
]


# ─────────────────────────────────────────────────────────────
#  PYTHON CHANNEL RELAY  (TS-aware corruption)
# ─────────────────────────────────────────────────────────────
class PythonChannelRelay(threading.Thread):
    """
    Listens on TX_PORT for MPEG-TS datagrams.
    Applies TS-aware byte corruption based on link-budget lock%.
    Preserves 0x47 sync bytes so the decoder can still find packet boundaries.
    Forwards corrupted datagrams to RX_PORT.
    """
    TX_PORT = 5005
    RX_PORT = 5002

    def __init__(self, get_lock_fn):
        super().__init__(daemon=True)
        self.get_lock = get_lock_fn
        self.running  = True

        self.rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rx_sock.bind(('127.0.0.1', self.TX_PORT))
        self.rx_sock.settimeout(0.05)

        self.tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @staticmethod
    def _ber(lock_pct: int) -> float:
        """Cliff-effect BER from lock%: near-zero above threshold, catastrophic below."""
        if lock_pct >= 95: return 0.0
        if lock_pct <= 0:   return 1.0
        t = (95 - lock_pct) / 95.0
        return min(t ** 4.0, 0.95)

    @staticmethod
    def _corrupt_ts(data: bytes, ber: float) -> bytes:
        """
        Corrupt MPEG-2 TS payload bytes.
        Strategy:
          - Never touch sync bytes (0x47 at byte 0 of each 188-byte packet)
          - Corrupt adaptation/payload bytes with per-bit BER
          - Occasionally corrupt the PUSI byte (triggers I-frame resync)
        This causes the real MPEG-2 decoder to produce authentic artifacts:
          motion-vector smear, temporal ghosting, concealment blocks.
        """
        arr = bytearray(data)
        per_byte_err = min(ber * 10.0, 0.98)
        for off in range(0, len(arr), 188):
            if off >= len(arr) or arr[off] != 0x47:
                continue
            # Header: bytes 1-3 (leave mostly intact — only corrupt occasionally)
            if random.random() < ber * 0.3:
                arr[off + 1] ^= random.randint(1, 3)   # flip low PID bits
            # Payload: bytes 4-187 (primary corruption target)
            for i in range(off + 4, min(off + 188, len(arr))):
                if random.random() < per_byte_err:
                    arr[i] ^= 1 << random.randrange(8)
        return bytes(arr)

    def run(self):
        while self.running:
            try:
                data, _ = self.rx_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception:
                continue

            lock = self.get_lock()
            ber  = self._ber(lock)

            # Drop datagrams less aggressively so we can see block artifacts down to 45% lock
            drop = max(0.0, (ber - 0.05) * 1.5)
            if random.random() < drop:
                continue

            if ber > 5e-4:
                data = self._corrupt_ts(data, ber)

            try:
                self.tx_sock.sendto(data, ('127.0.0.1', self.RX_PORT))
            except Exception:
                pass

    def stop(self):
        self.running = False
        try: self.rx_sock.close()
        except Exception: pass


# ─────────────────────────────────────────────────────────────
#  MPEG-TS ENCODER THREAD
# ─────────────────────────────────────────────────────────────
class MpegTsEncoderThread(QThread):
    """
    Two modes:
    • File   – ffmpeg reads file directly, re-encodes to MPEG-2 TS
    • Synth  – raw RGB24 frames piped from Python test-pattern generator
               + lavfi sine-wave audio

    Interlacing: '-flags +ildct+ilme -alternate_scan 1 -vf setfield=tff'
    Hardware:    mpeg2_qsv / mpeg2_vaapi / mpeg2video (auto-detected)
    """
    def __init__(self, video_file: str, width: int, height: int,
                 fps: int, bitrate_kbps: int, interlaced: bool,
                 audio_codec: str = 'mp2', seek_seconds: float = 0.0):
        super().__init__()
        self.video_file   = video_file
        self.width        = width
        self.height       = height
        self.fps          = fps
        self.bitrate_kbps = bitrate_kbps
        self.interlaced   = interlaced
        self.audio_codec  = audio_codec
        self.seek_seconds = seek_seconds
        self.running      = True
        self.proc         = None
        self.err_log      = subprocess.DEVNULL

    def push_frame(self, rgb24: bytes):
        """Synthetic mode: push one raw RGB24 frame."""
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write(rgb24)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def _has_audio(self):
        if not self.video_file or not os.path.exists(self.video_file):
            return False
        cmd = ['ffprobe', '-show_streams', '-select_streams', 'a', '-loglevel', 'error', self.video_file]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            return "codec_type=audio" in r.stdout
        except Exception:
            return False

    def _build_cmd(self):
        gop = max(int(self.fps), 12)
        bv  = f'{self.bitrate_kbps}k'
        mv  = f'{int(self.bitrate_kbps * 1.5)}k'
        buf = f'{int(self.bitrate_kbps * 4)}k'
        codec = HW_ENC

        # Video filter chain
        vf = []
        if VAAPI_VF:
            vf += VAAPI_VF
        if self.video_file:
            vf += [f'scale={self.width}:{self.height}:flags=bicubic',
                   f'fps={self.fps}']
        else:
            # Synthetic mode: input is always 720x480, scale to target resolution in ffmpeg
            vf += [f'scale={self.width}:{self.height}:flags=fast_bilinear']
            
        if self.interlaced:
            vf += ['setfield=tff']
        vf_str = ','.join(vf) if vf else None

        # Interlaced DCT/motion-estimation for software encoder only
        il_flags = (['-flags', '+ildct+ilme', '-alternate_scan', '1']
                    if self.interlaced and codec == 'mpeg2video' else [])

        common_enc = (
            ['-vcodec', codec, '-b:v', bv, '-maxrate', mv, '-bufsize', buf,
             '-g', str(gop), '-bf', '2']
            + il_flags
            + ['-acodec', self.audio_codec, '-b:a', '192k', '-ar', '48000', '-ac', '2',
               '-f', 'mpegts', 'pipe:1']
        )

        seek_opt = []
        if self.seek_seconds > 0:
            seek_opt = ['-ss', f'{self.seek_seconds:.2f}']

        if self.video_file:
            has_aud = self._has_audio()
            if has_aud:
                cmd = (['ffmpeg', '-y', '-loglevel', 'error']
                       + seek_opt
                       + ['-stream_loop', '-1', '-re',
                          '-i', self.video_file]
                       + HW_ENC_FLAGS
                       + (['-vf', vf_str] if vf_str else [])
                       + ['-map', '0:v:0', '-map', '0:a:0']
                       + common_enc)
            else:
                # Inject silent audio track so the MPEG-TS stream always has audio
                cmd = (['ffmpeg', '-y', '-loglevel', 'error']
                       + seek_opt
                       + ['-stream_loop', '-1', '-re',
                          '-i', self.video_file,
                          '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000']
                       + HW_ENC_FLAGS
                       + (['-vf', vf_str] if vf_str else [])
                       + ['-map', '0:v:0', '-map', '1:a:0']
                       + common_enc)
            return cmd, subprocess.DEVNULL
        else:
            # Synthetic: raw video from stdin (always 720x480) + lavfi audio
            audio_src = 'sine=frequency=1000:sample_rate=48000:d=99999'
            cmd = (['ffmpeg', '-y', '-loglevel', 'error',
                    '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                    '-s', '720x480',
                    '-r', str(self.fps), '-i', 'pipe:0',
                    '-f', 'lavfi', '-i', audio_src]
                   + HW_ENC_FLAGS
                   + (['-vf', vf_str] if vf_str else [])
                   + common_enc)
            return cmd, subprocess.PIPE

    def run(self):
        cmd, stdin_val = self._build_cmd()
        print(f"[ENC] {HW_ENC} {self.width}x{self.height}@{self.fps} "
              f"{'interlaced' if self.interlaced else 'progressive'} "
              f"{self.bitrate_kbps}kbps")
              
        err_log_path = '/home/hunter/.gemini/antigravity/brain/b1720ff7-ba1e-4e91-ac3e-9526e6a3cfff/scratch/encoder_stderr.log'
        try:
            self.err_log = open(err_log_path, 'w')
        except Exception:
            self.err_log = subprocess.DEVNULL

        try:
            self.proc = subprocess.Popen(
                cmd, stdin=stdin_val,
                stdout=subprocess.PIPE, stderr=self.err_log
            )
        except Exception as e:
            print(f"[ENC] Failed: {e}")
            if self.err_log != subprocess.DEVNULL:
                self.err_log.close()
            return

        tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        pkts_sent = 0

        while self.running:
            try:
                chunk = self.proc.stdout.read(TS_CHUNK)
            except Exception:
                break
            if not chunk:
                if not self.running:
                    break
                time.sleep(0.001)
                continue
            # Pad short reads with null TS packets
            if len(chunk) < TS_CHUNK:
                n_null = (TS_CHUNK - len(chunk)) // 188
                chunk += (NULL_TS_PKT * n_null)[: TS_CHUNK - len(chunk)]
            try:
                tx_sock.sendto(chunk, ('127.0.0.1', PythonChannelRelay.TX_PORT))
                pkts_sent += 1
            except Exception:
                pass

        tx_sock.close()
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                pass
        if self.err_log != subprocess.DEVNULL:
            try: self.err_log.close()
            except Exception: pass
        print(f"[ENC] Stopped. {pkts_sent} datagrams sent.")

    def stop(self):
        self.running = False
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
            except Exception:
                pass
        self.wait()


# ─────────────────────────────────────────────────────────────
#  MPEG-TS DECODER THREAD
# ─────────────────────────────────────────────────────────────
class MpegTsDecoderThread(QThread):
    """
    Receives corrupted MPEG-TS datagrams from channel relay.
    Runs TWO parallel ffmpeg decoders sharing the same TS stream:
      - Video decoder: outputs raw RGB24 frames scaled to 480x270 (via frame_ready signal)
      - Audio decoder: outputs PCM S16LE 48kHz stereo, played via aplay in the background
    """
    frame_ready = pyqtSignal(bytes)   # raw RGB24 480x270

    def __init__(self, width: int, height: int, fps: int,
                 deinterlace: bool = False, port: int = 5002):
        super().__init__()
        self.width       = width
        self.height      = height
        self.fps         = fps
        self.deinterlace = deinterlace
        self.port        = port
        self.running     = True
        # Decoder always outputs exactly 480x270 to offload GUI thread scaling
        self.frame_size  = 480 * 270 * 3

        self.video_proc  = None
        self.audio_proc  = None
        self.aplay_proc  = None
        self.sock        = None

        # Thread-safe control states
        self.vol_level   = 0.70
        self.lock_level  = 0
        self.recording_file = None
        self.respawn_lock = threading.Lock()

        # Separate queues so slow video decoder can't starve audio and vice-versa
        self.vq = queue.Queue(maxsize=96)
        self.aq = queue.Queue(maxsize=96)

    def set_deinterlace(self, val: bool):
        self.deinterlace = val
        self._respawn('video_proc', self.video_proc)

    # ── proc construction ──────────────────────────────────────
    def _video_cmd(self):
        vf = []
        # If hardware decoding is active, download to system memory nv12 format first
        if HW_DEC_FLAGS:
            vf.append('hwdownload')
            vf.append('format=nv12')
        if self.deinterlace:
            vf.append('yadif=mode=0:parity=auto:deint=all')
        # Scale directly to screen size to offload GUI CPU
        vf.append('scale=480:270:flags=bicubic')
        vf.append('format=rgb24')
        
        return (
            ['ffmpeg', '-y', '-loglevel', 'error']
            + HW_DEC_FLAGS
            + ['-fflags', 'nobuffer',
               '-err_detect', 'ignore_err',
               '-ec', 'deblock+favor_inter',
               '-analyzeduration', '2000000',
               '-probesize', '2000000',
               '-f', 'mpegts', '-i', 'pipe:0',
               '-map', '0:v?',
               '-vf', ','.join(vf),
               '-f', 'rawvideo', '-pix_fmt', 'rgb24',
               'pipe:1']
        )

    def _audio_cmd(self):
        return [
            'ffmpeg', '-y', '-loglevel', 'error',
            '-fflags', 'nobuffer',
            '-err_detect', 'ignore_err',
            '-analyzeduration', '2000000',
            '-probesize', '2000000',
            '-f', 'mpegts', '-i', 'pipe:0',
            '-map', '0:a?',
            '-f', 's16le', '-acodec', 'pcm_s16le',
            '-ac', '2', '-ar', '48000',
            'pipe:1'
        ]

    def _spawn(self, cmd, name):
        log_path = f'/home/hunter/.gemini/antigravity/brain/b1720ff7-ba1e-4e91-ac3e-9526e6a3cfff/scratch/decoder_{name}_stderr.log'
        try:
            log_file = open(log_path, 'w')
        except Exception:
            log_file = subprocess.DEVNULL
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=log_file
        )
        proc.err_log = log_file
        return proc

    def _start_video(self):
        try:
            self.video_proc = self._spawn(self._video_cmd(), 'video')
            return True
        except Exception as e:
            print(f"[DEC-V] start failed: {e}")
            return False

    def _start_audio(self):
        try:
            self.audio_proc = self._spawn(self._audio_cmd(), 'audio')
            return True
        except Exception as e:
            print(f"[DEC-A] start failed: {e}")
            return False

    # ── writer threads (queue → proc stdin) ───────────────────
    def _write_loop(self, q: queue.Queue, proc_name: str):
        while self.running:
            try:
                data = q.get(timeout=0.1)
            except queue.Empty:
                continue
            proc = getattr(self, proc_name)
            if proc and proc.stdin:
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    if self.running:
                        print(f"[{proc_name}] pipe broken — restarting")
                        self._respawn(proc_name, proc)

    def _respawn(self, proc_name: str, old_proc=None):
        with self.respawn_lock:
            current = getattr(self, proc_name)
            if old_proc is not None and current is not old_proc:
                return
            if current:
                try:
                    current.terminate()
                    current.wait(timeout=1)
                    if hasattr(current, 'err_log') and current.err_log != subprocess.DEVNULL:
                        current.err_log.close()
                except Exception:
                    pass
            if proc_name == 'video_proc':
                if self._start_video():
                    threading.Thread(target=self._read_video, daemon=True).start()
            else:
                if self._start_audio():
                    threading.Thread(target=self._read_audio, daemon=True).start()

    # ── reader threads (proc stdout → signal/aplay) ───────────
    def _read_video(self):
        proc = self.video_proc
        while self.running and proc is self.video_proc:
            try:
                raw = proc.stdout.read(self.frame_size)
                if raw and len(raw) == self.frame_size:
                    self.frame_ready.emit(raw)
                elif not raw:
                    break
            except Exception:
                break

    def _read_audio(self):
        CHUNK = 9600  # 50 ms @ 48kHz stereo S16LE
        proc = self.audio_proc
        while self.running and proc is self.audio_proc:
            try:
                raw = proc.stdout.read(CHUNK)
                if raw:
                    self._write_audio_to_aplay(raw)
                elif not raw:
                    break
            except Exception:
                break

    def _write_audio_to_aplay(self, pcm: bytes):
        # Background thread aplay playback with digital volume & squelch
        if self.lock_level < 15:
            return  # squelch
        vol = self.vol_level
        if vol < 0.01:
            return
        if vol != 1.0:
            try:
                arr  = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                arr *= vol
                pcm  = arr.astype(np.int16).tobytes()
            except Exception:
                pass

        # Ensure audio player is running
        if not self.aplay_proc or self.aplay_proc.poll() is not None:
            try:
                if sys.platform.startswith('linux'):
                    try:
                        self.aplay_proc = subprocess.Popen(
                            ['aplay', '-t', 'raw', '-f', 'S16_LE', '-c', '2', '-r', '48000'],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                        )
                    except Exception:
                        self.aplay_proc = None
                
                if not self.aplay_proc:
                    # ffplay fallback: works on Windows, macOS, Linux
                    self.aplay_proc = subprocess.Popen(
                        ['ffplay', '-loglevel', 'quiet', '-f', 's16le', '-ac', '2', '-ar', '48000', '-nodisp', '-autoexit', '-'],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
            except Exception as e:
                print(f"[AUDIO] failed to start background player: {e}")
                return

        if self.aplay_proc and self.aplay_proc.stdin:
            try:
                self.aplay_proc.stdin.write(pcm)
                self.aplay_proc.stdin.flush()
            except Exception:
                pass

    # ── main loop ─────────────────────────────────────────────
    def run(self):
        # Create and bind socket inside background thread with retry loop to avoid port reuse crashes
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(0.05)
        
        bound = False
        for i in range(10):
            if not self.running:
                return
            try:
                self.sock.bind(('127.0.0.1', self.port))
                bound = True
                break
            except Exception as e:
                print(f"[DEC] Bind to port {self.port} failed (attempt {i+1}/10), retrying: {e}")
                time.sleep(0.1)

        if not bound or not self.running:
            print(f"[DEC] Could not bind or thread stopped.")
            return

        if not self._start_video():
            return
        if not self.running:
            return
        self._start_audio()

        for name, q in [('video_proc', self.vq), ('audio_proc', self.aq)]:
            threading.Thread(target=self._write_loop,
                             args=(q, name), daemon=True).start()
        threading.Thread(target=self._read_video, daemon=True).start()
        threading.Thread(target=self._read_audio, daemon=True).start()

        print(f"[DEC] Listening on :{self.port}  {self.width}x{self.height}")

        while self.running:
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception:
                break

            # Raw MPEG-TS recording
            if self.recording_file:
                try:
                    self.recording_file.write(data)
                except Exception:
                    pass

            # Feed both decoders; drop if queue full (extra corruption = more artifacts)
            try: self.vq.put_nowait(data)
            except queue.Full: pass
            try: self.aq.put_nowait(data)
            except queue.Full: pass

    def stop(self):
        self.running = False
        for proc in [self.video_proc, self.audio_proc, self.aplay_proc]:
            if proc:
                try:
                    proc.stdin.close()
                    proc.terminate()
                    proc.wait(timeout=1)
                except Exception:
                    pass
                if hasattr(proc, 'err_log') and proc.err_log != subprocess.DEVNULL:
                    try: proc.err_log.close()
                    except Exception: pass
        if self.sock:
            try: self.sock.close()
            except Exception: pass
        self.wait()


# ─────────────────────────────────────────────────────────────
#  VIDEO READER THREAD  (TX preview display only)
# ─────────────────────────────────────────────────────────────
class VideoReaderThread(QThread):
    frame_ready = pyqtSignal(bytes)

    def __init__(self, proc, frame_size):
        super().__init__()
        self.proc       = proc
        self.frame_size = frame_size
        self.running    = True

    def run(self):
        while self.running and self.proc:
            try:
                data = self.proc.stdout.read(self.frame_size)
                if data and len(data) == self.frame_size and self.running:
                    self.frame_ready.emit(data)
                elif not data:
                    break
            except Exception:
                break

    def stop(self):
        self.running = False
        self.wait()


# ─────────────────────────────────────────────────────────────
#  CLICKABLE IMAGE LABEL
# ─────────────────────────────────────────────────────────────
class ClickableLabel(QLabel):
    double_clicked = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Double-click for Fullscreen")
        self.setScaledContents(True)
        
    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()


# ─────────────────────────────────────────────────────────────
#  FULLSCREEN DIALOG WINDOW
# ─────────────────────────────────────────────────────────────
class FullscreenWindow(QLabel):
    closed = pyqtSignal()

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setScaledContents(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Double-click or press Escape to exit Fullscreen")
        self.showFullScreen()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()

    def mouseDoubleClickEvent(self, event):
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


# ─────────────────────────────────────────────────────────────
#  MAIN APPLICATION
# ─────────────────────────────────────────────────────────────
class DtvPlaygroundApp(QMainWindow):

    _lock_pct = 0
    _effective_snr = 0.0

    def get_lock_pct(self):  return self._lock_pct

    def __init__(self):
        super().__init__()
        self.fullscreen_view = None
        self.fullscreen_target = None
        self.setWindowTitle("DTV SDR Playground  —  MPEG-2 TS")
        self.resize(1360, 880)
        self._apply_stylesheet()

        # ── GRC (reference/visual DSP only) ───────────────────
        self.gr_tb = None
        if GRC_AVAILABLE:
            try:
                print("[GRC] Starting DSP reference block...")
                self.gr_tb = dtv_simulation()
                self.gr_tb.start()
            except Exception as e:
                print(f"[GRC] Failed: {e}")

        # ── State ─────────────────────────────────────────────
        self.video_file      = ''
        self.playlist        = []
        self.seek_seconds    = 0.0
        self.playback_paused = False
        self.video_duration  = 0.0
        self.video_width     = 720
        self.video_height    = 480
        self.fps             = 29
        self.interlaced      = False
        self.deinterlace_rx  = False
        self.bitrate_kbps    = 3000
        self.quality_override = -1
        self.audio_codec     = 'mp2'

        self.channel_name    = 'Antigravity HD'
        self.channel_number  = '7.1'

        self.last_frame_recv = 0.0
        self.last_decoded    = None   # PIL Image
        self.recording       = False
        self.recording_frames = []
        self.current_rx_frame = Image.new('RGB', (480, 270), (0, 0, 0))
        self.timer_record = QTimer()
        self.timer_record.timeout.connect(self._record_tick)
        self.frame_count     = 0

        # TX test-pattern state
        self.ball_x = 360;  self.ball_y = 240
        self.ball_dx = 6;   self.ball_dy = 4
        self.text_x  = 720
        self.bounced = False
        self.tx_field = 0   # interlace field parity

        # Sub-processes for TX display
        self.preview_proc   = None
        self.preview_thread = None
        self.latest_preview = None
        self.preview_w = 480
        self.preview_h = 270

        # Encoder / Decoder / Relay
        self.mpeg_encoder = None
        self.mpeg_decoder = None
        self.channel_relay = None

        # aplay
        self.aplay_proc = None

        # ── Build UI ──────────────────────────────────────────
        self.init_ui()
        self.on_impairment_changed()

        # ── Channel Relay ─────────────────────────────────────
        self.channel_relay = PythonChannelRelay(get_lock_fn=self.get_lock_pct)
        self.channel_relay.start()
        print(f"[RELAY] TX:{PythonChannelRelay.TX_PORT} → RX:{PythonChannelRelay.RX_PORT}")

        # ── MPEG-TS Encoder ───────────────────────────────────
        self._start_encoder()

        # ── MPEG-TS Decoder ───────────────────────────────────
        self._start_decoder()

        # ── aplay for audio output ────────────────────────────
        self._start_aplay()

        # ── Timers ────────────────────────────────────────────
        self.timer_tx = QTimer()
        self.timer_tx.timeout.connect(self.media_loop)
        self.timer_tx.start(int(1000 / max(self.fps, 1)))

        self.timer_metrics = QTimer()
        self.timer_metrics.timeout.connect(self.update_metrics)
        self.timer_metrics.start(400)

        self.timer_rx = QTimer()
        self.timer_rx.timeout.connect(self.update_rx_display)
        self.timer_rx.start(80)

        self._tx_pkt_count = 0
        self._rx_pkt_count = 0

    # ── Startup helpers ────────────────────────────────────────
    def _start_encoder(self):
        if self.mpeg_encoder:
            self.mpeg_encoder.stop()
            self.mpeg_encoder = None
        
        seek_sec = getattr(self, 'seek_seconds', 0.0)
        self.mpeg_encoder = MpegTsEncoderThread(
            video_file=self.video_file,
            width=self.video_width, height=self.video_height,
            fps=self.fps, bitrate_kbps=self.bitrate_kbps,
            interlaced=self.interlaced,
            audio_codec=self.audio_codec,
            seek_seconds=seek_sec
        )
        self.mpeg_encoder.start()
        # TX display preview (small ffmpeg for UI only)
        self._start_preview()

    def _start_decoder(self):
        if self.mpeg_decoder:
            self.mpeg_decoder.stop()
            self.mpeg_decoder = None
        self.last_decoded    = None
        self.last_frame_recv = 0.0
        self.decoder_start_time = time.time()
        self.mpeg_decoder = MpegTsDecoderThread(
            width=self.video_width, height=self.video_height,
            fps=self.fps, deinterlace=self.deinterlace_rx
        )
        is_unmuted = self.btn_mute_rx.isChecked() if hasattr(self, 'btn_mute_rx') else True
        self.mpeg_decoder.vol_level = (self.vol_slider.value() if is_unmuted else 0) / 100.0
        self.mpeg_decoder.lock_level = self._lock_pct
        if hasattr(self, '_rec_file') and self._rec_file:
            self.mpeg_decoder.recording_file = self._rec_file
        self.mpeg_decoder.frame_ready.connect(self.on_mpeg_frame)
        self.mpeg_decoder.start()

    def _start_preview(self):
        """Small ffmpeg at display resolution for TX screen preview."""
        if self.preview_proc:
            try:
                self.preview_proc.terminate()
                self.preview_proc.wait(timeout=1)
            except Exception: pass
            self.preview_proc = None
        if self.preview_thread:
            self.preview_thread.stop()
            self.preview_thread = None
        self.latest_preview = None

        if not self.video_file:
            return   # synthetic: generated in media_loop directly

        audio_out = []
        if hasattr(self, 'btn_mute_preview') and self.btn_mute_preview.isChecked():
            audio_out = ['-map', '0:a?', '-f', 'pulse', 'default']

        cmd = ['ffmpeg', '-y', '-loglevel', 'error',
               '-stream_loop', '-1', '-re',
               '-i', self.video_file,
               '-vf', f'scale={self.preview_w}:{self.preview_h}:flags=fast_bilinear',
               '-r', str(min(self.fps, 30)),
               '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'] + audio_out
        try:
            self.preview_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.preview_thread = VideoReaderThread(
                self.preview_proc, self.preview_w * self.preview_h * 3)
            self.preview_thread.frame_ready.connect(self._on_preview_frame)
            self.preview_thread.start()
        except Exception as e:
            print(f"[PREVIEW] failed: {e}")

    def _on_preview_frame(self, data: bytes):
        self.latest_preview = data

    def _start_aplay(self):
        pass

    def _restart_pipeline(self):
        """Stop+restart encoder and decoder when params change."""
        if self.mpeg_encoder:
            self.mpeg_encoder.stop()
            self.mpeg_encoder = None
        self._start_decoder()
        time.sleep(0.15)
        self._start_encoder()
        self.timer_tx.setInterval(int(1000 / max(self.fps, 1)))
        # Reset TX pattern state
        self.ball_x = self.video_width  // 2
        self.ball_y = self.video_height // 2
        self.text_x = self.video_width

    # ─────────────────────────────────────────────────────────────
    #  STYLESHEET
    # ─────────────────────────────────────────────────────────────
    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #0d1117; color: #e6edf3;
                                   font-family: 'DejaVu Sans', sans-serif; }
            QGroupBox { border: 1px solid #30363d; border-radius: 6px;
                        margin-top: 8px; font-weight: bold; color: #79c0ff;
                        padding-top: 4px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
            QLabel { font-size: 12px; color: #c9d1d9; }
            QPushButton { background-color: #21262d; border: 1px solid #30363d;
                          border-radius: 5px; padding: 6px 10px; font-weight: bold; color: #c9d1d9; }
            QPushButton:hover { background-color: #30363d; border-color: #58a6ff; }
            QPushButton:pressed { background-color: #161b22; }
            QSlider::groove:horizontal { border: 1px solid #30363d; height: 5px;
                                         background: #21262d; border-radius: 2px; }
            QSlider::handle:horizontal { background: #58a6ff; width: 13px;
                                         margin: -4px 0; border-radius: 6px; }
            QSlider::handle:horizontal:hover { background: #79c0ff; }
            QComboBox { background-color: #21262d; border: 1px solid #30363d;
                        border-radius: 4px; padding: 4px 8px; color: #c9d1d9; }
            QComboBox QAbstractItemView { background: #21262d; selection-background-color: #1f6feb; }
            QProgressBar { border: 1px solid #30363d; border-radius: 4px; text-align: center;
                           background-color: #21262d; color: #c9d1d9; font-weight: bold; }
            QProgressBar::chunk { background-color: #238636; border-radius: 3px; }
            QLineEdit { background-color: #21262d; border: 1px solid #30363d;
                        border-radius: 4px; padding: 4px; color: #c9d1d9; }
            QCheckBox { color: #c9d1d9; }
            QCheckBox::indicator { width: 14px; height: 14px;
                                   border: 1px solid #58a6ff; border-radius: 3px; }
            QCheckBox::indicator:checked { background-color: #1f6feb; }
            QTabWidget::pane { border: 1px solid #30363d; background: #0d1117; }
            QTabBar::tab { background: #21262d; color: #8b949e; padding: 8px 14px;
                           border-top-left-radius: 4px; border-top-right-radius: 4px;
                           margin-right: 2px; font-size: 11px; }
            QTabBar::tab:selected { background: #1f6feb; color: #fff; }
            QTabBar::tab:hover { background: #30363d; color: #c9d1d9; }
            QScrollArea { border: none; }
        """)

    # ─────────────────────────────────────────────────────────────
    #  UI CONSTRUCTION
    # ─────────────────────────────────────────────────────────────
    def init_ui(self):
        root = QWidget()
        vbox = QVBoxLayout(); vbox.setContentsMargins(8, 6, 8, 4); vbox.setSpacing(4)

        # Header
        hdr = QHBoxLayout()
        ttl = QLabel("DTV SDR Playground  —  MPEG-2 TS")
        ttl.setFont(QFont("DejaVu Sans", 15, QFont.Bold))
        ttl.setStyleSheet("color: #58a6ff; padding: 2px;")
        hdr.addWidget(ttl); hdr.addStretch()
        hdr.addWidget(QLabel("CH:")); self.num_input = QLineEdit("7.1")
        self.num_input.setFixedWidth(45); self.num_input.textChanged.connect(self.on_guide_changed)
        hdr.addWidget(self.num_input); hdr.addWidget(QLabel("Name:"))
        self.name_input = QLineEdit("Antigravity HD"); self.name_input.setFixedWidth(130)
        self.name_input.textChanged.connect(self.on_guide_changed)
        hdr.addWidget(self.name_input)
        vbox.addLayout(hdr)

        # Splitter
        spl = QSplitter(Qt.Horizontal)
        lw = QWidget(); lw.setMaximumWidth(440); lw.setMinimumWidth(360)
        lv = QVBoxLayout(); lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self._build_tabs())
        lw.setLayout(lv); spl.addWidget(lw)

        rw = QWidget()
        rv = QVBoxLayout(); rv.setContentsMargins(4, 0, 0, 0)
        rv.addWidget(self._build_screens())
        rv.addWidget(self._build_player_controls())
        rv.addWidget(self._build_signal_strip())
        rw.setLayout(rv); spl.addWidget(rw)
        spl.setStretchFactor(0, 0); spl.setStretchFactor(1, 1)
        vbox.addWidget(spl, 1)

        self.metrics_lbl = QLabel("Initializing MPEG-2 TS pipeline...")
        self.metrics_lbl.setStyleSheet(
            "background:#161b22; padding:4px 8px; border-top:1px solid #30363d;"
            " font-family:monospace; font-size:11px; color:#8b949e;")
        vbox.addWidget(self.metrics_lbl)

        root.setLayout(vbox)
        self.setCentralWidget(root)

    def _build_tabs(self):
        t = QTabWidget()
        t.addTab(self._tab_tuner(),   "Tuner")
        t.addTab(self._tab_prop(),    "Prop & Skins")
        t.addTab(self._tab_media(),   "Media & Quality")
        t.addTab(self._tab_guide(),   "Help / Guide")
        return t

    # ── TAB 1: Tuner ──────────────────────────────────────────
    def _tab_tuner(self):
        w = QWidget(); sc = QScrollArea(); sc.setWidgetResizable(True)
        inn = QWidget(); v = QVBoxLayout(); v.setSpacing(6)

        # Presets
        pb = QGroupBox("Quick Presets"); pv = QVBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems([
            "-- Select Preset --",
            "1. ATSC 480i Local (UHF, 10 km, Set-top)",
            "2. DVB-T 720p Rooftop (UHF, 25 km)",
            "3. DVB-S2 1080i Satellite (Ku-Band, 38000 km)",
            "4. J.83B 1080p Cable (UHF, 2 km)",
            "5. Tropospheric DX 480i (VHF, 120 km, stable)",
            "6. Sporadic E-Skip 720p (VHF, 800 km, fading)",
            "7. ATSC 1080i Fringe (VHF, 75 km, Set-top)",
            "8. DVB-T2 1080p Modern (UHF, 15 km)",
            "9. Ku-Band Satellite Rain Fade (Ku-Band, 38000 km, Heavy Rain)",
            "10. J.83B Cable High Noise (J.83B, UHF, 5 km)",
            "11. Severe Multipath Ghosting (ATSC, VHF, 15 km)",
            "12. Datamoshing Cliff Edge (DVB-T2, UHF, 42 km)"
        ])
        self.preset_combo.currentIndexChanged.connect(self.on_preset_changed)
        pv.addWidget(self.preset_combo); pb.setLayout(pv); v.addWidget(pb)

        # Standard
        sb_box = QGroupBox("DTV Standard / Modulation"); sv = QVBoxLayout()
        self.std_combo = QComboBox()
        self.std_combo.addItems(["ATSC (8VSB)", "DVB-S2 (8PSK)",
                                  "J.83B (64QAM)", "DVB-T2 (256QAM)", "DVB-T (OFDM)"])
        self.std_combo.currentIndexChanged.connect(self.on_standard_changed)
        sv.addWidget(self.std_combo); sb_box.setLayout(sv); v.addWidget(sb_box)

        # RF Link
        rf = QGroupBox("RF Link & Weather"); rfv = QVBoxLayout(); rfv.setSpacing(3)
        rfv.addWidget(QLabel("Frequency Band"))
        self.freq_band_combo = QComboBox()
        self.freq_band_combo.addItems(["VHF (174 MHz)", "UHF (600 MHz)",
                                        "L-Band (1.5 GHz)", "Ku-Band (12 GHz)", "Ka-Band (26 GHz)"])
        self.freq_band_combo.currentIndexChanged.connect(self.on_impairment_changed)
        rfv.addWidget(self.freq_band_combo)

        rfv.addWidget(QLabel("Weather / Atmospheric"))
        self.weather_combo = QComboBox()
        self.weather_combo.addItems(["Clear Sky", "Fog / Mist",
                                      "Light Rain", "Heavy Rain", "Severe Thunderstorm"])
        self.weather_combo.currentIndexChanged.connect(self.on_impairment_changed)
        rfv.addWidget(self.weather_combo)

        rfv.addWidget(QLabel("TX EIRP Power"))
        self.tx_power_slider = QSlider(Qt.Horizontal)
        self.tx_power_slider.setRange(10, 80); self.tx_power_slider.setValue(40)
        self.tx_power_slider.valueChanged.connect(self.on_impairment_changed)
        self.tx_power_lbl = QLabel("40 dBW")
        rfv.addWidget(self.tx_power_slider); rfv.addWidget(self.tx_power_lbl)

        rfv.addWidget(QLabel("Distance"))
        self.range_slider = QSlider(Qt.Horizontal)
        self.range_slider.setRange(1, 40000); self.range_slider.setValue(10)
        self.range_slider.valueChanged.connect(self.on_impairment_changed)
        self.range_lbl = QLabel("10 km")
        rfv.addWidget(self.range_slider); rfv.addWidget(self.range_lbl)

        ex = QHBoxLayout()
        self.lna_checkbox = QCheckBox("LNA (+12 dB)")
        self.lna_checkbox.toggled.connect(self.on_impairment_changed)
        self.adv_checkbox = QCheckBox("Advanced DSP")
        ex.addWidget(self.lna_checkbox); ex.addWidget(self.adv_checkbox)
        rfv.addLayout(ex)

        self.adv_box = QGroupBox("Advanced DSP Impairments")
        av = QVBoxLayout(); av.setSpacing(2)
        av.addWidget(QLabel("Local Interference / Noise Floor"))
        self.noise_slider = QSlider(Qt.Horizontal)
        self.noise_slider.setRange(0, 100); self.noise_slider.setValue(0)
        self.noise_slider.valueChanged.connect(self.on_impairment_changed)
        self.noise_lbl = QLabel("0%")
        av.addWidget(self.noise_slider); av.addWidget(self.noise_lbl)
        av.addWidget(QLabel("Carrier Freq Offset (Tuner Drift)"))
        self.freq_slider = QSlider(Qt.Horizontal)
        self.freq_slider.setRange(-100, 100); self.freq_slider.setValue(0)
        self.freq_slider.valueChanged.connect(self.on_impairment_changed)
        self.freq_lbl = QLabel("0 Hz")
        av.addWidget(self.freq_slider); av.addWidget(self.freq_lbl)
        av.addWidget(QLabel("Clock Timing Offset / Jitter"))
        self.time_slider = QSlider(Qt.Horizontal)
        self.time_slider.setRange(990, 1010); self.time_slider.setValue(1000)
        self.time_slider.valueChanged.connect(self.on_impairment_changed)
        self.timing_offset_lbl = QLabel("1.000 (Ideal)")
        av.addWidget(self.time_slider); av.addWidget(self.timing_offset_lbl)
        av.addWidget(QLabel("Multipath Reflection (Ghosting)"))
        self.fade_slider = QSlider(Qt.Horizontal)
        self.fade_slider.setRange(0, 90); self.fade_slider.setValue(0)
        self.fade_slider.valueChanged.connect(self.on_impairment_changed)
        self.fade_lbl = QLabel("0.00 (LoS)")
        av.addWidget(self.fade_slider); av.addWidget(self.fade_lbl)
        self.adv_box.setLayout(av); self.adv_box.setVisible(False)
        self.adv_checkbox.toggled.connect(self.adv_box.setVisible)
        rfv.addWidget(self.adv_box)

        lb = QGroupBox("Real-time Link Budget"); lbv = QVBoxLayout(); lbv.setSpacing(1)
        self.fspl_lbl        = QLabel("FSPL: -- dB")
        self.weather_loss_lbl = QLabel("Weather Loss: -- dB")
        self.rx_power_lbl    = QLabel("RSSI: -- dBm")
        self.snr_lbl         = QLabel("SNR: -- dB")
        self.ber_lbl         = QLabel("Est. BER: --")
        for l in [self.fspl_lbl, self.weather_loss_lbl,
                  self.rx_power_lbl, self.snr_lbl, self.ber_lbl]:
            lbv.addWidget(l)
        lb.setLayout(lbv); rfv.addWidget(lb)
        rf.setLayout(rfv); v.addWidget(rf); v.addStretch()

        inn.setLayout(v); sc.setWidget(inn)
        ol = QVBoxLayout(); ol.setContentsMargins(0,0,0,0); ol.addWidget(sc)
        w.setLayout(ol); return w

    # ── TAB 2: Propagation & Skins ────────────────────────────
    def _tab_prop(self):
        w = QWidget(); v = QVBoxLayout(); v.setSpacing(6)
        pb = QGroupBox("RF Propagation Mode"); pv = QVBoxLayout()
        self.prop_combo = QComboBox()
        self.prop_combo.addItems(["Line-of-Sight (Standard)",
                                   "Tropospheric DX (+12 dB, stable)",
                                   "Sporadic E-Skip (fading, realistic model)"])
        self.prop_combo.currentIndexChanged.connect(self.on_impairment_changed)
        pv.addWidget(self.prop_combo)
        self.prop_desc_lbl = QLabel("Standard free-space propagation.")
        self.prop_desc_lbl.setWordWrap(True)
        self.prop_desc_lbl.setStyleSheet("color:#e3b341; font-style:italic;")
        pv.addWidget(self.prop_desc_lbl); pb.setLayout(pv); v.addWidget(pb)

        sk = QGroupBox("Receiver Box Theme"); sv = QVBoxLayout()
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["ATSC/DVB-T Terrestrial Set-top Box",
                                    "DVB-S2 Satellite Receiver Box",
                                    "Digital Cable Set-top Box (J.83B)"])
        sv.addWidget(self.theme_combo); sk.setLayout(sv); v.addWidget(sk)
        v.addStretch(); w.setLayout(v); return w

    # ── TAB 3: Media & Quality ────────────────────────────────
    def _tab_media(self):
        w = QWidget(); sc = QScrollArea(); sc.setWidgetResizable(True)
        inn = QWidget(); v = QVBoxLayout(); v.setSpacing(6)

        # Source
        src = QGroupBox("Media Source"); sv = QVBoxLayout()
        self.btn_file = QPushButton("Choose Video / Audio File...")
        self.btn_file.clicked.connect(self.on_select_file)
        sv.addWidget(self.btn_file)
        self.file_lbl = QLabel("Source: SMPTE test pattern (synthetic)")
        self.file_lbl.setWordWrap(True)
        self.file_lbl.setStyleSheet("color:#e3b341; font-style:italic;")
        sv.addWidget(self.file_lbl); src.setLayout(sv); v.addWidget(src)

        # TX params
        tx = QGroupBox("TX Video Parameters"); tv = QVBoxLayout(); tv.setSpacing(4)
        tv.addWidget(QLabel("DTV Resolution / Standard"))
        self.res_combo = QComboBox()
        for lbl, *_ in DTV_RESOLUTIONS:
            self.res_combo.addItem(lbl)
        self.res_combo.currentIndexChanged.connect(self.on_resolution_changed)
        tv.addWidget(self.res_combo)

        tv.addWidget(QLabel("MPEG-2 Bitrate (quality)"))
        self.jpg_slider = QSlider(Qt.Horizontal)   # kept same name for compat
        self.jpg_slider.setRange(10, 100); self.jpg_slider.setValue(60)
        self.jpg_slider.valueChanged.connect(self.on_bitrate_changed)
        self.jpg_lbl = QLabel("~3000 kbps (SD quality)")
        tv.addWidget(self.jpg_slider); tv.addWidget(self.jpg_lbl)

        tv.addWidget(QLabel("TX Frame Rate (FPS)"))
        self.fps_slider = QSlider(Qt.Horizontal)
        self.fps_slider.setRange(24, 60); self.fps_slider.setValue(29)
        self.fps_slider.valueChanged.connect(self.on_fps_changed)
        self.fps_lbl = QLabel("29 FPS")
        tv.addWidget(self.fps_slider); tv.addWidget(self.fps_lbl)

        self.interlace_checkbox = QCheckBox("TX Interlaced Scan  (real ildct/ilme encoding)")
        self.interlace_checkbox.setChecked(False)
        self.interlace_checkbox.stateChanged.connect(self.on_interlace_changed)
        tv.addWidget(self.interlace_checkbox)

        tv.addWidget(QLabel("Audio Codec"))
        self.audio_codec_combo = QComboBox()
        self.audio_codec_combo.addItems([
            "MP2  (DVB Default)",
            "AC-3  (ATSC Dolby Digital)",
            "AAC  (ISDB-T HE-AAC)"
        ])
        self.audio_codec_combo.currentIndexChanged.connect(self.on_audio_codec_changed)
        tv.addWidget(self.audio_codec_combo)

        tv.addWidget(QLabel("Hardware Acceleration Settings"))
        self.hw_accel_combo = QComboBox()
        self.hw_accel_combo.addItems([
            "Auto-Detect (QSV -> VAAPI -> Software)",
            "Intel QSV (mpeg2_qsv)",
            "VAAPI (AMD/Intel Linux - mpeg2_vaapi)",
            "Software Only (mpeg2video)"
        ])
        self.hw_accel_combo.setToolTip("Select GPU acceleration API. NVIDIA GPUs do not support hardware MPEG-2 encoding, so use Software Only or Auto-Detect.")
        self.hw_accel_combo.currentIndexChanged.connect(self.on_hw_accel_changed)
        tv.addWidget(self.hw_accel_combo)
        tx.setLayout(tv); v.addWidget(tx)

        # RX params
        rx = QGroupBox("RX Processing"); rv = QVBoxLayout(); rv.setSpacing(4)
        self.deinterlace_checkbox = QCheckBox("RX Deinterlace  (yadif filter in decoder)")
        self.deinterlace_checkbox.setChecked(False)
        self.deinterlace_checkbox.stateChanged.connect(self.on_deinterlace_changed)
        rv.addWidget(self.deinterlace_checkbox)

        rv.addWidget(QLabel("Audio Volume"))
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100); self.vol_slider.setValue(70)
        self.vol_slider.valueChanged.connect(self.on_volume_changed)
        rv.addWidget(self.vol_slider)

        rv.addWidget(QLabel("Channel Quality Override  (-1 = physics mode)"))
        self.qual_slider = QSlider(Qt.Horizontal)
        self.qual_slider.setRange(-1, 100); self.qual_slider.setValue(-1)
        self.qual_slider.valueChanged.connect(self._on_qual_override)
        self.qual_lbl = QLabel("AUTO (physics-based)")
        rv.addWidget(self.qual_slider); rv.addWidget(self.qual_lbl)

        rv.addWidget(QLabel("Recording Resolution"))
        self.rec_res_combo = QComboBox()
        self.rec_res_combo.addItems([
            "Follow Stream Resolution",
            "480p  NTSC (854x480)",
            "576p  PAL (1024x576)",
            "720p  HD (1280x720)",
            "1080p Full HD (1920x1080)"
        ])
        rv.addWidget(self.rec_res_combo)

        rec_row = QHBoxLayout()
        self.btn_record = QPushButton("Record RX"); self.btn_record.setCheckable(True)
        self.btn_record.clicked.connect(self._toggle_rec)
        self.btn_save = QPushButton("Save Recording..."); self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save_rec)
        rec_row.addWidget(self.btn_record); rec_row.addWidget(self.btn_save)
        rv.addLayout(rec_row)
        self.rec_lbl = QLabel("Not recording"); self.rec_lbl.setStyleSheet("color:#8b949e;")
        rv.addWidget(self.rec_lbl)
        rx.setLayout(rv); v.addWidget(rx)
        v.addStretch()

        inn.setLayout(v); sc.setWidget(inn)
        ol = QVBoxLayout(); ol.setContentsMargins(0,0,0,0); ol.addWidget(sc)
        w.setLayout(ol); return w

    # ── TAB 4: Guide ──────────────────────────────────────────
    def _tab_guide(self):
        w = QWidget(); sc = QScrollArea(); sc.setWidgetResizable(True)
        inn = QWidget(); v = QVBoxLayout()
        g = QLabel("""
<h3 style="color:#58a6ff;">DTV SDR Playground  —  MPEG-2 TS Guide</h3>
<p><b>How signal corruption works (REAL codec, not fake):</b><br>
The TX encodes your source as real MPEG-2 inside a Transport Stream.<br>
The channel relay corrupts TS payload bytes based on your link budget SNR.<br>
The RX ffmpeg decoder processes the corrupted stream with <code>-ec deblock+favor_inter</code>:<br>
&nbsp;&nbsp;• Missing I-frame → freeze until next GOP (every ~1 second)<br>
&nbsp;&nbsp;• Corrupted motion vectors → temporal smear, content from wrong location<br>
&nbsp;&nbsp;• DCT coefficient errors → block distortion within that region<br>
&nbsp;&nbsp;• Full packet drop → decoder conceals with interpolation from prev frame<br>
These are <b>authentic MPEG-2 decoder artifacts</b>, not simulated.</p>
<p><b>Lock thresholds by standard:</b><br>
DVB-T (OFDM): 5 dB SNR — easiest to lock<br>
DVB-S2 (8PSK): 10 dB SNR<br>
ATSC (8VSB): 15 dB SNR<br>
DVB-T2 (256QAM): 16 dB SNR<br>
J.83B (64QAM): 22 dB SNR — needs excellent signal</p>
<p><b>Quick lock:</b> Select Preset 1 (480i Local). Distance 10 km, DVB-T, LNA on.<br>
<b>Force artifacts:</b> Use the Quality Override slider in Media &amp; Quality tab.<br>
Drag it to 20-50% for authentic datamosh, 0% for full outage/outage screen.</p>
<p><b>Interlacing:</b> Enable "TX Interlaced" to use real MPEG-2 ildct/ilme flags.<br>
Enable "RX Deinterlace" to apply yadif in the decoder pipeline.</p>
<p><b>Hardware accel:</b> Auto-detected at launch (QSV → VAAPI → software).</p>
""")
        g.setWordWrap(True); g.setTextFormat(Qt.RichText)
        g.setStyleSheet("color:#c9d1d9; padding:8px;")
        v.addWidget(g); v.addStretch()
        inn.setLayout(v); sc.setWidget(inn)
        ol = QVBoxLayout(); ol.setContentsMargins(0,0,0,0); ol.addWidget(sc)
        w.setLayout(ol); return w

    def _build_screens(self):
        box = QGroupBox("Live DTV Screens"); h = QHBoxLayout(); h.setSpacing(8)
        
        # Screen 1: Preview (Transmitting Source)
        col1_widget = QWidget()
        col1 = QVBoxLayout(col1_widget)
        col1.setContentsMargins(0, 0, 0, 0)
        lbl1 = QLabel("<b>Transmitting Source (TX Preview)</b>"); lbl1.setAlignment(Qt.AlignCenter)
        col1.addWidget(lbl1)
        
        self.orig_screen = ClickableLabel()
        self.orig_screen.setFixedSize(480, 270)
        self.orig_screen.setStyleSheet("background:#000; border:1px solid #30363d;")
        self.orig_screen.setAlignment(Qt.AlignCenter)
        self.orig_screen.double_clicked.connect(self.on_tx_double_clicked)
        col1.addWidget(self.orig_screen)
        
        # Preview Audio controls
        prev_aud_row = QHBoxLayout()
        self.btn_mute_preview = QCheckBox("🔊 Play Preview Audio (direct)")
        self.btn_mute_preview.setChecked(False) # default to False (muted)
        self.btn_mute_preview.stateChanged.connect(self.on_preview_mute_changed)
        prev_aud_row.addWidget(self.btn_mute_preview)
        
        # Preview stats/info
        self.preview_stats_lbl = QLabel("Res: 720x480 | Codec: Synth")
        self.preview_stats_lbl.setStyleSheet("color: #8b949e; font-size: 10px; font-family: monospace;")
        prev_aud_row.addWidget(self.preview_stats_lbl)
        col1.addLayout(prev_aud_row)
        
        h.addWidget(col1_widget)
        
        # Screen 2: RX (Received DTV Output)
        col2_widget = QWidget()
        col2 = QVBoxLayout(col2_widget)
        col2.setContentsMargins(0, 0, 0, 0)
        lbl2 = QLabel("<b>Received DTV Output (RX)</b>"); lbl2.setAlignment(Qt.AlignCenter)
        col2.addWidget(lbl2)
        
        self.recv_screen = ClickableLabel()
        self.recv_screen.setFixedSize(480, 270)
        self.recv_screen.setStyleSheet("background:#000; border:1px solid #30363d;")
        self.recv_screen.setAlignment(Qt.AlignCenter)
        self.recv_screen.double_clicked.connect(self.on_rx_double_clicked)
        col2.addWidget(self.recv_screen)
        
        # RX Audio controls
        rx_aud_row = QHBoxLayout()
        self.btn_mute_rx = QCheckBox("🔊 Play RX Audio")
        self.btn_mute_rx.setChecked(True) # default to True (unmuted)
        self.btn_mute_rx.stateChanged.connect(self.on_rx_mute_changed)
        rx_aud_row.addWidget(self.btn_mute_rx)
        
        # RX stats/info
        self.rx_stats_lbl = QLabel("Res: --x-- | Deint: --")
        self.rx_stats_lbl.setStyleSheet("color: #8b949e; font-size: 10px; font-family: monospace;")
        rx_aud_row.addWidget(self.rx_stats_lbl)
        col2.addLayout(rx_aud_row)
        
        h.addWidget(col2_widget)
        box.setLayout(h); return box

    def _build_signal_strip(self):
        box = QGroupBox("Tuner Status"); v = QVBoxLayout(); v.setSpacing(4)
        bar_row = QHBoxLayout()
        bar_row.addWidget(QLabel("Signal Lock:"))
        self.signal_bar = QProgressBar()
        self.signal_bar.setRange(0, 100); self.signal_bar.setValue(0)
        self.signal_bar.setFixedHeight(18)
        bar_row.addWidget(self.signal_bar)
        self.signal_lbl = QLabel("0% (NO SIGNAL)"); self.signal_lbl.setFixedWidth(170)
        bar_row.addWidget(self.signal_lbl); v.addLayout(bar_row)
        self.banner_lbl = QLabel("NO SIGNAL\n[ SEARCHING FOR CHANNELS ]")
        self.banner_lbl.setFont(QFont("DejaVu Sans", 11, QFont.Bold))
        self.banner_lbl.setStyleSheet(
            "background:#1c1e22; border:2px solid #da3633; border-radius:5px;"
            " padding:6px; color:#da3633;")
        self.banner_lbl.setAlignment(Qt.AlignCenter)
        v.addWidget(self.banner_lbl)

        # Added stats row
        stats_row = QHBoxLayout()
        self.stats_tx_lbl = QLabel("TX Src: --")
        self.stats_rx_lbl = QLabel("RX Dec: --")
        self.stats_aud_lbl = QLabel("Audio Codec: --")
        
        for lbl in [self.stats_tx_lbl, self.stats_rx_lbl, self.stats_aud_lbl]:
            lbl.setStyleSheet("color:#8b949e; font-size:10px; font-family:monospace; padding: 2px;")
            stats_row.addWidget(lbl)
        v.addLayout(stats_row)

        box.setLayout(v); return box

    # ─────────────────────────────────────────────────────────────
    #  HANDLERS
    # ─────────────────────────────────────────────────────────────
    def on_guide_changed(self):
        self.channel_name   = self.name_input.text()
        self.channel_number = self.num_input.text()

    def on_standard_changed(self, idx):
        if self.gr_tb:
            try: self.gr_tb.set_active_standard(idx)
            except Exception: pass
        self.on_impairment_changed()

    def on_select_file(self):
        fname, _ = QFileDialog.getOpenFileName(
            self, "Open Media File", "/home/hunter",
            "Video/Audio (*.mp4 *.mkv *.avi *.ts *.m2ts *.mov *.webm)"
        )
        if fname:
            self.video_file = fname
            self.file_lbl.setText(f"Source: {os.path.basename(fname)}")
            self.seek_seconds = 0.0
            self.play_start_offset = 0.0
            self.play_start_time = time.time()
            self.playback_paused = False
            if hasattr(self, 'btn_play_pause'):
                self.btn_play_pause.setText("⏸ Pause")
            self._probe_file_details(fname)
            self._restart_pipeline()

    def on_resolution_changed(self, idx):
        if idx < 0 or idx >= len(DTV_RESOLUTIONS):
            return
        _, w, h, il, fps_def = DTV_RESOLUTIONS[idx]
        self.video_width  = w
        self.video_height = h
        self.fps          = fps_def
        self.fps_slider.setValue(fps_def)
        if not il:
            self.interlace_checkbox.setChecked(False)
        self.interlaced = self.interlace_checkbox.isChecked()
        
        # Auto-adjust bitrate slider based on resolution for realistic quality
        bitrate_slider_val = [60, 65, 80, 88, 92, 20][idx]
        self.jpg_slider.setValue(bitrate_slider_val)
        
        self._restart_pipeline()

    def on_bitrate_changed(self, val):
        # Logarithmic: 10 → 500 kbps, 100 → 25000 kbps
        self.bitrate_kbps = int(500.0 * (50.0 ** ((val - 10) / 90.0)))
        labels = {0: 'very heavy blocking', 30: 'heavy artifacts',
                  60: 'SD quality', 80: 'HD quality', 95: 'near-lossless'}
        tag = ''
        for thresh, txt in sorted(labels.items(), reverse=True):
            if val >= thresh:
                tag = txt; break
        self.jpg_lbl.setText(f"~{self.bitrate_kbps} kbps  ({tag})")
        # Restart encoder with new bitrate (no need to restart decoder)
        self._start_encoder()

    def on_fps_changed(self, val):
        self.fps = val
        self.fps_lbl.setText(f"{val} FPS")
        self._restart_pipeline()

    def on_interlace_changed(self, state):
        self.interlaced = (state == Qt.Checked)
        self._restart_pipeline()

    def on_deinterlace_changed(self, state):
        self.deinterlace_rx = (state == Qt.Checked)
        if self.mpeg_decoder and self.mpeg_decoder.isRunning():
            self.mpeg_decoder.set_deinterlace(self.deinterlace_rx)
        else:
            self._start_decoder()

    def on_audio_codec_changed(self, idx):
        codecs = ['mp2', 'ac3', 'aac']
        if 0 <= idx < len(codecs):
            self.audio_codec = codecs[idx]
            self._restart_pipeline()

    def on_hw_accel_changed(self, idx):
        global HW_ENC, HW_DEC_FLAGS, HW_ENC_FLAGS, VAAPI_VF
        if idx == 0:  # Auto-Detect
            probe_hw()
        elif idx == 1:  # Intel QSV
            HW_ENC = 'mpeg2_qsv'
            HW_DEC_FLAGS = []
            HW_ENC_FLAGS = []
            VAAPI_VF = []
        elif idx == 2:  # VAAPI
            dev = '/dev/dri/renderD128'
            HW_ENC = 'mpeg2_vaapi'
            HW_ENC_FLAGS = ['-vaapi_device', dev] if os.path.exists(dev) else []
            VAAPI_VF = ['format=nv12,hwupload,']
            HW_DEC_FLAGS = []
        else:  # Software Only
            HW_ENC = 'mpeg2video'
            HW_DEC_FLAGS = []
            HW_ENC_FLAGS = []
            VAAPI_VF = []
        
        print(f"[HW] Switched to: {HW_ENC}")
        self._restart_pipeline()

    def on_tx_double_clicked(self):
        self.enter_fullscreen('tx')
        
    def on_rx_double_clicked(self):
        self.enter_fullscreen('rx')
        
    def enter_fullscreen(self, target):
        if self.fullscreen_view:
            try: self.fullscreen_view.close()
            except Exception: pass
            
        title = "Transmitting Source (TX Preview)" if target == 'tx' else "Received DTV Output (RX)"
        self.fullscreen_view = FullscreenWindow(title, self)
        self.fullscreen_target = target
        self.fullscreen_view.closed.connect(self.on_fullscreen_closed)
        
        # Show current frame immediately
        pix = self.orig_screen.pixmap() if target == 'tx' else self.recv_screen.pixmap()
        if pix:
            self.fullscreen_view.setPixmap(pix)
            
    def on_fullscreen_closed(self):
        self.fullscreen_view = None
        self.fullscreen_target = None

    def on_volume_changed(self, val):
        is_unmuted = self.btn_mute_rx.isChecked() if hasattr(self, 'btn_mute_rx') else True
        vol = val if is_unmuted else 0
        if self.mpeg_decoder:
            self.mpeg_decoder.vol_level = vol / 100.0

    def on_preview_mute_changed(self, state):
        self._start_preview()

    def on_rx_mute_changed(self, state):
        is_unmuted = self.btn_mute_rx.isChecked()
        vol = self.vol_slider.value() if is_unmuted else 0
        if self.mpeg_decoder:
            self.mpeg_decoder.vol_level = vol / 100.0

    def _probe_file_details(self, filepath):
        self.file_v_codec = "Unknown"
        self.file_v_res = "Unknown"
        self.file_v_fps = "Unknown"
        self.file_a_codec = "Unknown"
        self.file_a_rate = "Unknown"
        self.video_duration = 0.0
        
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', 'stream=codec_name,width,height,r_frame_rate',
               '-of', 'default=noprint_wrappers=1:nokey=1', filepath]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            parts = r.stdout.strip().split('\n')
            if len(parts) >= 4:
                self.file_v_codec = parts[0]
                self.file_v_res = f"{parts[1]}x{parts[2]}"
                fps_parts = parts[3].split('/')
                if len(fps_parts) == 2 and float(fps_parts[1]) > 0:
                    self.file_v_fps = f"{float(fps_parts[0]) / float(fps_parts[1]):.1f}"
                else:
                    self.file_v_fps = parts[3]
        except Exception:
            pass
            
        cmd_a = ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                 '-show_entries', 'stream=codec_name,sample_rate',
                 '-of', 'default=noprint_wrappers=1:nokey=1', filepath]
        try:
            r = subprocess.run(cmd_a, capture_output=True, text=True, timeout=2)
            parts = r.stdout.strip().split('\n')
            if len(parts) >= 2:
                self.file_a_codec = parts[0]
                self.file_a_rate = f"{int(parts[1])//1000}kHz"
        except Exception:
            pass

        cmd_d = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', filepath]
        try:
            r = subprocess.run(cmd_d, capture_output=True, text=True, timeout=2)
            val = r.stdout.strip()
            if val:
                self.video_duration = float(val)
        except Exception:
            pass

        if hasattr(self, 'timeline_slider'):
            if self.video_duration > 0:
                self.timeline_slider.setRange(0, int(self.video_duration))
                self.timeline_slider.setEnabled(True)
            else:
                self.timeline_slider.setRange(0, 100)
                self.timeline_slider.setEnabled(False)
                self.time_lbl.setText("00:00 / 00:00")

    def _build_player_controls(self):
        player_box = QGroupBox("Media Player Control Room")
        pv = QVBoxLayout(); pv.setSpacing(4)
        
        # Timeline row
        time_row = QHBoxLayout()
        self.btn_play_pause = QPushButton("⏸ Pause")
        self.btn_play_pause.setFixedWidth(80)
        self.btn_play_pause.clicked.connect(self.on_play_pause_clicked)
        time_row.addWidget(self.btn_play_pause)
        
        self.timeline_slider = QSlider(Qt.Horizontal)
        self.timeline_slider.setRange(0, 100)
        self.timeline_slider.setEnabled(False)
        self.timeline_slider.sliderReleased.connect(self.on_timeline_seek)
        time_row.addWidget(self.timeline_slider)
        
        self.time_lbl = QLabel("00:00 / 00:00")
        self.time_lbl.setStyleSheet("font-family:monospace; font-size:10px; color:#8b949e;")
        time_row.addWidget(self.time_lbl)
        pv.addLayout(time_row)
        
        # Playlist row
        playlist_row = QHBoxLayout()
        self.playlist_widget = QListWidget()
        self.playlist_widget.setFixedHeight(80)
        self.playlist_widget.itemDoubleClicked.connect(self.on_playlist_item_double_clicked)
        playlist_row.addWidget(self.playlist_widget)
        
        p_btn_col = QVBoxLayout()
        btn_add = QPushButton("Add File")
        btn_add.clicked.connect(self.on_playlist_add)
        btn_remove = QPushButton("Remove")
        btn_remove.clicked.connect(self.on_playlist_remove)
        p_btn_col.addWidget(btn_add)
        p_btn_col.addWidget(btn_remove)
        
        p_nav_row = QHBoxLayout()
        btn_prev = QPushButton("⏮")
        btn_prev.clicked.connect(self.on_playlist_prev)
        btn_next = QPushButton("⏭")
        btn_next.clicked.connect(self.on_playlist_next)
        p_nav_row.addWidget(btn_prev)
        p_nav_row.addWidget(btn_next)
        p_btn_col.addLayout(p_nav_row)
        
        playlist_row.addLayout(p_btn_col)
        pv.addLayout(playlist_row)
        
        player_box.setLayout(pv)
        return player_box

    def on_play_pause_clicked(self):
        if not self.video_file: return
        self.playback_paused = not self.playback_paused
        
        if self.playback_paused:
            self.btn_play_pause.setText("▶ Play")
            self.pause_start_time = time.time()
            if sys.platform != 'win32':
                if self.mpeg_encoder and self.mpeg_encoder.proc:
                    try: self.mpeg_encoder.proc.send_signal(signal.SIGSTOP)
                    except Exception: pass
                if self.preview_proc:
                    try: self.preview_proc.send_signal(signal.SIGSTOP)
                    except Exception: pass
            else:
                # Windows fallback: stop processes to pause
                if self.mpeg_encoder:
                    try: self.mpeg_encoder.stop()
                    except Exception: pass
                    self.mpeg_encoder = None
                if self.preview_proc:
                    try:
                        self.preview_proc.terminate()
                        self.preview_proc.wait(timeout=1)
                    except Exception: pass
                    self.preview_proc = None
        else:
            self.btn_play_pause.setText("⏸ Pause")
            if hasattr(self, 'pause_start_time'):
                self.play_start_time += (time.time() - self.pause_start_time)
            
            if sys.platform != 'win32':
                if self.mpeg_encoder and self.mpeg_encoder.proc:
                    try: self.mpeg_encoder.proc.send_signal(signal.SIGCONT)
                    except Exception: pass
                if self.preview_proc:
                    try: self.preview_proc.send_signal(signal.SIGCONT)
                    except Exception: pass
            else:
                # Windows fallback: restart pipeline from the current progress offset
                elapsed = time.time() - getattr(self, 'play_start_time', time.time())
                curr_pos = getattr(self, 'play_start_offset', 0.0) + elapsed
                self.seek_seconds = float(curr_pos)
                self._restart_pipeline()

    def on_timeline_seek(self):
        val = self.timeline_slider.value()
        self.play_seek_to(val)

    def play_seek_to(self, seconds):
        if not self.video_file: return
        self.seek_seconds = float(seconds)
        self.play_start_offset = float(seconds)
        self.play_start_time = time.time()
        self.playback_paused = False
        self.btn_play_pause.setText("⏸ Pause")
        self._restart_pipeline()

    def on_playlist_add(self):
        fname, _ = QFileDialog.getOpenFileName(
            self, "Add to Playlist", "/home/hunter",
            "Video/Audio (*.mp4 *.mkv *.avi *.ts *.m2ts *.mov *.webm)"
        )
        if fname:
            if not hasattr(self, 'playlist'):
                self.playlist = []
            self.playlist.append(fname)
            self.playlist_widget.addItem(os.path.basename(fname))
            if len(self.playlist) == 1:
                self.play_playlist_index(0)

    def on_playlist_remove(self):
        if not hasattr(self, 'playlist') or not self.playlist: return
        row = self.playlist_widget.currentRow()
        if 0 <= row < len(self.playlist):
            self.playlist.pop(row)
            self.playlist_widget.takeItem(row)

    def on_playlist_item_double_clicked(self, item):
        row = self.playlist_widget.row(item)
        self.play_playlist_index(row)

    def play_playlist_index(self, idx):
        if hasattr(self, 'playlist') and 0 <= idx < len(self.playlist):
            self.playlist_widget.setCurrentRow(idx)
            self.video_file = self.playlist[idx]
            self.file_lbl.setText(f"Source: {os.path.basename(self.video_file)}")
            self.seek_seconds = 0.0
            self.play_start_offset = 0.0
            self.play_start_time = time.time()
            self.playback_paused = False
            self.btn_play_pause.setText("⏸ Pause")
            
            self._probe_file_details(self.video_file)
            self._restart_pipeline()

    def on_playlist_next(self):
        if not hasattr(self, 'playlist') or not self.playlist: return
        curr = self.playlist_widget.currentRow()
        next_idx = (curr + 1) % len(self.playlist)
        self.play_playlist_index(next_idx)

    def on_playlist_prev(self):
        if not hasattr(self, 'playlist') or not self.playlist: return
        curr = self.playlist_widget.currentRow()
        prev_idx = (curr - 1) % len(self.playlist)
        self.play_playlist_index(prev_idx)

    def _on_qual_override(self, val):
        self.quality_override = val
        if val < 0:
            self.qual_lbl.setText("AUTO (physics-based)")
        else:
            tags = [(0, "FULL OUTAGE"), (20, "severe datamosh"),
                    (50, "weak signal"), (75, "fair"), (90, "good")]
            tag = "excellent"
            for thresh, t in tags:
                if val <= thresh: tag = t; break
            self.qual_lbl.setText(f"OVERRIDE: {val}%  [{tag}]")

    def on_preset_changed(self, idx):
        if idx == 0: return
        widgets = [self.std_combo, self.freq_band_combo, self.weather_combo,
                   self.tx_power_slider, self.range_slider, self.lna_checkbox,
                   self.prop_combo, self.theme_combo, self.res_combo, self.interlace_checkbox,
                   self.audio_codec_combo, self.noise_slider, self.freq_slider,
                   self.time_slider, self.fade_slider]
        for w in widgets: w.blockSignals(True)

        cfg = {
            1: dict(std=0, band=1, wx=0, pwr=40, dist=10,  lna=True,  prop=0, theme=0, res=0, il=True,  acodec=1, noise=0, freq=0, time=1000, fade=0),
            2: dict(std=4, band=1, wx=0, pwr=43, dist=25,  lna=True,  prop=0, theme=0, res=2, il=False, acodec=0, noise=0, freq=0, time=1000, fade=0),
            3: dict(std=1, band=3, wx=0, pwr=60, dist=38000, lna=True, prop=0, theme=1, res=3, il=True,  acodec=2, noise=0, freq=0, time=1000, fade=0),
            4: dict(std=2, band=1, wx=0, pwr=38, dist=2,   lna=False, prop=0, theme=2, res=4, il=False, acodec=1, noise=0, freq=0, time=1000, fade=0),
            5: dict(std=4, band=0, wx=0, pwr=55, dist=120, lna=True,  prop=1, theme=0, res=0, il=True,  acodec=0, noise=0, freq=0, time=1000, fade=0),
            6: dict(std=4, band=0, wx=0, pwr=65, dist=800, lna=True,  prop=2, theme=0, res=2, il=False, acodec=0, noise=0, freq=0, time=1000, fade=0),
            7: dict(std=0, band=0, wx=0, pwr=45, dist=75,  lna=True,  prop=0, theme=0, res=3, il=True,  acodec=1, noise=0, freq=0, time=1000, fade=0),
            8: dict(std=3, band=1, wx=0, pwr=42, dist=15,  lna=True,  prop=0, theme=0, res=4, il=False, acodec=2, noise=0, freq=0, time=1000, fade=0),
            9: dict(std=1, band=3, wx=3, pwr=65, dist=38000, lna=True, prop=0, theme=1, res=3, il=True,  acodec=2, noise=0, freq=0, time=1000, fade=0),
            10: dict(std=2, band=1, wx=0, pwr=35, dist=5,   lna=False, prop=0, theme=2, res=4, il=False, acodec=1, noise=35, freq=0, time=1000, fade=0),
            11: dict(std=0, band=0, wx=0, pwr=40, dist=15,  lna=True,  prop=0, theme=0, res=3, il=True,  acodec=1, noise=0, freq=0, time=1000, fade=45),
            12: dict(std=3, band=1, wx=0, pwr=40, dist=42,  lna=True,  prop=0, theme=0, res=2, il=False, acodec=2, noise=0, freq=0, time=1000, fade=0),
        }.get(idx, {})

        if cfg:
            self.std_combo.setCurrentIndex(cfg['std'])
            self.freq_band_combo.setCurrentIndex(cfg['band'])
            self.weather_combo.setCurrentIndex(cfg['wx'])
            self.tx_power_slider.setValue(cfg['pwr'])
            self.range_slider.setValue(cfg['dist'])
            self.lna_checkbox.setChecked(cfg['lna'])
            self.prop_combo.setCurrentIndex(cfg['prop'])
            self.theme_combo.setCurrentIndex(cfg['theme'])
            self.res_combo.setCurrentIndex(cfg['res'])
            self.interlace_checkbox.setChecked(cfg['il'])
            self.audio_codec_combo.setCurrentIndex(cfg['acodec'])
            self.audio_codec = ['mp2', 'ac3', 'aac'][cfg['acodec']]
            self.noise_slider.setValue(cfg.get('noise', 0))
            self.freq_slider.setValue(cfg.get('freq', 0))
            self.time_slider.setValue(cfg.get('time', 1000))
            self.fade_slider.setValue(cfg.get('fade', 0))

        for w in widgets: w.blockSignals(False)

        _, w, h, _, fps_def = DTV_RESOLUTIONS[self.res_combo.currentIndex()]
        self.video_width = w; self.video_height = h; self.fps = fps_def
        self.interlaced = self.interlace_checkbox.isChecked()
        self.fps_slider.setValue(fps_def)
        if self.gr_tb:
            try: self.gr_tb.set_active_standard(self.std_combo.currentIndex())
            except Exception: pass
        self.on_impairment_changed()
        self._restart_pipeline()

    # ─────────────────────────────────────────────────────────────
    #  LINK BUDGET / IMPAIRMENT CALCULATION
    # ─────────────────────────────────────────────────────────────
    def on_impairment_changed(self):
        dist   = self.range_slider.value()
        fi     = self.freq_band_combo.currentIndex()
        freq   = [174.0, 600.0, 1500.0, 12000.0, 26500.0][min(fi, 4)]
        wi     = self.weather_combo.currentIndex()
        atten  = [
            [0,     0,     0,     0,     0    ],
            [0.001, 0.005, 0.02,  0.15,  0.3  ],
            [0.002, 0.01,  0.05,  0.5,   1.2  ],
            [0.005, 0.03,  0.12,  3.5,   8.0  ],
            [0.01,  0.10,  0.35,  8.0,   20.0 ],
        ][wi][min(fi, 4)]

        std_idx  = self.std_combo.currentIndex()
        medium_loss = 0.0
        if std_idx in (0, 3, 4):  # Terrestrial standards: ATSC, DVB-T2, DVB-T
            medium_loss = 35.0   # 35 dB terrain/foliage/urban attenuation
        elif std_idx == 2:        # Cable standard: J.83B
            medium_loss = dist * 25.0  # Coaxial cable loss (25 dB/km)

        tx_dbw = self.tx_power_slider.value()
        tx_dbm = tx_dbw + 30.0
        fspl   = 20*math.log10(max(dist, 0.1)) + 20*math.log10(freq) + 32.44
        # Limit weather loss path length to 15 km (troposphere height) to prevent infinite attenuation on satellite links
        wloss  = min(dist, 15.0) * atten
        tloss  = fspl + wloss + medium_loss

        prop  = self.prop_combo.currentIndex()
        pgain = 0.0
        tnow  = time.time()
        eskip = 0.0
        if prop == 1:
            pgain = 12.0
            self.prop_desc_lbl.setText("Tropospheric DX: stable ducting +12 dB.")
        elif prop == 2:
            slow  = 20.0 * math.sin(tnow * 0.25)
            fast  = 6.0  * math.sin(tnow * 2.3)
            burst = 15.0 * max(0, math.sin(tnow * 0.08))
            eskip = slow + fast + burst + random.uniform(-2, 2)
            pgain = eskip
            self.prop_desc_lbl.setText(f"Sporadic E-Skip: {eskip:+.1f} dB fading.")
        else:
            self.prop_desc_lbl.setText("Line-of-Sight: standard free-space path loss.")

        # Band-dependent antenna gains (VHF: 3 dBi, UHF: 6 dBi, L-Band: 12 dBi, Ku-Band: 36 dBi, Ka-Band: 42 dBi)
        # Represents realistic home TV antennas & high-gain satellite dishes
        ant_gain = [3.0, 6.0, 12.0, 36.0, 42.0][min(fi, 4)]
        rx_gain = ant_gain + (12.0 if self.lna_checkbox.isChecked() else 0.0)
        rx_pwr  = tx_dbm - tloss + rx_gain + pgain

        # Realistic noise floor: thermal + NF at standard bandwidth
        bw_mhz = [6, 8, 8, 8, 8][self.std_combo.currentIndex()]
        n_floor = -174.0 + 10*math.log10(bw_mhz * 1e6) + 7.0   # 7 dB NF

        ni = self.noise_slider.value()
        n_interf = -999.0 if ni == 0 else n_floor + (ni / 100.0) * 50.0
        n_lin = 10.0 ** (n_floor / 10.0)
        if n_interf > -999.0:
            n_lin += 10.0 ** (n_interf / 10.0)
        n_total = 10.0 * math.log10(n_lin)
        snr = rx_pwr - n_total

        fo   = self.freq_slider.value() / 1000.0
        tv   = self.time_slider.value() / 1000.0
        mv   = self.fade_slider.value() / 100.0
        snr -= abs(fo)*150 + abs(tv-1)*1000 + mv*15
        self._effective_snr = snr

        std_idx  = self.std_combo.currentIndex()
        thresh   = [15, 10, 22, 16, 5][std_idx]
        margin   = snr - thresh
        if margin < 0:
            lock = 0
        elif margin < 3:
            lock = int(margin / 3.0 * 60)
        else:
            lock = min(60 + int((margin - 3) * 13.3), 100)

        if prop == 2:
            eskip_factor = max(0.0, min(1.0, (eskip + 30.0) / 60.0))
            lock = int(lock * eskip_factor)

        if self.quality_override >= 0:
            lock = self.quality_override

        self._lock_pct = lock
        if self.mpeg_decoder:
            self.mpeg_decoder.lock_level = lock

        # Estimated BER
        ber_est = 0.0 if lock >= 100 else (1.0 if lock == 0 else
                  ((100 - lock) / 100.0) ** 3.0)

        # Update labels
        dist_str = (f"{dist/1000:.1f} Mm" if dist >= 1000 else f"{dist} km")
        w_str = (f"{10**(tx_dbw/10)/1000:.1f} kW" if 10**(tx_dbw/10) >= 1000
                 else f"{10**(tx_dbw/10):.0f} W")
        self.tx_power_lbl.setText(f"{tx_dbw} dBW ({w_str})")
        self.range_lbl.setText(dist_str)
        self.fspl_lbl.setText(f"FSPL: {fspl:.1f} dB")
        m_lbl = "Terrain Loss" if std_idx in (0, 3, 4) else ("Cable Loss" if std_idx == 2 else "Medium Loss")
        self.weather_loss_lbl.setText(f"Weather Loss: {wloss:.2f} dB | {m_lbl}: {medium_loss:.1f} dB")
        self.rx_power_lbl.setText(f"RSSI: {rx_pwr:.1f} dBm")
        self.snr_lbl.setText(f"SNR: {snr:.1f} dB  (need ≥{thresh} dB)")
        self.ber_lbl.setText(f"Est. BER: {ber_est:.2e}" if ber_est > 0
                              else "Est. BER: < 1×10⁻⁹")
        self.noise_lbl.setText(f"{ni}%" + (f" (+{n_interf:.0f} dBm)" if ni else " (clean)"))
        self.freq_lbl.setText(f"{fo * 500000:+.0f} Hz")
        self.timing_offset_lbl.setText("1.000 (Ideal)" if tv == 1.0 else f"{tv:.3f} (drift)")
        self.fade_lbl.setText("0.00 (LoS)" if mv == 0 else f"{mv:.2f} (ghosting)")

        self.signal_bar.setValue(lock)
        if lock >= 75:
            self.signal_bar.setStyleSheet(
                "QProgressBar::chunk{background:#238636;border-radius:3px;}")
            self.signal_lbl.setText(f"{lock}% (LOCKED)")
        elif lock >= 20:
            self.signal_bar.setStyleSheet(
                "QProgressBar::chunk{background:#e3b341;border-radius:3px;}")
            self.signal_lbl.setText(f"{lock}% (WEAK)")
        else:
            self.signal_bar.setStyleSheet(
                "QProgressBar::chunk{background:#da3633;border-radius:3px;}")
            self.signal_lbl.setText(f"{lock}% (NO SIGNAL)")

        if self.gr_tb:
            try:
                sl = 10.0 ** (snr / 10.0)
                self.gr_tb.set_noise_level(min(math.sqrt(1.0 / max(sl, 1e-9)), 1.5))
                self.gr_tb.set_freq_offset(fo)
                self.gr_tb.set_timing_offset(tv)
                self.gr_tb.set_multipath_gain(mv)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────
    #  TX MEDIA LOOP  (synthetic test pattern)
    # ─────────────────────────────────────────────────────────────
    def media_loop(self):
        # TX display
        if self.video_file:
            if self.latest_preview is not None:
                qimg = QImage(self.latest_preview, self.preview_w, self.preview_h, QImage.Format_RGB888)
                pix = QPixmap.fromImage(qimg)
                self.orig_screen.setPixmap(pix)
                if self.fullscreen_target == 'tx' and self.fullscreen_view:
                    self.fullscreen_view.setPixmap(pix)
            # Encoder reads file independently — no push needed
        else:
            # Synthetic: generate frame at 720x480 for fast performance and stable physics
            frame = self._make_test_pattern(720, 480)

            if self.interlace_checkbox.isChecked():
                tx_frame = self._field_split(frame, 720, 480)
            else:
                tx_frame = frame

            qimg = QImage(frame, 720, 480, QImage.Format_RGB888)
            pix = QPixmap.fromImage(qimg).scaled(480, 270, Qt.KeepAspectRatio, Qt.FastTransformation)
            self.orig_screen.setPixmap(pix)
            if self.fullscreen_target == 'tx' and self.fullscreen_view:
                self.fullscreen_view.setPixmap(pix)
            if self.mpeg_encoder:
                self.mpeg_encoder.push_frame(tx_frame)

        self.frame_count += 1

    def _field_split(self, rgb: bytes, w: int, h: int) -> bytes:
        """
        True interlaced field weave:
        Combines the current field (even or odd lines) with the previous frame's field.
        This represents combing artifacts realistically on a progressive screen,
        maintains 100% video brightness, and avoids the seizure-inducing 30Hz flicker.
        """
        expected = w * h * 3
        if len(rgb) != expected:
            return rgb
        arr = np.frombuffer(rgb, dtype=np.uint8).reshape(h, w, 3).copy()
        
        # Initialize last_tx_frame if not exists
        if not hasattr(self, 'last_tx_frame') or self.last_tx_frame is None or self.last_tx_frame.shape != arr.shape:
            self.last_tx_frame = arr.copy()
            self.tx_field = 0

        # Weave
        if self.tx_field == 0:
            self.last_tx_frame[0::2] = arr[0::2]   # weave even lines
        else:
            self.last_tx_frame[1::2] = arr[1::2]   # weave odd lines
            
        self.tx_field ^= 1
        return self.last_tx_frame.tobytes()

    def _make_test_pattern(self, w: int, h: int) -> bytes:
        """SMPTE EG 1 colour bars + PLUGE + bouncing dot + scrolling label."""
        img  = Image.new('RGB', (w, h))
        draw = ImageDraw.Draw(img)
        bar_h = int(h * 0.75)

        bars = [(192,192,192),(192,192,0),(0,192,192),(0,192,0),
                (192,0,192),(192,0,0),(0,0,192),(19,19,19)]
        bw = w // 8
        for i, c in enumerate(bars):
            draw.rectangle([i*bw, 0, (i+1)*bw, bar_h], fill=c)

        # PLUGE
        draw.rectangle([0,        bar_h, w//3,   h], fill=(0,  0,  0))
        draw.rectangle([w//3,     bar_h, 2*w//3, h], fill=(19, 19, 19))
        draw.rectangle([2*w//3,   bar_h, w,      h], fill=(192,192,192))

        # Bouncing ball
        r = max(6, min(w, h) // 40)
        self.ball_x = max(r, min(self.ball_x + self.ball_dx, w - r))
        self.ball_y = max(r, min(self.ball_y + self.ball_dy, bar_h - r))
        if self.ball_x in (r, w - r):  self.ball_dx = -self.ball_dx; self.bounced = True
        if self.ball_y in (r, bar_h - r): self.ball_dy = -self.ball_dy; self.bounced = True
        draw.ellipse([self.ball_x-r, self.ball_y-r,
                      self.ball_x+r, self.ball_y+r], fill=(255,255,255))

        # Scrolling standard label
        self.text_x -= 2
        if self.text_x < -300: self.text_x = w
        stds = ["ATSC (8VSB)", "DVB-S2 (8PSK)", "J.83B (64QAM)",
                "DVB-T2 (256QAM)", "DVB-T (OFDM QPSK)"]
        lbl = stds[self.std_combo.currentIndex()]
        try:
            font = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                                      max(11, h // 30))
        except Exception:
            font = ImageFont.load_default()
        draw.text((self.text_x, bar_h + 4), lbl, fill=(255, 255, 0), font=font)

        return img.tobytes()

    # ─────────────────────────────────────────────────────────────
    #  RX FRAME / AUDIO HANDLERS
    # ─────────────────────────────────────────────────────────────
    def on_mpeg_frame(self, raw_rgb: bytes):
        """
        Received raw RGB24 frame from MPEG-2 decoder.
        Frame already contains authentic MPEG-2 error concealment artifacts
        from the -ec deblock+favor_inter flag — no additional processing needed.
        """
        self._rx_pkt_count += 1
        self.last_frame_recv = time.time()
        w, h = 480, 270

        if len(raw_rgb) != w * h * 3:
            return

        img = Image.frombytes('RGB', (w, h), raw_rgb)
        self.last_decoded = img.copy()
        self.current_rx_frame = img.copy()

        qimg   = QImage(raw_rgb, w, h, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        self.recv_screen.setPixmap(pixmap)
        if self.fullscreen_target == 'rx' and self.fullscreen_view:
            self.fullscreen_view.setPixmap(pixmap)

    def on_mpeg_audio(self, pcm: bytes):
        pass

    # ─────────────────────────────────────────────────────────────
    #  METRICS + RX DISPLAY UPDATERS
    # ─────────────────────────────────────────────────────────────
    def update_metrics(self):
        self.on_impairment_changed()

        lock = self._lock_pct
        if lock == 0:
            self.banner_lbl.setText("NO SIGNAL\n[ SEARCHING FOR CHANNELS ]")
            self.banner_lbl.setStyleSheet(
                "background:#1c1e22;border:2px solid #da3633;"
                "border-radius:5px;padding:6px;color:#da3633;")
        elif lock < 40:
            garbled = ''.join(
                random.choice('#?!*@%') if c != ' ' and random.random() < 0.4 else c
                for c in self.channel_name)
            self.banner_lbl.setText(f"WEAK  CH {self.channel_number}\n[{garbled}]")
            self.banner_lbl.setStyleSheet(
                "background:#1c1e22;border:2px solid #e3b341;"
                "border-radius:5px;padding:6px;color:#e3b341;")
        else:
            self.banner_lbl.setText(
                f"LIVE  CH {self.channel_number}\n[{self.channel_name}]")
            self.banner_lbl.setStyleSheet(
                "background:#0d1117;border:2px solid #238636;"
                "border-radius:5px;padding:6px;color:#3fb950;")

        tx = self._tx_pkt_count * 2
        rx = self._rx_pkt_count * 2
        self._tx_pkt_count = 0
        self._rx_pkt_count = 0

        _, w, h, _, _ = DTV_RESOLUTIONS[self.res_combo.currentIndex()]
        stds = ["ATSC", "DVB-S2", "J.83B", "DVB-T2", "DVB-T"]
        il = "i" if self.interlace_checkbox.isChecked() else "p"
        hw = HW_ENC.replace('mpeg2', 'HW').replace('video', 'SW').upper()
        self.metrics_lbl.setText(
            f"  {stds[self.std_combo.currentIndex()]}  |  "
            f"{w}x{h}{il}@{self.fps}  |  {hw}  |  "
            f"TX {tx} pkt/s  RX {rx} pkt/s  |  "
            f"Lock: {lock}%  SNR: {self._effective_snr:.1f} dB  |  "
            f"BW: {self.bitrate_kbps} kbps"
        )

        # Update Stats Labels
        if self.video_file:
            v_codec = getattr(self, 'file_v_codec', 'Unknown')
            v_res   = getattr(self, 'file_v_res', 'Unknown')
            v_fps   = getattr(self, 'file_v_fps', 'Unknown')
            self.stats_tx_lbl.setText(f"TX Src: {v_res} @ {v_fps}fps ({v_codec})")
            self.preview_stats_lbl.setText(f"Res: {v_res} | Codec: {v_codec}")
        else:
            self.stats_tx_lbl.setText(f"TX Src: 720x480 @ {self.fps}fps (Synth)")
            self.preview_stats_lbl.setText("Res: 720x480 | Codec: Synth")

        # Audio Stats
        self.stats_aud_lbl.setText(f"Audio Codec: {self.audio_codec.upper()} (192kbps stereo)")

        # RX Stats
        deint_str = "yadif" if self.deinterlace_rx else "None"
        if self._lock_pct > 0 and self.last_frame_recv > 0 and (time.time() - self.last_frame_recv < 2.5):
            self.stats_rx_lbl.setText(f"RX Dec: {self.video_width}x{self.video_height} (Deint: {deint_str})")
            self.rx_stats_lbl.setText(f"Res: {self.video_width}x{self.video_height} | Deint: {deint_str}")
        else:
            self.stats_rx_lbl.setText("RX Dec: No Signal")
            self.rx_stats_lbl.setText("Res: --x-- | Deint: --")

    def update_rx_display(self):
        """Show themed outage screen when signal lost or frame timeout."""
        # Update playback timeline slider and stats label
        if self.video_file and getattr(self, 'video_duration', 0.0) > 0 and not getattr(self, 'playback_paused', False):
            elapsed = time.time() - getattr(self, 'play_start_time', time.time())
            curr_pos = getattr(self, 'play_start_offset', 0.0) + elapsed
            
            if elapsed >= self.video_duration:
                # Wrap start time to match modulo loop
                self.play_start_time += self.video_duration * (elapsed // self.video_duration)
                curr_pos = curr_pos % self.video_duration

            if hasattr(self, 'timeline_slider') and not self.timeline_slider.isSliderDown():
                self.timeline_slider.setValue(int(curr_pos))
                
            if hasattr(self, 'time_lbl'):
                cur_min, cur_sec = divmod(int(curr_pos), 60)
                dur_min, dur_sec = divmod(int(self.video_duration), 60)
                self.time_lbl.setText(f"{cur_min:02d}:{cur_sec:02d} / {dur_min:02d}:{dur_sec:02d}")

        lock = self._lock_pct
        now  = time.time()
        
        if lock == 0:
            self._draw_no_signal()
        else:
            if self.last_frame_recv == 0.0:
                if now - getattr(self, 'decoder_start_time', now) > 2.5:
                    self._draw_no_signal()
            else:
                if now - self.last_frame_recv > 2.5:
                    self._draw_no_signal()

    def _get_font(self, name="DejaVuSans-Bold.ttf", size=12):
        try:
            return ImageFont.truetype(f"/usr/share/fonts/TTF/{name}", size)
        except Exception:
            return ImageFont.load_default()

    def _draw_no_signal(self):
        theme = self.theme_combo.currentIndex()
        W, H  = 480, 270

        if theme == 0:    # Terrestrial — digital set-top box card
            img  = Image.new('RGB', (W, H), (15, 15, 18))
            draw = ImageDraw.Draw(img)
            fb = self._get_font("DejaVuSans-Bold.ttf", 15)
            fn = self._get_font("DejaVuSans.ttf", 11)
            # Draw a modern outlined warning box
            draw.rectangle([18, H//2-26, W-18, H//2+26], fill=(0,0,0), outline=(220,70,70), width=1)
            draw.text((36, H//2-20), "NO DIGITAL SIGNAL", fill=(255,255,255), font=fb)
            draw.text((36, H//2+2),  "Check antenna & coaxial connection", fill=(200,200,200), font=fn)

        elif theme == 1:  # Satellite — blue screen
            img  = Image.new('RGB', (W, H), (10, 42, 95))
            draw = ImageDraw.Draw(img)
            fb   = self._get_font("DejaVuSans-Bold.ttf", 14)
            fn   = self._get_font("DejaVuSans.ttf", 11)
            draw.text((22, 26),  "SATELLITE RECEIVER  Error 771", fill=(235,203,139), font=fb)
            draw.line([22, 48, W-22, 48], fill=(235,203,139), width=2)
            draw.text((22, 56),  "Searching for satellite signal...", fill=(255,255,255), font=fb)
            for i, line in enumerate([
                "• Check LNB coaxial connection",
                "• Verify dish pointing (El/Az angles)",
                "• LNB voltage: 13V (V-pol) / 18V (H-pol)",
                "• Rain fade: Ku/Ka bands severely attenuated in heavy rain",
            ]):
                draw.text((22, 98 + i*18), line, fill=(180,180,200), font=fn)

        else:             # Cable — gray outage
            img  = Image.new('RGB', (W, H), (22, 22, 24))
            draw = ImageDraw.Draw(img)
            fb   = self._get_font("DejaVuSans-Bold.ttf", 14)
            fn   = self._get_font("DejaVuSans.ttf", 11)
            draw.text((22, 28), "CABLE TV  —  NO SIGNAL", fill=(220,70,70), font=fb)
            draw.line([22, 50, W-22, 50], fill=(220,70,70), width=2)
            draw.text((22, 60), "No cable signal detected on this outlet.", fill=(255,255,255), font=fb)
            draw.text((22, 96), "Error 504  —  Link Down", fill=(150,150,150), font=fn)
            draw.text((22, 118), "Please check coaxial wall outlet and", fill=(180,180,180), font=fn)
            draw.text((22, 138), "the cable connected to the rear panel.", fill=(180,180,180), font=fn)
            draw.text((22, 180), "Support: 1-800-DTV-PLAY", fill=(80,120,200), font=fn)

        self.current_rx_frame = img.copy()
        qimg = QImage(img.tobytes('raw','RGB'), W, H, QImage.Format_RGB888)
        pix = QPixmap.fromImage(qimg)
        self.recv_screen.setPixmap(pix)
        if self.fullscreen_target == 'rx' and self.fullscreen_view:
            self.fullscreen_view.setPixmap(pix)

    # ─────────────────────────────────────────────────────────────
    #  RECORDING
    # ─────────────────────────────────────────────────────────────
    def _toggle_rec(self, checked):
        self.recording = checked
        if checked:
            self.recording_frames = []
            self.record_start_time = time.time()
            self.timer_record.start(int(1000 / max(self.fps, 1)))
            self.btn_record.setText("Stop Recording")
            self.btn_record.setStyleSheet(
                "background:#da3633;color:white;font-weight:bold;")
            self.rec_lbl.setText("Recording RX (MPEG-TS packets to disk)...")
            self.btn_save.setEnabled(False)
            
            # Open the temporary TS capture file
            try:
                rec_path = '/home/hunter/.gemini/antigravity/brain/b1720ff7-ba1e-4e91-ac3e-9526e6a3cfff/scratch/rx_capture.ts'
                self._rec_file = open(rec_path, 'wb')
                if self.mpeg_decoder:
                    self.mpeg_decoder.recording_file = self._rec_file
            except Exception as e:
                print(f"[REC] Failed to open capture file: {e}")
                self._rec_file = None
        else:
            self.timer_record.stop()
            if hasattr(self, 'record_start_time'):
                self.record_elapsed = time.time() - self.record_start_time
            self.btn_record.setText("Record RX")
            self.btn_record.setStyleSheet("")
            
            # Close the capture file
            if hasattr(self, '_rec_file') and self._rec_file:
                try:
                    if self.mpeg_decoder:
                        self.mpeg_decoder.recording_file = None
                    self._rec_file.close()
                except Exception:
                    pass
            self._rec_file = None
            
            # Check if file exists and has size
            rec_path = '/home/hunter/.gemini/antigravity/brain/b1720ff7-ba1e-4e91-ac3e-9526e6a3cfff/scratch/rx_capture.ts'
            has_data = os.path.exists(rec_path) and os.path.getsize(rec_path) > 0
            if has_data or len(self.recording_frames) > 0:
                sz = os.path.getsize(rec_path) / 1024.0 if os.path.exists(rec_path) else 0.0
                self.rec_lbl.setText(f"Stopped — {sz:.1f} KB TS stream captured, {len(self.recording_frames)} frames")
                self.btn_save.setEnabled(True)
            else:
                self.rec_lbl.setText("Stopped — no data captured")
                self.btn_save.setEnabled(False)

    def _record_tick(self):
        if self.recording and self.current_rx_frame:
            self.recording_frames.append(self.current_rx_frame.copy())
            self.rec_lbl.setText(f"Recording...  {len(self.recording_frames)} frames")

    def _save_rec(self):
        if not self.recording_frames:
            return
            
        rec_path = '/home/hunter/.gemini/antigravity/brain/b1720ff7-ba1e-4e91-ac3e-9526e6a3cfff/scratch/rx_capture.ts'
        has_audio_input = os.path.exists(rec_path) and os.path.getsize(rec_path) > 0
        
        fname, _ = QFileDialog.getSaveFileName(
            self, "Save RX Recording", "/home/hunter/rx_recording.mp4",
            "MP4 Video (*.mp4)")
        if not fname: return

        # Determine target resolution on GUI thread
        idx = self.rec_res_combo.currentIndex()
        if idx == 0:
            target_w = self.video_width
            target_h = self.video_height
        elif idx == 1:
            target_w, target_h = 854, 480
        elif idx == 2:
            target_w, target_h = 1024, 576
        elif idx == 3:
            target_w, target_h = 1280, 720
        elif idx == 4:
            target_w, target_h = 1920, 1080
        else:
            target_w, target_h = self.video_width, self.video_height

        # Ensure width and height are divisible by 2 for H.264 compatibility
        target_w = (target_w // 2) * 2
        target_h = (target_h // 2) * 2

        # Calculate actual captured fps to guarantee A/V sync
        elapsed = getattr(self, 'record_elapsed', 0.0)
        n_frames = len(self.recording_frames)
        if elapsed > 0.1 and n_frames > 0:
            actual_fps = n_frames / elapsed
        else:
            actual_fps = max(self.fps, 1)

        self.rec_lbl.setText("Encoding to MP4 (transcoding TS with Audio)..." if has_audio_input else "Encoding to MP4 (video only)...")
        QtWidgets.QApplication.processEvents()
        
        # Transcode using FFmpeg in a background thread to keep UI responsive
        def transcode_worker():
            if has_audio_input:
                cmd = ['ffmpeg', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                       '-framerate', f'{actual_fps:.4f}', '-s', '480x270', '-i', '-',
                       '-vn', '-i', rec_path, '-map', '0:v:0', '-map', '1:a:0?',
                       '-vf', f'setpts=PTS-STARTPTS,scale={target_w}:{target_h}:flags=bicubic',
                       '-af', 'asetpts=PTS-STARTPTS',
                       '-c:v', 'libx264', '-crf', '22', '-preset', 'fast',
                       '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', fname]
            else:
                cmd = ['ffmpeg', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                       '-framerate', f'{actual_fps:.4f}', '-s', '480x270', '-i', '-',
                       '-map', '0:v:0',
                       '-vf', f'setpts=PTS-STARTPTS,scale={target_w}:{target_h}:flags=bicubic',
                       '-c:v', 'libx264', '-crf', '22', '-preset', 'fast',
                       '-pix_fmt', 'yuv420p', fname]
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                # Write all frames to stdin
                for img in self.recording_frames:
                    if proc.poll() is not None:
                        break
                    try:
                        proc.stdin.write(img.tobytes())
                    except Exception:
                        break
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                
                proc.wait(timeout=60)
                if proc.returncode == 0:
                    QtCore.QMetaObject.invokeMethod(self.rec_lbl, "setText",
                        QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, f"Saved: {os.path.basename(fname)}"))
                else:
                    err = proc.stderr.read().decode()
                    QtCore.QMetaObject.invokeMethod(self.rec_lbl, "setText",
                        QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, f"Save failed: transcode error"))
                    print(f"[REC-SAVE] FFmpeg failed:\n{err}")
            except Exception as e:
                QtCore.QMetaObject.invokeMethod(self.rec_lbl, "setText",
                    QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, f"Save failed: {e}"))
                    
        threading.Thread(target=transcode_worker, daemon=True).start()

    # ─────────────────────────────────────────────────────────────
    #  CLOSE / CLEANUP
    # ─────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        print("Shutting down DTV Playground...")
        for t in [self.timer_tx, self.timer_metrics, self.timer_rx]:
            try: t.stop()
            except Exception: pass

        if self.preview_thread:
            try: self.preview_thread.stop()
            except Exception: pass
        if self.preview_proc:
            try: self.preview_proc.terminate()
            except Exception: pass

        if self.mpeg_encoder:
            try: self.mpeg_encoder.stop()
            except Exception: pass
        if self.mpeg_decoder:
            try: self.mpeg_decoder.stop()
            except Exception: pass
        if self.channel_relay:
            try: self.channel_relay.stop()
            except Exception: pass
        if self.aplay_proc:
            try: self.aplay_proc.terminate()
            except Exception: pass
        if self.gr_tb:
            try: self.gr_tb.stop(); self.gr_tb.wait()
            except Exception: pass

        event.accept()


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────
def main():
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle('Fusion')
    win = DtvPlaygroundApp()
    win.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
