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
import sys, os
os.environ["QT_LOGGING_RULES"] = "qt.qpa.wayland.warning=false;qt.qpa.wayland=false"
import time, socket, threading, subprocess, math, random, queue, signal, json
import numpy as np
from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
    QComboBox, QPushButton, QFileDialog, QGroupBox, QCheckBox,
    QProgressBar, QLineEdit, QTabWidget, QScrollArea, QSplitter, QListWidget,
    QFormLayout, QColorDialog
)
from PyQt5.QtGui import QImage, QPixmap, QFont
from PyQt5.QtCore import QTimer, Qt, QThread, pyqtSignal
from PIL import Image, ImageDraw, ImageFont

USER_HOME = os.path.expanduser("~")
APP_DATA_DIR = os.path.join(USER_HOME, ".dtv_playground")
if not os.path.exists(APP_DATA_DIR):
    try: os.makedirs(APP_DATA_DIR)
    except Exception: APP_DATA_DIR = os.getcwd()

sys.path.append(USER_HOME)
try:
    from dtv_simulation import dtv_simulation
    GRC_AVAILABLE = True
except Exception as e:
    print(f"[WARN] GRC unavailable: {e}")
    GRC_AVAILABLE = False


# ─────────────────────────────────────────────────────────────
#  HARDWARE ACCELERATION PROBE
#  Tries QSV → NVENC → AMF → VAAPI → Software.
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
            r = subprocess.run(cmd, timeout=5, capture_output=True)
            return r.returncode == 0
        except Exception:
            return False

    # Intel QSV (Windows/Linux)
    if _try(['ffmpeg', '-y', '-loglevel', 'quiet',
             '-f', 'lavfi', '-i', 'color=black:s=320x240:r=25:d=0.1',
             '-vcodec', 'mpeg2_qsv', '-frames:v', '1', '-f', 'null', '-']):
        HW_ENC       = 'mpeg2_qsv'
        HW_DEC_FLAGS = []
        print("[HW] Intel QSV  (mpeg2_qsv)")
        return

    # NVIDIA NVENC (Supports H.264/HEVC, but check if user wants it for DTV)
    # Note: NVIDIA doesn't support MPEG-2 hardware encoding. 
    # We fallback to software for MPEG-2 but could use NVENC for H.264/AVC DTV if needed.
    
    # AMD AMF (Windows)
    if _try(['ffmpeg', '-y', '-loglevel', 'quiet',
             '-f', 'lavfi', '-i', 'color=black:s=320x240:r=25:d=0.1',
             '-vcodec', 'mpeg2_amf', '-frames:v', '1', '-f', 'null', '-']):
        HW_ENC = 'mpeg2_amf'
        print("[HW] AMD AMF (mpeg2_amf)")
        return

    # VAAPI (Linux only)
    if sys.platform.startswith('linux'):
        dev = '/dev/dri/renderD128'
        if os.path.exists(dev):
            if _try(['ffmpeg', '-y', '-loglevel', 'quiet',
                     '-vaapi_device', dev,
                     '-f', 'lavfi', '-i', 'color=black:s=320x240:r=25:d=0.1',
                     '-vf', 'format=nv12,hwupload',
                     '-vcodec', 'mpeg2_vaapi', '-frames:v', '1', '-f', 'null', '-']):
                HW_ENC       = 'mpeg2_vaapi'
                HW_ENC_FLAGS = ['-vaapi_device', dev]
                VAAPI_VF     = ['format=nv12,hwupload,']
                HW_DEC_FLAGS = []
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
    Listens on tx_port for MPEG-TS datagrams.
    Applies TS-aware byte corruption based on link-budget lock%.
    Preserves 0x47 sync bytes so the decoder can still find packet boundaries.
    Forwards corrupted datagrams to rx_port.
    """
    def __init__(self, get_lock_fn, tx_port=5005, rx_port=5002, parent_win=None):
        super().__init__(daemon=True)
        self.get_lock = get_lock_fn
        self.running  = True
        self.tx_port  = tx_port
        self.rx_port  = rx_port
        self.parent_win = parent_win

        self.rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Increase OS UDP receive buffer to ~8MB to prevent drops on high-bitrate 1080p
        self.rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        self.rx_sock.bind(('127.0.0.1', self.tx_port))
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
            if off + 188 > len(arr):
                break
            if arr[off] != 0x47:
                continue
            # Header: bytes 1-3 (leave mostly intact — only corrupt occasionally)
            if random.random() < ber * 0.3:
                arr[off + 1] ^= random.randint(1, 3)   # flip low PID bits
            # Payload: bytes 4-187 (primary corruption target)
            for i in range(off + 4, off + 188):
                if random.random() < per_byte_err:
                    arr[i] ^= 1 << random.randrange(8)
        return bytes(arr)

    def run(self):
        while self.running:
            try:
                try:
                    data, _ = self.rx_sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except Exception:
                    continue

                num_pkts = len(data) // 188
                if self.parent_win:
                    self.parent_win._tx_pkt_count += num_pkts

                lock = self.get_lock()
                ber  = self._ber(lock)

                # Drop datagrams less aggressively so we can see block artifacts down to 45% lock
                drop = max(0.0, (ber - 0.05) * 1.5)
                if random.random() < drop:
                    continue

                if self.parent_win:
                    self.parent_win._rx_pkt_count += num_pkts

                if ber > 5e-4:
                    data = self._corrupt_ts(data, ber)

                try:
                    self.tx_sock.sendto(data, ('127.0.0.1', self.rx_port))
                except Exception:
                    pass
            except Exception as e:
                print(f"[RELAY] Unexpected error: {e}")

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
                 audio_codec: str = 'mp2', seek_seconds: float = 0.0,
                 tx_port: int = 5005, crf: int = 22,
                 custom_enc_args: str = "", preset: str = "medium"):
        super().__init__()
        self.video_file   = video_file
        self.width        = width
        self.height       = height
        self.fps          = fps
        self.bitrate_kbps = bitrate_kbps
        self.interlaced   = interlaced
        self.audio_codec  = audio_codec
        self.seek_seconds = seek_seconds
        self.tx_port      = tx_port
        self.crf          = crf
        self.custom_enc_args = custom_enc_args
        self.preset       = preset
        self.running      = True
        self.proc         = None
        self.err_log      = subprocess.DEVNULL

    def push_frame(self, rgb24: bytes):
        """Synthetic mode: push one raw RGB24 frame via non-blocking queue."""
        if not hasattr(self, '_push_q'):
            self._push_q = queue.Queue(maxsize=4)
            threading.Thread(target=self._push_loop, daemon=True).start()
        try:
            self._push_q.put_nowait(rgb24)
        except queue.Full:
            pass

    def _push_loop(self):
        while self.running:
            try:
                rgb24 = self._push_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if self.proc and self.proc.stdin and not self.proc.stdin.closed:
                try:
                    self.proc.stdin.write(rgb24)
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    break

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
        if self.video_file:
            vf += [f'scale={self.width}:{self.height}:flags=bicubic',
                   f'fps={self.fps}']
        else:
            # Synthetic mode: input is either 720x480 (4:3) or 854x480 (16:9), scale to target resolution
            vf += [f'scale={self.width}:{self.height}:flags=fast_bilinear']
            
        if self.interlaced:
            vf += ['setfield=tff']
            
        # IMPORTANT: VAAPI hwupload must happen AFTER scaling/interlacing in this setup
        if VAAPI_VF:
            vf += VAAPI_VF
            
        vf_str = ','.join(vf) if vf else None

        # Interlaced DCT/motion-estimation for software encoder only
        il_flags = (['-flags', '+ildct+ilme', '-alternate_scan', '1']
                    if self.interlaced and codec == 'mpeg2video' else [])

        import shlex
        custom_args_list = []
        if self.custom_enc_args:
            try:
                custom_args_list = shlex.split(self.custom_enc_args)
            except Exception as e:
                print(f"[ENC] Error parsing custom encoder args: {e}")

        # check if custom args overrides codec
        for i, arg in enumerate(custom_args_list):
            if arg in ('-vcodec', '-c:v') and i + 1 < len(custom_args_list):
                codec = custom_args_list[i+1]

        common_enc = (
            ['-vcodec', codec, '-b:v', bv, '-maxrate', mv, '-bufsize', buf,
             '-g', str(gop), '-bf', '2']
            + il_flags
        )
        
        if 'h264' in codec or 'x264' in codec:
            common_enc += ['-crf', str(self.crf), '-preset', self.preset]

        common_enc += (
            ['-acodec', self.audio_codec, '-b:a', '192k', '-ar', '48000', '-ac', '2']
            + custom_args_list
            + ['-f', 'mpegts', 'pipe:1']
        )

        seek_opt = []
        seek_sec = getattr(self, 'seek_seconds', 0.0)
        if seek_sec > 0:
            seek_opt = ['-ss', f'{seek_sec:.2f}']

        if self.video_file:
            has_aud = self._has_audio()
            if has_aud:
                cmd = (['ffmpeg', '-y', '-loglevel', 'error']
                       + ['-fflags', '+genpts', '-stream_loop', '-1', '-re',
                          '-i', self.video_file]
                       + seek_opt
                       + HW_ENC_FLAGS
                       + (['-vf', vf_str] if vf_str else [])
                       + ['-map', '0:v:0', '-map', '0:a:0']
                       + common_enc)
            else:
                # Inject silent audio track so the MPEG-TS stream always has audio
                cmd = (['ffmpeg', '-y', '-loglevel', 'error']
                       + ['-fflags', '+genpts', '-stream_loop', '-1', '-re',
                          '-i', self.video_file,
                          '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000']
                       + seek_opt
                       + HW_ENC_FLAGS
                       + (['-vf', vf_str] if vf_str else [])
                       + ['-map', '0:v:0', '-map', '1:a:0']
                       + common_enc)
            return cmd, subprocess.DEVNULL
        else:
            # Synthetic: raw video from stdin. Determine if we are sending 4:3 or 16:9
            synth_w = 848 if self.width / self.height > 1.4 else 720
            audio_src = 'sine=frequency=1000:sample_rate=48000:d=99999'
            cmd = (['ffmpeg', '-y', '-loglevel', 'error',
                    '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                    '-s', f'{synth_w}x480',
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
              
        err_log_path = os.path.join(APP_DATA_DIR, "encoder_stderr.log")
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
                tx_sock.sendto(chunk, ('127.0.0.1', self.tx_port))
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
                try:
                    self.proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:
                pass


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
                 deinterlace: bool = False, port: int = 5002,
                 custom_dec_args: str = ""):
        super().__init__()
        self.width       = width
        self.height      = height
        self.fps         = fps
        self.deinterlace = deinterlace
        self.port        = port
        self.custom_dec_args = custom_dec_args
        self.running     = True
        # Decoder now outputs 960x540 for better scaling quality
        self.frame_size  = 960 * 540 * 3

        self.video_proc  = None
        self.audio_proc  = None
        self.aplay_proc  = None
        self.sock        = None

        # Thread-safe control states
        self.vol_level   = 0.70
        self.lock_level  = 0
        self.recording_file = None
        self.respawn_lock = threading.Lock()

        # Separate queues so slow video decoder can't starve audio and vice-versa.
        # Maxsize must be large enough to absorb OS scheduling jitter at 20Mbps (1080p).
        # We rely primarily on the 8MB OS UDP socket buffer to prevent packet drops.
        # Keeping these internal queues smaller (128 = ~168KB) prevents A/V desync latency.
        self.vq = queue.Queue(maxsize=128)
        self.aq = queue.Queue(maxsize=128)

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
        # Scale directly to screen size to offload GUI CPU.
        # Use a higher resolution for the decoder output to improve quality on large screens.
        vf.append('scale=960:540:flags=lanczos')
        vf.append('format=rgb24')
        
        import shlex
        custom_args_list = []
        if self.custom_dec_args:
            try:
                custom_args_list = shlex.split(self.custom_dec_args)
            except Exception as e:
                print(f"[DEC-V] Error parsing custom decoder args: {e}")

        return (
            ['ffmpeg', '-y', '-loglevel', 'error']
            + HW_DEC_FLAGS
            + ['-max_error_rate', '1.0',
               '-fflags', 'nobuffer',
               '-err_detect', 'ignore_err',
               '-ec', 'deblock+favor_inter',
               '-analyzeduration', '100000',
               '-probesize', '16384',
               '-f', 'mpegts', '-i', 'pipe:0']
            + custom_args_list
            + ['-map', '0:v?',
               '-vf', ','.join(vf),
               '-f', 'rawvideo', '-pix_fmt', 'rgb24',
               'pipe:1']
        )

    def _audio_cmd(self):
        import shlex
        custom_args_list = []
        if self.custom_dec_args:
            try:
                custom_args_list = shlex.split(self.custom_dec_args)
            except Exception as e:
                print(f"[DEC-A] Error parsing custom decoder args: {e}")

        return (
            ['ffmpeg', '-y', '-loglevel', 'error',
             '-max_error_rate', '1.0',
             '-fflags', 'nobuffer',
             '-err_detect', 'ignore_err',
             '-analyzeduration', '100000',
             '-probesize', '16384',
             '-f', 'mpegts', '-i', 'pipe:0']
            + custom_args_list
            + ['-map', '0:a?',
               '-f', 's16le', '-acodec', 'pcm_s16le',
               '-ac', '2', '-ar', '48000',
               'pipe:1']
        )

    def _spawn(self, cmd, name):
        log_path = os.path.join(APP_DATA_DIR, f"decoder_{name}_stderr.log")
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
            if proc and proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    if self.running:
                        print(f"[{proc_name}] pipe broken — restarting")
                        self._respawn(proc_name, proc)

    def _respawn(self, proc_name: str, old_proc=None):
        with self.respawn_lock:
            current = getattr(self, proc_name)
            if old_proc is not None and current is not old_proc:
                return
                
            # Reset the watchdog times on the parent main window so it doesn't immediately force recovery again!
            if hasattr(self, 'parent_win') and self.parent_win:
                self.parent_win.last_recovery_time = time.time()
                self.parent_win.decoder_start_time = time.time()
                self.parent_win.last_frame_recv = 0.0

            if current:
                try:
                    if current.stdin:
                        current.stdin.close()
                    current.terminate()
                    try:
                        current.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        current.kill()
                except Exception:
                    pass
                if hasattr(current, 'err_log') and current.err_log != subprocess.DEVNULL:
                    try: current.err_log.close()
                    except Exception: pass
            if proc_name == 'video_proc':
                # Clear queue to prevent reading corrupted backlog packets
                while not self.vq.empty():
                    try: self.vq.get_nowait()
                    except queue.Empty: break
                if self._start_video():
                    threading.Thread(target=self._read_video, daemon=True).start()
            else:
                # Clear queue to prevent reading corrupted backlog packets
                while not self.aq.empty():
                    try: self.aq.get_nowait()
                    except queue.Empty: break
                if self._start_audio():
                    threading.Thread(target=self._read_audio, daemon=True).start()

    # ── reader threads (proc stdout → signal/aplay) ───────────
    def _read_video(self):
        proc = self.video_proc
        buffer = bytearray()
        
        while self.running and proc is self.video_proc:
            try:
                # Read exactly frame_size bytes, looping if necessary
                needed = self.frame_size - len(buffer)
                if needed > 0:
                    chunk = proc.stdout.read(needed)
                    if not chunk:
                        # EOF hit. If running, respawn the decoder.
                        if self.running:
                            print("[DEC-V] video decoder EOF — restarting")
                            self._respawn('video_proc', proc)
                        break
                    buffer.extend(chunk)
                
                if len(buffer) == self.frame_size:
                    self.frame_ready.emit(bytes(buffer))
                    buffer.clear()
                    
            except Exception as e:
                if self.running:
                    print(f"[DEC-V] read error: {e} — restarting")
                    self._respawn('video_proc', proc)
                break

    def _read_audio(self):
        CHUNK = 9600  # 50 ms @ 48kHz stereo S16LE
        proc = self.audio_proc
        while self.running and proc is self.audio_proc:
            try:
                raw = proc.stdout.read(CHUNK)
                if raw:
                    self._write_audio_to_aplay(raw)
                else:
                    if self.running:
                        print("[DEC-A] audio decoder EOF — restarting")
                        self._respawn('audio_proc', proc)
                    break
            except Exception as e:
                if self.running:
                    print(f"[DEC-A] read error: {e} — restarting")
                    self._respawn('audio_proc', proc)
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
                            ['aplay', '-t', 'raw', '-f', 'S16_LE', '-c', '2', '-r', '48000', '--buffer-time=50000'],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                        )
                    except Exception:
                        self.aplay_proc = None
                
                if not self.aplay_proc:
                    # ffplay fallback: works on Windows, macOS, Linux
                    self.aplay_proc = subprocess.Popen(
                        ['ffplay', '-loglevel', 'quiet', '-nodisp', '-autoexit', 
                         '-fflags', 'nobuffer', '-flags', 'low_delay', 
                         '-f', 's16le', '-ac', '2', '-ar', '48000', '-i', '-'],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
            except Exception as e:
                print(f"[AUDIO] failed to start background player: {e}")
                return

        if self.aplay_proc and self.aplay_proc.stdin:
            try:
                self.aplay_proc.stdin.write(pcm)
                self.aplay_proc.stdin.flush()
            except Exception as e:
                print(f"[AUDIO] aplay write failed: {e}")
                try: self.aplay_proc.terminate()
                except Exception: pass
                self.aplay_proc = None

    # ── main loop ─────────────────────────────────────────────
    def run(self):
        # Create and bind socket inside background thread with retry loop to avoid port reuse crashes
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Increase OS UDP receive buffer to ~8MB to prevent drops on high-bitrate 1080p
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
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
            print("[DEC] Could not bind or thread stopped.")
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
                time.sleep(0.01)
                continue

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
                    if proc.stdin:
                        proc.stdin.close()
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except Exception:
                    pass
                if hasattr(proc, 'err_log') and proc.err_log != subprocess.DEVNULL:
                    try: proc.err_log.close()
                    except Exception: pass
        if self.sock:
            try: self.sock.close()
            except Exception: pass


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


# ─────────────────────────────────────────────────────────────
#  CLICKABLE IMAGE LABEL
# ─────────────────────────────────────────────────────────────
class ClickableLabel(QLabel):
    double_clicked = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Double-click for Fullscreen")
        self.setAlignment(Qt.AlignCenter)
        
    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()


# ─────────────────────────────────────────────────────────────
#  FULLSCREEN DIALOG WINDOW
# ─────────────────────────────────────────────────────────────
class FullscreenWindow(QLabel):
    closed = pyqtSignal()

    def __init__(self, title, parent=None, windowed=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.windowed = windowed
        
        if self.windowed:
            self.setWindowFlags(Qt.Window)
            self.resize(1280, 720)
        else:
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
            self.showFullScreen()
            
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#000;")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Double-click or press Escape to exit")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()
        self.show()

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

    # Signals for thread-safe UI updates
    restart_complete = pyqtSignal()
    probe_complete = pyqtSignal()

    def get_lock_pct(self):  return self._lock_pct

    def __init__(self):
        super().__init__()
        self.fullscreen_view = None
        self.fullscreen_target = None
        self.setWindowTitle("DTV SDR Playground  —  MPEG-2 TS")
        self.resize(1360, 880)
        self._apply_stylesheet()

        self.gr_tb = None

        # ── State ─────────────────────────────────────────────
        self.video_file      = ''
        self.playlist        = []
        self.seek_seconds    = 0.0
        self.playback_paused = False
        self.current_theme = 'dark'
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

        self._restart_lock = threading.Lock()
        self._is_restarting = False

        # Encoder / Decoder / Relay
        self.mpeg_encoder = None
        self.mpeg_decoder = None
        self.channel_relay = None

        # aplay
        self.aplay_proc = None

        # ── Build UI ──────────────────────────────────────────
        self.init_ui()
        self.on_service_type_changed()
        self.on_impairment_changed()

        # ── Channel Relay ─────────────────────────────────────
        tx_port = int(self.tx_port_input.text()) if hasattr(self, 'tx_port_input') else 5005
        rx_port = int(self.rx_port_input.text()) if hasattr(self, 'rx_port_input') else 5002
        self.channel_relay = PythonChannelRelay(get_lock_fn=self.get_lock_pct, tx_port=tx_port, rx_port=rx_port, parent_win=self)
        self.channel_relay.start()
        print(f"[RELAY] TX:{tx_port} → RX:{rx_port}")

        # ── Timers ────────────────────────────────────────────
        self.timer_tx = QTimer()
        self.timer_tx.timeout.connect(self.media_loop)

        self.timer_metrics = QTimer()
        self.timer_metrics.timeout.connect(self.update_metrics)

        self.timer_rx = QTimer()
        self.timer_rx.timeout.connect(self.update_rx_display)

        # ── Signals ───────────────────────────────────────────
        self.restart_complete.connect(self._on_restart_complete)
        self.probe_complete.connect(self._on_probe_complete)

        # ── Start Pipeline ────────────────────────────────────
        self._restart_pipeline()

        # ── Start Timers ──────────────────────────────────────
        self.timer_tx.start(int(1000 / max(self.fps, 1)))
        self.timer_metrics.start(400)
        self.timer_rx.start(80)

        self._tx_pkt_count = 0
        self._rx_pkt_count = 0

        # Delay GRC start so GUI shows up immediately
        QTimer.singleShot(1000, self._start_grc)

    def _start_grc(self):
        """Delayed initialization of GNU Radio reference block."""
        if GRC_AVAILABLE:
            try:
                print("[GRC] Initializing DSP reference block...")
                self.gr_tb = dtv_simulation()
                self.gr_tb.start()
                print("[GRC] DSP block started successfully.")
            except Exception as e:
                print(f"[GRC] Initialization failed (likely memory or shm issue): {e}")
                self.gr_tb = None

    # ── Startup helpers ────────────────────────────────────────
    def _start_encoder(self):
        if self.mpeg_encoder:
            self._safe_stop_thread('mpeg_encoder')
        
        seek_sec = getattr(self, 'seek_seconds', 0.0)
        tx_port = int(self.tx_port_input.text()) if hasattr(self, 'tx_port_input') else 5005
        crf = self.custom_crf_slider.value() if hasattr(self, 'custom_crf_slider') else 22
        preset = self.custom_preset_combo.currentText() if hasattr(self, 'custom_preset_combo') else "medium"
        custom_enc_args = self.custom_enc_args_input.text() if hasattr(self, 'custom_enc_args_input') else ""

        vid_file = "" if self.playback_paused else self.video_file
        self.mpeg_encoder = MpegTsEncoderThread(
            video_file=vid_file,
            width=self.video_width, height=self.video_height,
            fps=self.fps, bitrate_kbps=self.bitrate_kbps,
            interlaced=self.interlaced,
            audio_codec=self.audio_codec,
            seek_seconds=seek_sec,
            tx_port=tx_port,
            crf=crf,
            custom_enc_args=custom_enc_args,
            preset=preset
        )
        self.mpeg_encoder.start()
        # TX display preview (small ffmpeg for UI only)
        if not self.playback_paused:
            self._start_preview()

    def _start_decoder(self):
        if self.mpeg_decoder:
            self._safe_stop_thread('mpeg_decoder')
        self.last_decoded    = None
        self.last_frame_recv = 0.0
        self.decoder_start_time = time.time()
        
        rx_port = int(self.rx_port_input.text()) if hasattr(self, 'rx_port_input') else 5002
        custom_dec_args = self.custom_dec_args_input.text() if hasattr(self, 'custom_dec_args_input') else ""

        self.mpeg_decoder = MpegTsDecoderThread(
            width=self.video_width, height=self.video_height,
            fps=self.fps, deinterlace=self.deinterlace_rx,
            port=rx_port, custom_dec_args=custom_dec_args
        )
        self.mpeg_decoder.parent_win = self
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

        seek_opt = []
        seek_sec = getattr(self, 'seek_seconds', 0.0)
        if seek_sec > 0:
            seek_opt = ['-ss', f'{seek_sec:.2f}']

        cmd = ['ffmpeg', '-y', '-loglevel', 'error'] + [
               '-fflags', '+genpts', '-stream_loop', '-1', '-re',
               '-i', self.video_file] + seek_opt + [
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

    def on_tx_preview_enable_changed(self, state):
        enabled = (state == Qt.Checked)
        if enabled:
            self._start_preview()
        else:
            if self.preview_proc:
                try:
                    if self.preview_proc.stdin: self.preview_proc.stdin.close()
                except Exception: pass
                try:
                    if self.preview_proc.stdout: self.preview_proc.stdout.close()
                except Exception: pass
                try:
                    self.preview_proc.terminate()
                    self.preview_proc.wait(timeout=0.2)
                except Exception: pass
                self.preview_proc = None
            self._safe_stop_thread('preview_thread')
            self.orig_screen.setText("Preview Disabled")
            self.orig_screen.setStyleSheet("background:#000; color:#555; font-size: 14px; font-weight: bold; border:1px solid #30363d;")

    def on_rx_preview_enable_changed(self, state):
        enabled = (state == Qt.Checked)
        if not enabled:
            self.recv_screen.setText("Preview Disabled")
            self.recv_screen.setStyleSheet("background:#000; color:#555; font-size: 14px; font-weight: bold; border:1px solid #30363d;")
        else:
            if self._lock_pct == 0:
                self._draw_no_signal()
            else:
                self.recv_screen.setStyleSheet("background:#000; border:1px solid #30363d;")

    def _safe_stop_thread(self, attr_name):
        thread = getattr(self, attr_name, None)
        if thread:
            try:
                thread.stop()
                thread.wait()
            except Exception:
                pass
            if not hasattr(self, '_old_threads'):
                self._old_threads = []
            self._old_threads.append(thread)
            setattr(self, attr_name, None)
        if hasattr(self, '_old_threads'):
            self._old_threads = [t for t in self._old_threads if not t.isFinished()]

    def on_toggle_theme(self):
        if self.current_theme == 'dark':
            self.current_theme = 'light'
            if hasattr(self, 'btn_toggle_theme'):
                self.btn_toggle_theme.setText("Dark Mode")
        else:
            self.current_theme = 'dark'
            if hasattr(self, 'btn_toggle_theme'):
                self.btn_toggle_theme.setText("Light Mode")
        self._apply_stylesheet(self.current_theme)

    def on_theme_changed(self, idx):
        if idx == 5:
            if hasattr(self, 'custom_theme_widget'):
                self.custom_theme_widget.setVisible(True)
        else:
            if hasattr(self, 'custom_theme_widget'):
                self.custom_theme_widget.setVisible(False)

    def pick_bg_color(self):
        color = QColorDialog.getColor(QtGui.QColor(self.custom_bg_edit.text()), self)
        if color.isValid():
            self.custom_bg_edit.setText(color.name())

    def pick_border_color(self):
        color = QColorDialog.getColor(QtGui.QColor(self.custom_border_edit.text()), self)
        if color.isValid():
            self.custom_border_edit.setText(color.name())

    def pick_custom_image(self):
        fname, _ = QFileDialog.getOpenFileName(
            self, "Select Custom Outage Image", USER_HOME,
            "Images (*.png *.jpg *.jpeg *.bmp *.gif)"
        )
        if fname:
            self.custom_image_edit.setText(fname)
            self.on_custom_theme_changed()

    def on_pattern_changed(self, idx):
        is_custom_img = (idx == 5)
        if hasattr(self, 'custom_image_widget'):
            self.custom_image_widget.setVisible(is_custom_img)

    def on_custom_theme_changed(self, *args):
        if self._lock_pct == 0:
            self.update_rx_display()

    def _restart_pipeline(self, restart_decoder=True):
        """Stop+restart relay, encoder and decoder when params change."""
        if self._is_restarting:
            self._pending_restart = True
            self._pending_restart_dec = restart_decoder
            return
        self._is_restarting = True
        self._pending_restart = False
        
        # Update seek position to current playback time to avoid "jump back"
        if self.video_file and getattr(self, 'video_duration', 0.0) > 0 and not getattr(self, 'playback_paused', False):
            elapsed = time.time() - getattr(self, 'play_start_time', time.time())
            curr_pos = (getattr(self, 'play_start_offset', 0.0) + elapsed) % self.video_duration
            self.seek_seconds = float(curr_pos)
            self.play_start_offset = float(curr_pos)
            self.play_start_time = time.time()

        old_enc = self.mpeg_encoder
        old_dec = self.mpeg_decoder if restart_decoder else None
        old_prev = getattr(self, 'preview_thread', None)
        old_prev_proc = getattr(self, 'preview_proc', None)
        old_relay = self.channel_relay

        self.mpeg_encoder = None
        if restart_decoder:
            self.mpeg_decoder = None
        self.preview_thread = None
        self.preview_proc = None
        self.channel_relay = None
        
        self._next_restart_decoder = restart_decoder

        def stop_worker():
            # Stop processes in background to prevent UI hangs
            if old_prev_proc:
                try:
                    old_prev_proc.terminate()
                    old_prev_proc.wait(timeout=0.2)
                except Exception: pass
            if old_enc:
                try: old_enc.stop()
                except Exception: pass
            if old_dec:
                try: old_dec.stop()
                except Exception: pass
            if old_prev:
                try: old_prev.stop()
                except Exception: pass
            if old_relay:
                try:
                    old_relay.stop()
                except Exception: pass

            # Now wait for threads to exit
            if old_enc:
                try: old_enc.wait()
                except Exception: pass
            if old_dec:
                try: old_dec.wait()
                except Exception: pass
            if old_prev:
                try: old_prev.wait()
                except Exception: pass
            if old_relay:
                try: old_relay.join(timeout=0.2)
                except Exception: pass
            
            # Notify GUI thread to start new processes safely
            self.restart_complete.emit()

        threading.Thread(target=stop_worker, daemon=True).start()

    def _on_restart_complete(self):
        try:
            # Safely instantiate QThreads in the main GUI thread
            tx_port = int(self.tx_port_input.text()) if hasattr(self, 'tx_port_input') else 5005
            rx_port = int(self.rx_port_input.text()) if hasattr(self, 'rx_port_input') else 5002
            self.channel_relay = PythonChannelRelay(get_lock_fn=self.get_lock_pct, tx_port=tx_port, rx_port=rx_port, parent_win=self)
            self.channel_relay.start()

            if self._next_restart_decoder:
                self._start_decoder()
            self._start_encoder()
            
            self.timer_tx.setInterval(int(1000 / max(self.fps, 1)))
            # Reset TX pattern state
            self.ball_x = self.video_width  // 2
            self.ball_y = self.video_height // 2
            self.text_x = self.video_width
        except Exception as e:
            print(f"[RESTART] Error on restart complete: {e}")
        finally:
            self._is_restarting = False
            
            # If user rapidly clicked presets/playlist during restart, do it again
            if getattr(self, '_pending_restart', False):
                self._restart_pipeline(getattr(self, '_pending_restart_dec', True))

    # ─────────────────────────────────────────────────────────────
    #  STYLESHEET
    # ─────────────────────────────────────────────────────────────
    def _apply_stylesheet(self, theme='dark'):
        if theme == 'dark':
            self.setStyleSheet("""
                QMainWindow, QWidget { background-color: #0d1117; color: #e6edf3;
                                       font-family: 'DejaVu Sans', sans-serif; }
                QGroupBox { border: 1px solid #30363d; border-radius: 6px;
                            margin-top: 8px; font-weight: bold; color: #79c0ff;
                            padding-top: 4px; }
                QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
                QLabel { font-size: 12px; color: #c9d1d9; }
                QLabel#metrics_lbl { background-color: #161b22; color: #8b949e; border-top: 1px solid #30363d; font-family: monospace; font-size: 11px; padding: 4px 8px; }
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
        else:
            self.setStyleSheet("""
                QMainWindow, QWidget { background-color: #f6f8fa; color: #24292f;
                                       font-family: 'DejaVu Sans', sans-serif; }
                QGroupBox { border: 1px solid #d0d7de; border-radius: 6px;
                            margin-top: 8px; font-weight: bold; color: #0969da;
                            padding-top: 4px; }
                QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
                QLabel { font-size: 12px; color: #24292f; }
                QLabel#metrics_lbl { background-color: #ffffff; color: #57606a; border-top: 1px solid #d0d7de; font-family: monospace; font-size: 11px; padding: 4px 8px; }
                QPushButton { background-color: #f6f8fa; border: 1px solid #d0d7de;
                              border-radius: 5px; padding: 6px 10px; font-weight: bold; color: #24292f; }
                QPushButton:hover { background-color: #eaeef2; border-color: #0969da; }
                QPushButton:pressed { background-color: #eaeef2; }
                QSlider::groove:horizontal { border: 1px solid #d0d7de; height: 5px;
                                             background: #eaeef2; border-radius: 2px; }
                QSlider::handle:horizontal { background: #0969da; width: 13px;
                                             margin: -4px 0; border-radius: 6px; }
                QSlider::handle:horizontal:hover { background: #2da44e; }
                QComboBox { background-color: #ffffff; border: 1px solid #d0d7de;
                            border-radius: 4px; padding: 4px 8px; color: #24292f; }
                QComboBox QAbstractItemView { background: #ffffff; selection-background-color: #0969da; }
                QProgressBar { border: 1px solid #d0d7de; border-radius: 4px; text-align: center;
                               background-color: #ffffff; color: #24292f; font-weight: bold; }
                QProgressBar::chunk { background-color: #2da44e; border-radius: 3px; }
                QLineEdit { background-color: #ffffff; border: 1px solid #d0d7de;
                            border-radius: 4px; padding: 4px; color: #24292f; }
                QCheckBox { color: #24292f; }
                QCheckBox::indicator { width: 14px; height: 14px;
                                       border: 1px solid #0969da; border-radius: 3px; }
                QCheckBox::indicator:checked { background-color: #0969da; }
                QTabWidget::pane { border: 1px solid #d0d7de; background: #ffffff; }
                QTabBar::tab { background: #eaeef2; color: #57606a; padding: 8px 14px;
                               border-top-left-radius: 4px; border-top-right-radius: 4px;
                               margin-right: 2px; font-size: 11px; }
                QTabBar::tab:selected { background: #0969da; color: #fff; }
                QTabBar::tab:hover { background: #d0d7de; color: #24292f; }
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
        
        self.btn_toggle_theme = QPushButton("Light Mode")
        self.btn_toggle_theme.setFixedWidth(100)
        self.btn_toggle_theme.clicked.connect(self.on_toggle_theme)
        hdr.addWidget(self.btn_toggle_theme)

        hdr.addWidget(QLabel("CH:")); self.num_input = QLineEdit("7.1")
        self.num_input.setMinimumWidth(45); self.num_input.textChanged.connect(self.on_guide_changed)
        hdr.addWidget(self.num_input); hdr.addWidget(QLabel("Name:"))
        self.name_input = QLineEdit("Antigravity HD"); self.name_input.setMinimumWidth(130)
        self.name_input.textChanged.connect(self.on_guide_changed)
        hdr.addWidget(self.name_input)
        vbox.addLayout(hdr)

        # Splitter
        spl = QSplitter(Qt.Horizontal)
        lw = QWidget(); lw.setMinimumWidth(320)
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
        self.metrics_lbl.setObjectName("metrics_lbl")
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
            "2. DVB-T 720p Rooftop (DVB-T, UHF, 25 km)",
            "3. DVB-S2 1080i Satellite (DVB-S2, Ku-Band, 38000 km, Clear)",
            "4. J.83B 1080p Cable (J.83B, UHF, 1 km)"
        ])
        self.preset_combo.currentIndexChanged.connect(self.on_preset_changed)
        pv.addWidget(self.preset_combo)

        ph = QHBoxLayout()
        self.btn_save_preset = QPushButton("Save Preset...")
        self.btn_load_preset = QPushButton("Load Preset...")
        self.btn_save_preset.clicked.connect(self.on_save_preset)
        self.btn_load_preset.clicked.connect(self.on_load_preset)
        ph.addWidget(self.btn_save_preset)
        ph.addWidget(self.btn_load_preset)
        pv.addLayout(ph)

        pb.setLayout(pv); v.addWidget(pb)

        # Service Type
        st_box = QGroupBox("DTV Service / Transmission"); stv = QVBoxLayout()
        self.service_type_combo = QComboBox()
        self.service_type_combo.addItems([
            "Antenna TV (Over-the-Air)",
            "Cable TV (Coaxial)",
            "Satellite TV"
        ])
        self.service_type_combo.currentIndexChanged.connect(self.on_service_type_changed)
        stv.addWidget(self.service_type_combo); st_box.setLayout(stv); v.addWidget(st_box)

        # Standard
        sb_box = QGroupBox("DTV Standard / Modulation"); sv = QVBoxLayout()
        self.std_combo = QComboBox()
        self.std_combo.currentIndexChanged.connect(self.on_standard_changed)
        sv.addWidget(self.std_combo); sb_box.setLayout(sv); v.addWidget(sb_box)

        # RF Link
        rf = QGroupBox("RF Link & Weather"); rfv = QVBoxLayout(); rfv.setSpacing(3)
        rfv.addWidget(QLabel("Frequency Band"))
        self.freq_band_combo = QComboBox()
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
        self.lna_checkbox = QCheckBox("LNA Gain")
        self.lna_checkbox.toggled.connect(self.on_impairment_changed)
        self.adv_checkbox = QCheckBox("Advanced DSP")
        ex.addWidget(self.lna_checkbox); ex.addWidget(self.adv_checkbox)
        rfv.addLayout(ex)

        rfv.addWidget(QLabel("Custom LNA Gain"))
        self.lna_gain_slider = QSlider(Qt.Horizontal)
        self.lna_gain_slider.setRange(5, 30); self.lna_gain_slider.setValue(12)
        self.lna_gain_slider.valueChanged.connect(self.on_impairment_changed)
        self.lna_gain_lbl = QLabel("12 dB")
        rfv.addWidget(self.lna_gain_slider); rfv.addWidget(self.lna_gain_lbl)

        rfv.addWidget(QLabel("Custom Noise Floor Offset"))
        self.noise_offset_slider = QSlider(Qt.Horizontal)
        self.noise_offset_slider.setRange(-30, 30); self.noise_offset_slider.setValue(0)
        self.noise_offset_slider.valueChanged.connect(self.on_impairment_changed)
        self.noise_offset_lbl = QLabel("0 dB")
        rfv.addWidget(self.noise_offset_slider); rfv.addWidget(self.noise_offset_lbl)

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
        self.theme_combo.addItems([
            "Dark Sleek (Default)",
            "Blue Cyber",
            "Retro Amber",
            "Green Matrix",
            "Light Theme",
            "Custom Designer Theme"
        ])
        self.theme_combo.currentIndexChanged.connect(self.on_theme_changed)
        self.theme_combo.currentIndexChanged.connect(self.on_custom_theme_changed)
        sv.addWidget(self.theme_combo)

        # Box Model Style Selection (Always visible, decoupled from color themes)
        mod_lay = QHBoxLayout()
        mod_lay.setContentsMargins(0, 4, 0, 0)
        mod_lbl = QLabel("Receiver Box Model:")
        self.box_model_combo = QComboBox()
        self.box_model_combo.addItems([
            "ATSC/DVB-T Antenna Box",
            "DVB-S2 Satellite Receiver",
            "Digital Cable TV Box (J.83B)"
        ])
        self.box_model_combo.currentIndexChanged.connect(self.on_custom_theme_changed)
        mod_lay.addWidget(mod_lbl)
        mod_lay.addWidget(self.box_model_combo)
        sv.addLayout(mod_lay)

        # Pattern Selection Layout (Always visible at the top level of the box)
        pat_lay = QHBoxLayout()
        pat_lay.setContentsMargins(0, 4, 0, 0)
        pat_lbl = QLabel("Test Card / Pattern:")
        pat_lay.addWidget(pat_lbl)
        self.custom_pattern_combo = QComboBox()
        self.custom_pattern_combo.addItems([
            "Solid Background Color",
            "SMPTE Color Bars",
            "Color Bars (Rainbow)",
            "Grid / Crosshatch",
            "White Noise (Animated)",
            "Custom Image File"
        ])
        self.custom_pattern_combo.currentIndexChanged.connect(self.on_pattern_changed)
        self.custom_pattern_combo.currentIndexChanged.connect(self.on_custom_theme_changed)
        pat_lay.addWidget(self.custom_pattern_combo)
        sv.addLayout(pat_lay)

        # Custom image row widget (hidden by default)
        self.custom_image_widget = QWidget()
        ci_lay = QHBoxLayout(); ci_lay.setContentsMargins(0, 0, 0, 0)
        ci_lbl = QLabel("Image File:")
        self.custom_image_edit = QLineEdit()
        self.custom_image_edit.textChanged.connect(self.on_custom_theme_changed)
        btn_pick_img = QPushButton("Browse")
        btn_pick_img.clicked.connect(self.pick_custom_image)
        ci_lay.addWidget(ci_lbl)
        ci_lay.addWidget(self.custom_image_edit)
        ci_lay.addWidget(btn_pick_img)
        self.custom_image_widget.setLayout(ci_lay)
        self.custom_image_widget.setVisible(False)
        sv.addWidget(self.custom_image_widget)

        # Custom theme widget designer
        self.custom_theme_widget = QWidget()
        custom_layout = QFormLayout()
        custom_layout.setContentsMargins(0, 4, 0, 0)
        custom_layout.setSpacing(4)

        # Bg color row
        self.custom_bg_edit = QLineEdit("#0f0f12")
        self.custom_bg_edit.textChanged.connect(self.on_custom_theme_changed)
        btn_bg_color = QPushButton("Pick")
        btn_bg_color.clicked.connect(self.pick_bg_color)
        bg_row = QHBoxLayout()
        bg_row.addWidget(self.custom_bg_edit)
        bg_row.addWidget(btn_bg_color)
        custom_layout.addRow("Background Color:", bg_row)

        # Border color/Accent row
        self.custom_border_edit = QLineEdit("#dc4646")
        self.custom_border_edit.textChanged.connect(self.on_custom_theme_changed)
        btn_border_color = QPushButton("Pick")
        btn_border_color.clicked.connect(self.pick_border_color)
        border_row = QHBoxLayout()
        border_row.addWidget(self.custom_border_edit)
        border_row.addWidget(btn_border_color)
        custom_layout.addRow("Border/Accent Color:", border_row)

        # Font family and size
        self.custom_font_family_edit = QLineEdit("DejaVuSans")
        self.custom_font_family_edit.textChanged.connect(self.on_custom_theme_changed)
        custom_layout.addRow("Font Family/Path:", self.custom_font_family_edit)

        self.custom_font_size_edit = QLineEdit("22")
        self.custom_font_size_edit.textChanged.connect(self.on_custom_theme_changed)
        custom_layout.addRow("Font Size (px):", self.custom_font_size_edit)

        # Title
        self.custom_title_edit = QLineEdit("CUSTOM BOX  —  NO SIGNAL")
        self.custom_title_edit.textChanged.connect(self.on_custom_theme_changed)
        custom_layout.addRow("Title Text:", self.custom_title_edit)

        # Message
        self.custom_msg_edit = QLineEdit("Check your tuner settings or coaxial cable connection.")
        self.custom_msg_edit.textChanged.connect(self.on_custom_theme_changed)
        custom_layout.addRow("Message Text:", self.custom_msg_edit)

        self.custom_theme_widget.setLayout(custom_layout)
        self.custom_theme_widget.setVisible(False)
        sv.addWidget(self.custom_theme_widget)

        sk.setLayout(sv); v.addWidget(sk)
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
        self.jpg_slider.valueChanged.connect(self.update_bitrate_label)
        self.jpg_slider.sliderReleased.connect(self.on_bitrate_changed_commit)
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
            "Auto-Detect (Best Available)",
            "Intel QSV (QuickSync)",
            "NVIDIA NVENC (Software fallback for MPEG-2)",
            "AMD AMF (Advanced Media Framework)",
            "Linux VAAPI (AMD/Intel)",
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

        rv.addWidget(QLabel("Recording Aspect Ratio"))
        self.rec_aspect_combo = QComboBox()
        self.rec_aspect_combo.addItems([
            "16:9 (Widescreen)",
            "4:3 (Standard)",
            "Stretch to Fill"
        ])
        self.rec_aspect_combo.setCurrentIndex(0)
        rv.addWidget(self.rec_aspect_combo)

        rv.addWidget(QLabel("On Playback Pause/Stop Broadcast:"))
        self.pause_behavior_combo = QComboBox()
        self.pause_behavior_combo.addItems([
            "Pause: Last Frame | Stop/End: Test Card",
            "Always Broadcast Test Card",
            "Always Broadcast Last Frame"
        ])
        self.pause_behavior_combo.setCurrentIndex(0)
        rv.addWidget(self.pause_behavior_combo)

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

        # Custom FFmpeg & Ports Settings
        cust = QGroupBox("Custom FFmpeg & UDP Port Settings"); cv = QFormLayout(); cv.setSpacing(4)
        
        # CRF
        self.custom_crf_slider = QSlider(Qt.Horizontal)
        self.custom_crf_slider.setRange(10, 51); self.custom_crf_slider.setValue(22)
        self.custom_crf_lbl = QLabel("22")
        crf_h = QHBoxLayout()
        crf_h.addWidget(self.custom_crf_slider)
        crf_h.addWidget(self.custom_crf_lbl)
        self.custom_crf_slider.valueChanged.connect(lambda val: self.custom_crf_lbl.setText(str(val)))
        self.custom_crf_slider.valueChanged.connect(lambda _: self._restart_pipeline())
        cv.addRow("Video CRF:", crf_h)

        # Preset
        self.custom_preset_combo = QComboBox()
        self.custom_preset_combo.addItems([
            "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"
        ])
        self.custom_preset_combo.setCurrentText("medium")
        self.custom_preset_combo.currentIndexChanged.connect(lambda _: self._restart_pipeline())
        cv.addRow("Encoder Preset:", self.custom_preset_combo)

        # TX Port
        self.tx_port_input = QLineEdit("5005")
        self.tx_port_input.setValidator(QtGui.QIntValidator(1024, 65535))
        self.tx_port_input.editingFinished.connect(self._restart_pipeline)
        cv.addRow("TX Port (UDP):", self.tx_port_input)

        # RX Port
        self.rx_port_input = QLineEdit("5002")
        self.rx_port_input.setValidator(QtGui.QIntValidator(1024, 65535))
        self.rx_port_input.editingFinished.connect(self._restart_pipeline)
        cv.addRow("RX Port (UDP):", self.rx_port_input)

        # Custom Encoder Args
        self.custom_enc_args_input = QLineEdit("")
        self.custom_enc_args_input.setPlaceholderText("e.g. -tune animation -profile:v main")
        self.custom_enc_args_input.editingFinished.connect(self._restart_pipeline)
        cv.addRow("Extra Encoder Args:", self.custom_enc_args_input)

        # Custom Decoder Args
        self.custom_dec_args_input = QLineEdit("")
        self.custom_dec_args_input.setPlaceholderText("e.g. -threads 4")
        self.custom_dec_args_input.editingFinished.connect(self._restart_pipeline)
        cv.addRow("Extra Decoder Args:", self.custom_dec_args_input)

        cust.setLayout(cv); v.addWidget(cust)

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
        self.orig_screen.setMinimumSize(320, 180)
        self.orig_screen.setStyleSheet("background:#000; border:1px solid #30363d;")
        self.orig_screen.setAlignment(Qt.AlignCenter)
        self.orig_screen.double_clicked.connect(self.on_tx_double_clicked)
        col1.addWidget(self.orig_screen, 1) # Give it stretch
        
        # Preview Audio controls
        prev_aud_row = QHBoxLayout()
        self.chk_enable_tx_preview = QCheckBox("Enable TX Preview")
        self.chk_enable_tx_preview.setChecked(True)
        self.chk_enable_tx_preview.stateChanged.connect(self.on_tx_preview_enable_changed)
        prev_aud_row.addWidget(self.chk_enable_tx_preview)

        self.btn_mute_preview = QCheckBox("Play Preview Audio (direct)")
        self.btn_mute_preview.setChecked(False) # default to False (muted)
        self.btn_mute_preview.stateChanged.connect(self.on_preview_mute_changed)
        prev_aud_row.addWidget(self.btn_mute_preview)
        
        # Preview stats/info
        self.preview_stats_lbl = QLabel("Res: 720x480 | Codec: Synth")
        self.preview_stats_lbl.setStyleSheet("color: #8b949e; font-size: 10px; font-family: monospace;")
        prev_aud_row.addWidget(self.preview_stats_lbl)
        col1.addLayout(prev_aud_row)
        
        h.addWidget(col1_widget, 1)
        
        # Screen 2: RX (Received DTV Output)
        col2_widget = QWidget()
        col2 = QVBoxLayout(col2_widget)
        col2.setContentsMargins(0, 0, 0, 0)
        lbl2 = QLabel("<b>Received DTV Output (RX)</b>"); lbl2.setAlignment(Qt.AlignCenter)
        col2.addWidget(lbl2)
        
        self.recv_screen = ClickableLabel()
        self.recv_screen.setMinimumSize(320, 180)
        self.recv_screen.setStyleSheet("background:#000; border:1px solid #30363d;")
        self.recv_screen.setAlignment(Qt.AlignCenter)
        self.recv_screen.double_clicked.connect(self.on_rx_double_clicked)
        col2.addWidget(self.recv_screen, 1) # Give it stretch
        
        # RX Audio controls
        rx_aud_row = QHBoxLayout()
        self.chk_enable_rx_preview = QCheckBox("Enable RX Preview")
        self.chk_enable_rx_preview.setChecked(True)
        self.chk_enable_rx_preview.stateChanged.connect(self.on_rx_preview_enable_changed)
        rx_aud_row.addWidget(self.chk_enable_rx_preview)

        self.btn_mute_rx = QCheckBox("Play RX Audio")
        self.btn_mute_rx.setChecked(True) # default to True (unmuted)
        self.btn_mute_rx.stateChanged.connect(self.on_rx_mute_changed)
        rx_aud_row.addWidget(self.btn_mute_rx)
        
        # RX stats/info
        self.rx_stats_lbl = QLabel("Res: --x-- | Deint: --")
        self.rx_stats_lbl.setStyleSheet("color: #8b949e; font-size: 10px; font-family: monospace;")
        rx_aud_row.addWidget(self.rx_stats_lbl)
        col2.addLayout(rx_aud_row)
        
        h.addWidget(col2_widget, 1)
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

    def on_service_type_changed(self):
        service_idx = self.service_type_combo.currentIndex()
        
        # Block signals to prevent intermediate callback loops
        self.std_combo.blockSignals(True)
        self.freq_band_combo.blockSignals(True)
        
        self.std_combo.clear()
        self.freq_band_combo.clear()
        
        if service_idx == 0:  # Antenna TV
            self.std_combo.addItems([
                "ATSC (8VSB)",
                "DVB-T (OFDM)",
                "DVB-T2 (256QAM)"
            ])
            self.freq_band_combo.addItems([
                "VHF (174 MHz)",
                "UHF (600 MHz)"
            ])
        elif service_idx == 1:  # Cable TV
            self.std_combo.addItems([
                "J.83B (64QAM)",
                "J.83B (256QAM)"
            ])
            self.freq_band_combo.addItems([
                "Sub-split (30 MHz)",
                "Mid-band (150 MHz)",
                "Super-band (300 MHz)",
                "Hyper-band (450 MHz)",
                "Ultra-band (600 MHz)",
                "Extended-band (850 MHz)",
                "Gigabit-band (1000 MHz)"
            ])
        elif service_idx == 2:  # Satellite TV
            self.std_combo.addItems([
                "DVB-S (QPSK)",
                "DVB-S2 (8PSK)"
            ])
            self.freq_band_combo.addItems([
                "L-Band (1.5 GHz)",
                "S-Band (2.5 GHz)",
                "C-Band (4.0 GHz)",
                "X-Band (10.0 GHz)",
                "Ku-Band (12.0 GHz)",
                "K-Band (20.0 GHz)",
                "Ka-Band (30.0 GHz)"
            ])
            
        self.std_combo.blockSignals(False)
        self.freq_band_combo.blockSignals(False)
        
        # Trigger updates
        if self.gr_tb:
            try:
                std_id, _, _ = self.get_active_standard_id_and_details()
                self.gr_tb.set_active_standard(std_id)
            except Exception: pass
            
        self.on_impairment_changed()

    def get_active_standard_id_and_details(self):
        std_text = self.std_combo.currentText()
        if "ATSC" in std_text:
            return 0, 6, 15
        elif "DVB-S2" in std_text:
            return 1, 8, 10
        elif "DVB-S" in std_text:
            return 1, 8, 8
        elif "J.83B (64QAM)" in std_text:
            return 2, 8, 22
        elif "J.83B (256QAM)" in std_text:
            return 2, 8, 28
        elif "DVB-T2" in std_text:
            return 3, 8, 16
        elif "DVB-T (OFDM)" in std_text:
            return 4, 8, 5
        return 0, 6, 15

    def get_active_band_details(self):
        band_text = self.freq_band_combo.currentText()
        if "VHF" in band_text:
            return 174.0, 3.0, 0
        elif "B-Band" in band_text:
            return 450.0, 4.5, 1
        elif "UHF" in band_text:
            return 600.0, 6.0, 2
        elif "Sub-split" in band_text:
            return 30.0, 1.0, 0
        elif "Mid-band" in band_text:
            return 150.0, 2.5, 0
        elif "Super-band" in band_text:
            return 300.0, 4.0, 1
        elif "Hyper-band" in band_text:
            return 450.0, 5.0, 1
        elif "Ultra-band" in band_text:
            return 600.0, 6.0, 2
        elif "Extended-band" in band_text:
            return 850.0, 7.0, 2
        elif "Gigabit-band" in band_text:
            return 1000.0, 8.0, 2
        elif "L-Band" in band_text:
            return 1500.0, 12.0, 3
        elif "S-Band" in band_text:
            return 2500.0, 18.0, 4
        elif "C-Band" in band_text:
            return 4000.0, 24.0, 5
        elif "X-Band" in band_text:
            return 10000.0, 30.0, 6
        elif "Ku-Band" in band_text:
            return 12000.0, 36.0, 7
        elif "K-Band" in band_text:
            return 20000.0, 39.0, 8
        elif "Ka-Band" in band_text:
            return 30000.0, 42.0, 9
        return 600.0, 6.0, 2

    def on_standard_changed(self, idx):
        if self.gr_tb:
            try:
                std_id, _, _ = self.get_active_standard_id_and_details()
                self.gr_tb.set_active_standard(std_id)
            except Exception: pass
        self.on_impairment_changed()

    def on_select_file(self):
        fname, _ = QFileDialog.getOpenFileName(
            self, "Open Media File", USER_HOME,
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
                self.btn_play_pause.setText("Pause")
            self._probe_file_details(fname)
            self._restart_pipeline(restart_decoder=False)

    def on_resolution_changed(self, idx):
        if idx < 0 or idx >= len(DTV_RESOLUTIONS):
            return
        _, w, h, il, fps_def = DTV_RESOLUTIONS[idx]
        self.video_width  = w
        self.video_height = h
        self.fps          = fps_def
        
        self.fps_slider.blockSignals(True)
        self.interlace_checkbox.blockSignals(True)
        self.jpg_slider.blockSignals(True)
        
        self.fps_slider.setValue(fps_def)
        if not il:
            self.interlace_checkbox.setChecked(False)
        self.interlaced = self.interlace_checkbox.isChecked()
        
        # Auto-adjust bitrate slider based on resolution for realistic quality
        bitrate_slider_val = [60, 65, 80, 88, 92, 20][idx]
        self.jpg_slider.setValue(bitrate_slider_val)
        self.bitrate_kbps = int(500.0 * (50.0 ** ((bitrate_slider_val - 10) / 90.0)))
        
        self.fps_slider.blockSignals(False)
        self.interlace_checkbox.blockSignals(False)
        self.jpg_slider.blockSignals(False)
        
        self._restart_pipeline()

    def update_bitrate_label(self, val):
        kbps = int(500.0 * (50.0 ** ((val - 10) / 90.0)))
        labels = {0: 'very heavy blocking', 30: 'heavy artifacts',
                  60: 'SD quality', 80: 'HD quality', 95: 'near-lossless'}
        tag = ''
        for thresh, txt in sorted(labels.items(), reverse=True):
            if val >= thresh:
                tag = txt; break
        self.jpg_lbl.setText(f"~{kbps} kbps  ({tag})")

    def on_bitrate_changed_commit(self):
        val = self.jpg_slider.value()
        self.bitrate_kbps = int(500.0 * (50.0 ** ((val - 10) / 90.0)))
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
            self._restart_pipeline(restart_decoder=True)

    def on_hw_accel_changed(self, idx):
        global HW_ENC, HW_DEC_FLAGS, HW_ENC_FLAGS, VAAPI_VF
        if idx == 0:  # Auto-Detect
            probe_hw()
        elif idx == 1:  # Intel QSV
            HW_ENC = 'mpeg2_qsv'
            HW_DEC_FLAGS, HW_ENC_FLAGS, VAAPI_VF = [], [], []
        elif idx == 2:  # NVIDIA
            HW_ENC = 'mpeg2video'
            HW_DEC_FLAGS, HW_ENC_FLAGS, VAAPI_VF = [], [], []
        elif idx == 3:  # AMD AMF
            HW_ENC = 'mpeg2_amf'
            HW_DEC_FLAGS, HW_ENC_FLAGS, VAAPI_VF = [], [], []
        elif idx == 4:  # VAAPI
            dev = '/dev/dri/renderD128'
            HW_ENC = 'mpeg2_vaapi'
            HW_ENC_FLAGS = ['-vaapi_device', dev] if os.path.exists(dev) else []
            VAAPI_VF = ['format=nv12,hwupload,']
            HW_DEC_FLAGS = []
        else:  # Software Only
            HW_ENC = 'mpeg2video'
            HW_DEC_FLAGS, HW_ENC_FLAGS, VAAPI_VF = [], [], []
        
        print(f"[HW] Switched to: {HW_ENC}")
        self._restart_pipeline(restart_decoder=False)

    def on_tx_double_clicked(self):
        self.enter_fullscreen('tx')
        
    def on_rx_double_clicked(self):
        self.enter_fullscreen('rx')
        
    def enter_fullscreen(self, target):
        if self.fullscreen_view:
            try: self.fullscreen_view.close()
            except Exception: pass
            
        title = "Transmitting Source (TX Preview)" if target == 'tx' else "Received DTV Output (RX)"
        
        # Ask for Windowed vs Fullscreen if performance is an issue
        msg = QtWidgets.QMessageBox()
        msg.setWindowTitle("Fullscreen Mode")
        msg.setText("Choose display mode:")
        btn_fs = msg.addButton("Exclusive Fullscreen", QtWidgets.QMessageBox.ActionRole)
        btn_win = msg.addButton("Windowed Mode (Better Performance)", QtWidgets.QMessageBox.ActionRole)
        msg.addButton(QtWidgets.QMessageBox.Cancel)
        msg.exec_()
        
        if msg.clickedButton() == btn_fs:
            windowed = False
        elif msg.clickedButton() == btn_win:
            windowed = True
        else:
            return

        self.fullscreen_view = FullscreenWindow(title, self, windowed=windowed)
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
        
        def probe_worker():
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

            # Update UI components safely
            self.probe_complete.emit()

        threading.Thread(target=probe_worker, daemon=True).start()

    def _on_probe_complete(self):
        if hasattr(self, 'timeline_slider'):
            if self.video_duration > 0:
                self.timeline_slider.setRange(0, int(self.video_duration))
                self.timeline_slider.setEnabled(True)
            else:
                self.timeline_slider.setRange(0, 100)
                self.timeline_slider.setEnabled(False)
                self.time_lbl.setText("00:00 / 00:00")

        if hasattr(self, 'lbl_player_info'):
            dur_min, dur_sec = divmod(int(self.video_duration), 60)
            dur_str = f"{dur_min:02d}:{dur_sec:02d}"
            info_text = (
                f"Video: {self.file_v_res} @ {self.file_v_fps}fps ({self.file_v_codec}) | "
                f"Audio: {self.file_a_codec} ({self.file_a_rate}) | Dur: {dur_str}"
            )
            self.lbl_player_info.setText(info_text)

    def _build_player_controls(self):
        player_box = QGroupBox("Media Player Control Room")
        pv = QVBoxLayout(); pv.setSpacing(4)
        
        # File info label
        self.lbl_player_info = QLabel("No active video file (playing synthetic pattern)")
        self.lbl_player_info.setStyleSheet("color:#8b949e; font-size:11px; font-style:italic;")
        pv.addWidget(self.lbl_player_info)

        # Timeline row
        time_row = QHBoxLayout()
        self.btn_play_pause = QPushButton("Pause")
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
        btn_prev = QPushButton("PREV")
        btn_prev.clicked.connect(self.on_playlist_prev)
        btn_next = QPushButton("NEXT")
        btn_next.clicked.connect(self.on_playlist_next)
        p_nav_row.addWidget(btn_prev)
        p_nav_row.addWidget(btn_next)
        p_btn_col.addLayout(p_nav_row)

        self.chk_auto_advance = QCheckBox("Auto-Advance")
        self.chk_auto_advance.setChecked(True)
        p_btn_col.addWidget(self.chk_auto_advance)
        
        self.chk_loop = QCheckBox("Loop Video")
        self.chk_loop.setChecked(True)
        p_btn_col.addWidget(self.chk_loop)
        
        playlist_row.addLayout(p_btn_col)
        pv.addLayout(playlist_row)
        
        player_box.setLayout(pv)
        return player_box

    def on_play_pause_clicked(self):
        if not self.video_file: return
        self.playback_paused = not self.playback_paused
        
        if self.playback_paused:
            self.btn_play_pause.setText("Play")
            self.pause_start_time = time.time()
            if getattr(self, 'latest_preview', None):
                self.paused_frame_rgb = self.latest_preview
            self._restart_pipeline()
        else:
            self.btn_play_pause.setText("Pause")
            if hasattr(self, 'pause_start_time'):
                self.play_start_time += (time.time() - self.pause_start_time)
            
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
        self.btn_play_pause.setText("Pause")
        self._restart_pipeline()

    def on_playlist_add(self):
        fname, _ = QFileDialog.getOpenFileName(
            self, "Add to Playlist", USER_HOME,
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
            was_playing = (self.playlist[row] == self.video_file)
            self.playlist.pop(row)
            self.playlist_widget.takeItem(row)
            
            if not self.playlist:
                self.video_file = ''
                self.file_lbl.setText("Source: SMPTE test pattern (synthetic)")
                if hasattr(self, 'lbl_player_info'):
                    self.lbl_player_info.setText("No active video file (playing synthetic pattern)")
                self.timeline_slider.setRange(0, 100)
                self.timeline_slider.setEnabled(False)
                self.time_lbl.setText("00:00 / 00:00")
                self.seek_seconds = 0.0
                self.play_start_offset = 0.0
                self.play_start_time = time.time()
                self._restart_pipeline()
            elif was_playing:
                new_idx = min(row, len(self.playlist) - 1)
                self.play_playlist_index(new_idx)

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
            self.btn_play_pause.setText("Pause")
            
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
        widgets = [self.service_type_combo, self.std_combo, self.freq_band_combo, self.weather_combo,
                   self.tx_power_slider, self.range_slider, self.lna_checkbox,
                   self.prop_combo, self.theme_combo, self.box_model_combo, self.res_combo, self.interlace_checkbox,
                   self.audio_codec_combo, self.noise_slider, self.freq_slider,
                   self.time_slider, self.fade_slider, self.fps_slider, self.jpg_slider,
                   self.custom_crf_slider, self.custom_preset_combo,
                   self.tx_port_input, self.rx_port_input,
                   self.custom_enc_args_input, self.custom_dec_args_input,
                   self.noise_offset_slider, self.lna_gain_slider, self.custom_pattern_combo]
        for w in widgets: w.blockSignals(True)

        cfg = {
            1: dict(service_type=0, std=0, band=1, wx=0, pwr=40, dist=10,  lna=True,  prop=0, theme=0, box_model=0, res=0, il=True,  acodec=1, noise=0, freq=0, time=1000, fade=0, noise_offset=0, lna_gain=12, custom_pattern=1),
            2: dict(service_type=0, std=1, band=1, wx=0, pwr=43, dist=25,  lna=True,  prop=0, theme=0, box_model=0, res=2, il=False, acodec=0, noise=0, freq=0, time=1000, fade=0, noise_offset=0, lna_gain=12, custom_pattern=2),
            3: dict(service_type=2, std=1, band=4, wx=0, pwr=60, dist=38000, lna=True, prop=0, theme=1, box_model=1, res=3, il=True,  acodec=2, noise=0, freq=0, time=1000, fade=0, noise_offset=0, lna_gain=12, custom_pattern=3),
            4: dict(service_type=1, std=0, band=4, wx=0, pwr=38, dist=2,   lna=False, prop=0, theme=2, box_model=2, res=4, il=False, acodec=1, noise=0, freq=0, time=1000, fade=0, noise_offset=0, lna_gain=12, custom_pattern=4),
        }.get(idx, {})

        if cfg:
            if 'service_type' in cfg:
                self.service_type_combo.setCurrentIndex(cfg['service_type'])
                self.service_type_combo.blockSignals(False)
                self.on_service_type_changed()
                self.service_type_combo.blockSignals(True)
                self.std_combo.blockSignals(True)
                self.freq_band_combo.blockSignals(True)
            self.std_combo.setCurrentIndex(cfg['std'])
            self.freq_band_combo.setCurrentIndex(cfg['band'])
            self.weather_combo.setCurrentIndex(cfg['wx'])
            self.tx_power_slider.setValue(cfg['pwr'])
            self.range_slider.setValue(cfg['dist'])
            self.lna_checkbox.setChecked(cfg['lna'])
            self.prop_combo.setCurrentIndex(cfg['prop'])
            self.theme_combo.setCurrentIndex(cfg['theme'])
            if 'box_model' in cfg: self.box_model_combo.setCurrentIndex(cfg['box_model'])
            self.res_combo.setCurrentIndex(cfg['res'])
            self.interlace_checkbox.setChecked(cfg['il'])
            self.audio_codec_combo.setCurrentIndex(cfg['acodec'])
            self.audio_codec = ['mp2', 'ac3', 'aac'][cfg['acodec']]
            self.noise_slider.setValue(cfg.get('noise', 0))
            if 'noise_offset' in cfg: self.noise_offset_slider.setValue(cfg['noise_offset'])
            if 'lna_gain' in cfg: self.lna_gain_slider.setValue(cfg['lna_gain'])
            if 'custom_pattern' in cfg: self.custom_pattern_combo.setCurrentIndex(cfg['custom_pattern'])
            self.freq_slider.setValue(cfg.get('freq', 0))
            self.time_slider.setValue(cfg.get('time', 1000))
            self.fade_slider.setValue(cfg.get('fade', 0))

        for w in widgets: w.blockSignals(False)

        res_idx = self.res_combo.currentIndex()
        bitrate_slider_val = [60, 65, 80, 88, 92, 20][res_idx]
        self.jpg_slider.setValue(bitrate_slider_val)
        self.bitrate_kbps = int(500.0 * (50.0 ** ((bitrate_slider_val - 10) / 90.0)))

        _, w, h, _, fps_def = DTV_RESOLUTIONS[res_idx]
        self.video_width = w; self.video_height = h; self.fps = fps_def
        self.interlaced = self.interlace_checkbox.isChecked()
        self.fps_slider.blockSignals(True)
        self.fps_slider.setValue(fps_def)
        self.fps_slider.blockSignals(False)
        if self.gr_tb:
            try: self.gr_tb.set_active_standard(self.std_combo.currentIndex())
            except Exception: pass
        
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(0)
        self.preset_combo.blockSignals(False)
        
        self.on_impairment_changed()
        self._restart_pipeline()

    def on_save_preset(self):
        fname, _ = QFileDialog.getSaveFileName(
            self, "Save Preset", os.path.expanduser('~'), "DTV Preset (*.json)"
        )
        if not fname: return
        if not fname.lower().endswith('.json'):
            fname += '.json'
        
        cfg = {
            'service_type': self.service_type_combo.currentIndex(),
            'std': self.std_combo.currentIndex(),
            'band': self.freq_band_combo.currentIndex(),
            'wx': self.weather_combo.currentIndex(),
            'pwr': self.tx_power_slider.value(),
            'dist': self.range_slider.value(),
            'lna': self.lna_checkbox.isChecked(),
            'prop': self.prop_combo.currentIndex(),
            'theme': self.theme_combo.currentIndex(),
            'box_model': self.box_model_combo.currentIndex(),
            'res': self.res_combo.currentIndex(),
            'il': self.interlace_checkbox.isChecked(),
            'acodec': self.audio_codec_combo.currentIndex(),
            'noise': self.noise_slider.value(),
            'freq': self.freq_slider.value(),
            'time': self.time_slider.value(),
            'fade': self.fade_slider.value(),
            'fps': self.fps_slider.value(),
            'bitrate_val': self.jpg_slider.value(),
            'custom_bg': self.custom_bg_edit.text(),
            'custom_border': self.custom_border_edit.text(),
            'custom_title': self.custom_title_edit.text(),
            'custom_msg': self.custom_msg_edit.text(),
            'custom_crf': self.custom_crf_slider.value(),
            'custom_preset': self.custom_preset_combo.currentText(),
            'tx_port': self.tx_port_input.text(),
            'rx_port': self.rx_port_input.text(),
            'custom_enc_args': self.custom_enc_args_input.text(),
            'custom_dec_args': self.custom_dec_args_input.text(),
            'noise_offset': self.noise_offset_slider.value(),
            'lna_gain': self.lna_gain_slider.value(),
            'custom_pattern': self.custom_pattern_combo.currentIndex(),
            'custom_image': self.custom_image_edit.text(),
            'custom_font_family': self.custom_font_family_edit.text(),
            'custom_font_size': self.custom_font_size_edit.text()
        }
        
        try:
            with open(fname, 'w') as f:
                json.dump(cfg, f, indent=4)
            print(f"[PRESET] Saved preset to {fname}")
        except Exception as e:
            print(f"[PRESET] Failed to save preset: {e}")

    def on_load_preset(self):
        fname, _ = QFileDialog.getOpenFileName(
            self, "Load Preset", os.path.expanduser('~'), "DTV Preset (*.json)"
        )
        if not fname: return
        
        try:
            with open(fname, 'r') as f:
                cfg = json.load(f)
            
            widgets = [self.service_type_combo, self.std_combo, self.freq_band_combo, self.weather_combo,
                       self.tx_power_slider, self.range_slider, self.lna_checkbox,
                       self.prop_combo, self.theme_combo, self.box_model_combo, self.res_combo, self.interlace_checkbox,
                       self.audio_codec_combo, self.noise_slider, self.freq_slider,
                       self.time_slider, self.fade_slider, self.fps_slider, self.jpg_slider,
                       self.custom_crf_slider, self.custom_preset_combo,
                       self.tx_port_input, self.rx_port_input,
                       self.custom_enc_args_input, self.custom_dec_args_input,
                       self.noise_offset_slider, self.lna_gain_slider, self.custom_pattern_combo]
            for w in widgets: w.blockSignals(True)
            
            if 'service_type' in cfg:
                self.service_type_combo.setCurrentIndex(cfg['service_type'])
                self.service_type_combo.blockSignals(False)
                self.on_service_type_changed()
                self.service_type_combo.blockSignals(True)
                self.std_combo.blockSignals(True)
                self.freq_band_combo.blockSignals(True)
            
            if 'std' in cfg: self.std_combo.setCurrentIndex(cfg['std'])
            if 'band' in cfg: self.freq_band_combo.setCurrentIndex(cfg['band'])
            if 'wx' in cfg: self.weather_combo.setCurrentIndex(cfg['wx'])
            if 'pwr' in cfg: self.tx_power_slider.setValue(cfg['pwr'])
            if 'dist' in cfg: self.range_slider.setValue(cfg['dist'])
            if 'lna' in cfg: self.lna_checkbox.setChecked(cfg['lna'])
            if 'prop' in cfg: self.prop_combo.setCurrentIndex(cfg['prop'])
            if 'theme' in cfg: self.theme_combo.setCurrentIndex(cfg['theme'])
            if 'box_model' in cfg: self.box_model_combo.setCurrentIndex(cfg['box_model'])
            if 'res' in cfg: self.res_combo.setCurrentIndex(cfg['res'])
            if 'il' in cfg: self.interlace_checkbox.setChecked(cfg['il'])
            if 'acodec' in cfg:
                self.audio_codec_combo.setCurrentIndex(cfg['acodec'])
                self.audio_codec = ['mp2', 'ac3', 'aac'][cfg['acodec']]
            if 'noise' in cfg: self.noise_slider.setValue(cfg['noise'])
            if 'freq' in cfg: self.freq_slider.setValue(cfg['freq'])
            if 'time' in cfg: self.time_slider.setValue(cfg['time'])
            if 'fade' in cfg: self.fade_slider.setValue(cfg['fade'])
            if 'fps' in cfg: self.fps_slider.setValue(cfg['fps'])
            if 'bitrate_val' in cfg: self.jpg_slider.setValue(cfg['bitrate_val'])
            if 'custom_bg' in cfg: self.custom_bg_edit.setText(cfg['custom_bg'])
            if 'custom_border' in cfg: self.custom_border_edit.setText(cfg['custom_border'])
            if 'custom_title' in cfg: self.custom_title_edit.setText(cfg['custom_title'])
            if 'custom_msg' in cfg: self.custom_msg_edit.setText(cfg['custom_msg'])
            if 'custom_crf' in cfg: self.custom_crf_slider.setValue(cfg['custom_crf'])
            if 'custom_preset' in cfg: self.custom_preset_combo.setCurrentText(cfg['custom_preset'])
            if 'tx_port' in cfg: self.tx_port_input.setText(cfg['tx_port'])
            if 'rx_port' in cfg: self.rx_port_input.setText(cfg['rx_port'])
            if 'custom_enc_args' in cfg: self.custom_enc_args_input.setText(cfg['custom_enc_args'])
            if 'noise_offset' in cfg: self.noise_offset_slider.setValue(cfg['noise_offset'])
            if 'lna_gain' in cfg: self.lna_gain_slider.setValue(cfg['lna_gain'])
            if 'custom_pattern' in cfg: self.custom_pattern_combo.setCurrentIndex(cfg['custom_pattern'])
            if 'custom_image' in cfg: self.custom_image_edit.setText(cfg['custom_image'])
            if 'custom_font_family' in cfg: self.custom_font_family_edit.setText(cfg['custom_font_family'])
            if 'custom_font_size' in cfg: self.custom_font_size_edit.setText(cfg['custom_font_size'])
            if 'custom_dec_args' in cfg: self.custom_dec_args_input.setText(cfg['custom_dec_args'])
            
            for w in widgets: w.blockSignals(False)
            
            _, w, h, _, _ = DTV_RESOLUTIONS[self.res_combo.currentIndex()]
            self.video_width = w
            self.video_height = h
            self.fps = self.fps_slider.value()
            self.interlaced = self.interlace_checkbox.isChecked()
            
            val = self.jpg_slider.value()
            self.bitrate_kbps = int(500.0 * (50.0 ** ((val - 10) / 90.0)))
            
            if self.gr_tb:
                try: self.gr_tb.set_active_standard(self.std_combo.currentIndex())
                except Exception: pass
            
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(0)
            self.preset_combo.blockSignals(False)
            
            self.on_impairment_changed()
            self._restart_pipeline()
            print(f"[PRESET] Loaded preset from {fname}")
        except Exception as e:
            print(f"[PRESET] Failed to load preset: {e}")

    # ─────────────────────────────────────────────────────────────
    #  LINK BUDGET / IMPAIRMENT CALCULATION
    # ─────────────────────────────────────────────────────────────
    def on_impairment_changed(self):
        # Update text labels
        if hasattr(self, 'noise_offset_lbl'):
            self.noise_offset_lbl.setText(f"{self.noise_offset_slider.value():+d} dB")
        if hasattr(self, 'lna_gain_lbl'):
            self.lna_gain_lbl.setText(f"{self.lna_gain_slider.value()} dB")

        dist   = self.range_slider.value()
        freq, ant_gain, atten_idx = self.get_active_band_details()
        wi     = self.weather_combo.currentIndex()
        atten  = [
            [0,     0,     0,     0,     0,     0,     0,     0,     0,     0    ],
            [0.001, 0.003, 0.005, 0.02,  0.04,  0.08,  0.12,  0.15,  0.22,  0.3  ],
            [0.002, 0.006, 0.01,  0.05,  0.10,  0.20,  0.35,  0.5,   0.85,  1.2  ],
            [0.005, 0.015, 0.03,  0.12,  0.25,  0.50,  2.00,  3.5,   5.80,  8.0  ],
            [0.01,  0.05,  0.10,  0.35,  0.70,  1.50,  5.00,  8.0,   14.00, 20.0 ],
        ][wi][min(atten_idx, 9)]

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

        # Band-dependent antenna gains
        # Represents realistic home TV antennas & high-gain satellite dishes
        lna_val = self.lna_gain_slider.value() if hasattr(self, 'lna_gain_slider') else 12.0
        rx_gain = ant_gain + (lna_val if self.lna_checkbox.isChecked() else 0.0)
        rx_pwr  = tx_dbm - tloss + rx_gain + pgain

        # Realistic noise floor: thermal + NF at standard bandwidth
        _, bw_mhz, thresh = self.get_active_standard_id_and_details()
        noise_offset = self.noise_offset_slider.value() if hasattr(self, 'noise_offset_slider') else 0.0
        n_floor = -174.0 + 10*math.log10(bw_mhz * 1e6) + 7.0 + noise_offset

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
        if self.video_file and not self.playback_paused:
            if self.chk_enable_tx_preview.isChecked() and self.latest_preview is not None:
                qimg = QImage(self.latest_preview, self.preview_w, self.preview_h, self.preview_w * 3, QImage.Format_RGB888)
                pix = QPixmap.fromImage(qimg)
                scaled_pix = pix.scaled(self.orig_screen.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.orig_screen.setPixmap(scaled_pix)
                if self.fullscreen_target == 'tx' and self.fullscreen_view:
                    fs_pix = pix.scaled(self.fullscreen_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self.fullscreen_view.setPixmap(fs_pix)
            # Encoder reads file independently — no push needed
        else:
            # Synthetic, stopped, or paused mode
            _, w, h, _, _ = DTV_RESOLUTIONS[self.res_combo.currentIndex()]
            synth_w = 848 if w / h > 1.4 else 720

            # Determine what to display/broadcast when paused/stopped
            behavior = self.pause_behavior_combo.currentIndex() if hasattr(self, 'pause_behavior_combo') else 0
            
            frame = None
            if self.playback_paused:
                if behavior == 1: # Always Test Card
                    frame = self._make_test_pattern(synth_w, 480)
                else: # Last Frame
                    if getattr(self, 'paused_frame_rgb', None):
                        try:
                            img = Image.frombytes('RGB', (self.preview_w, self.preview_h), self.paused_frame_rgb)
                            img = img.resize((synth_w, 480), Image.Resampling.LANCZOS)
                            frame = img.tobytes()
                        except Exception:
                            pass
            
            if frame is None:
                frame = self._make_test_pattern(synth_w, 480)

            if self.interlace_checkbox.isChecked():
                tx_frame = self._field_split(frame, synth_w, 480)
            else:
                tx_frame = frame

            if self.chk_enable_tx_preview.isChecked():
                qimg = QImage(frame, synth_w, 480, synth_w * 3, QImage.Format_RGB888)
                pix = QPixmap.fromImage(qimg)
                scaled_pix = pix.scaled(self.orig_screen.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.orig_screen.setPixmap(scaled_pix)

                if self.fullscreen_target == 'tx' and self.fullscreen_view:
                    fs_pix = pix.scaled(self.fullscreen_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self.fullscreen_view.setPixmap(fs_pix)

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
        # Check selected pattern from tuner / customization tab
        pattern_idx = self.custom_pattern_combo.currentIndex() if hasattr(self, 'custom_pattern_combo') else 1
        img_path = self.custom_image_edit.text() if hasattr(self, 'custom_image_edit') else ""
        bg_hex = self.custom_bg_edit.text() if hasattr(self, 'custom_bg_edit') else "#0f0f12"
        
        def hex_to_rgb(hex_str):
            hex_str = hex_str.lstrip('#')
            try: return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))
            except Exception: return (15, 15, 18)
        bg_color = hex_to_rgb(bg_hex)

        img = None
        if pattern_idx == 0: # Solid Background Color
            img = Image.new('RGB', (w, h), bg_color)
        elif pattern_idx == 1: # SMPTE Bars
            img = Image.new('RGB', (w, h))
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
        elif pattern_idx == 2: # Color Bars (Rainbow)
            img = Image.new('RGB', (w, h))
            draw = ImageDraw.Draw(img)
            colors = [
                (255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
                (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)
            ]
            bar_w = w // len(colors)
            for i, col in enumerate(colors):
                draw.rectangle([i*bar_w, 0, (i+1)*bar_w if i < len(colors)-1 else w, h], fill=col)
        elif pattern_idx == 3: # Grid / Crosshatch
            img = Image.new('RGB', (w, h), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            grid_size = 40
            for x in range(0, w, grid_size):
                draw.line([x, 0, x, h], fill=(255, 255, 255), width=1)
            for y in range(0, h, grid_size):
                draw.line([0, y, w, y], fill=(255, 255, 255), width=1)
        elif pattern_idx == 4: # White Noise (Animated)
            arr = np.random.randint(0, 256, (h, w, 3), dtype=np.uint8)
            img = Image.fromarray(arr, 'RGB')
        elif pattern_idx == 5: # Custom Image File
            if img_path and os.path.exists(img_path):
                cached_img = getattr(self, '_cached_tx_bg_img', None)
                cached_path = getattr(self, '_cached_tx_bg_path', None)
                cached_w = getattr(self, '_cached_tx_bg_w', None)
                cached_h = getattr(self, '_cached_tx_bg_h', None)
                if cached_img and cached_path == img_path and cached_w == w and cached_h == h:
                    img = cached_img
                else:
                    try:
                        img_loaded = Image.open(img_path).convert('RGB')
                        img = img_loaded.resize((w, h), Image.Resampling.LANCZOS)
                        self._cached_tx_bg_img = img
                        self._cached_tx_bg_path = img_path
                        self._cached_tx_bg_w = w
                        self._cached_tx_bg_h = h
                    except Exception as e:
                        print(f"[THEME] Failed to load custom image: {e}")
            if img is None:
                img = Image.new('RGB', (w, h), bg_color)

        draw = ImageDraw.Draw(img)
        bar_h = int(h * 0.75) if pattern_idx == 1 else h

        # Draw bouncing ball only on SMPTE or Grid patterns for visual test
        if pattern_idx in (1, 3):
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
        try: font = ImageFont.load_default()
        except Exception: font = None
        
        # Draw background panel for text readability
        draw.rectangle([self.text_x - 4, bar_h - 22 if pattern_idx == 1 else h - 22, self.text_x + len(lbl)*8 + 4, bar_h - 2 if pattern_idx == 1 else h - 2], fill=(0,0,0))
        draw.text((self.text_x, bar_h - 20 if pattern_idx == 1 else h - 20), lbl, fill=(255, 255, 0), font=font)

        return img.tobytes()

    # ─────────────────────────────────────────────────────────────
    #  RX FRAME / AUDIO HANDLERS
    # ─────────────────────────────────────────────────────────────
    def on_mpeg_frame(self, raw_rgb: bytes):
        """
        Received raw RGB24 frame from MPEG-2 decoder.
        """
        self.last_frame_recv = time.time()
        w, h = 960, 540

        if len(raw_rgb) != w * h * 3:
            return

        # Use bytearray to guarantee a copy and prevent "deep fried" artifacts
        frame_data = bytearray(raw_rgb)
        
        if self.recording:
            img = Image.frombytes('RGB', (w, h), bytes(frame_data))
            self.current_rx_frame = img

        if self.chk_enable_rx_preview.isChecked():
            qimg   = QImage(frame_data, w, h, w * 3, QImage.Format_RGB888).copy()
            pixmap = QPixmap.fromImage(qimg)
            
            scaled_pix = pixmap.scaled(self.recv_screen.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.recv_screen.setPixmap(scaled_pix)
            
            if self.fullscreen_target == 'rx' and self.fullscreen_view:
                fs_pix = pixmap.scaled(self.fullscreen_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.fullscreen_view.setPixmap(fs_pix)

    def on_mpeg_audio(self, pcm: bytes):
        pass

    # ─────────────────────────────────────────────────────────────
    #  METRICS + RX DISPLAY UPDATERS
    # ─────────────────────────────────────────────────────────────
    def update_metrics(self):
        self.on_impairment_changed()
        lock = self._lock_pct
        is_dark = (self.current_theme == 'dark') if hasattr(self, 'current_theme') else True
        bg_nosig = "#1c1e22" if is_dark else "#fcf0f0"
        bg_weak = "#1c1e22" if is_dark else "#fffbeb"
        bg_live = "#0d1117" if is_dark else "#f6ffed"

        if lock == 0:
            self.banner_lbl.setText("NO SIGNAL\n[ SEARCHING FOR CHANNELS ]")
            self.banner_lbl.setStyleSheet(
                f"background:{bg_nosig};border:2px solid #da3633;"
                f"border-radius:5px;padding:6px;color:#da3633;")
        elif lock < 40:
            garbled = ''.join(
                random.choice('#?!*@%') if c != ' ' and random.random() < 0.4 else c
                for c in self.channel_name)
            self.banner_lbl.setText(f"WEAK  CH {self.channel_number}\n[{garbled}]")
            self.banner_lbl.setStyleSheet(
                f"background:{bg_weak};border:2px solid #e3b341;"
                f"border-radius:5px;padding:6px;color:#e3b341;")
        else:
            self.banner_lbl.setText(
                f"LIVE  CH {self.channel_number}\n[{self.channel_name}]")
            self.banner_lbl.setStyleSheet(
                f"background:{bg_live};border:2px solid #238636;"
                f"border-radius:5px;padding:6px;color:#3fb950;")

        now = time.time()
        dt = now - getattr(self, 'last_metrics_time', now - 0.4)
        if dt <= 0:
            dt = 0.4
        self.last_metrics_time = now
        tx = int(self._tx_pkt_count / dt)
        rx = int(self._rx_pkt_count / dt)
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
            
            if curr_pos >= self.video_duration:
                if hasattr(self, 'chk_auto_advance') and self.chk_auto_advance.isChecked() and self.playlist_widget.count() > 1:
                    QtCore.QTimer.singleShot(0, self.on_playlist_next)
                elif hasattr(self, 'chk_loop') and self.chk_loop.isChecked():
                    self.play_start_time = time.time()
                    self.play_start_offset = 0.0
                    curr_pos = 0.0
                else:
                    self.playback_paused = True
                    self.btn_play_pause.setText("Play")
                    curr_pos = self.video_duration
                    # Switch to synthetic/testcard mode
                    self.video_file = ''
                    self.file_lbl.setText("Source: SMPTE test pattern (synthetic)")
                    if hasattr(self, 'lbl_player_info'):
                        self.lbl_player_info.setText("No active video file (playing synthetic pattern)")
                    self.timeline_slider.setRange(0, 100)
                    self.timeline_slider.setEnabled(False)
                    self.time_lbl.setText("00:00 / 00:00")
                    self.seek_seconds = 0.0
                    self.play_start_offset = 0.0
                    self.play_start_time = time.time()
                    self._restart_pipeline()

            if hasattr(self, 'timeline_slider') and not self.timeline_slider.isSliderDown():
                self.timeline_slider.setValue(int(curr_pos))
                
            if hasattr(self, 'time_lbl'):
                cur_min, cur_sec = divmod(int(curr_pos), 60)
                dur_min, dur_sec = divmod(int(self.video_duration), 60)
                self.time_lbl.setText(f"{cur_min:02d}:{cur_sec:02d} / {dur_min:02d}:{dur_sec:02d}")

        self.on_impairment_changed()
        lock = self._lock_pct
        now  = time.time()

        # Signal restoration transition check to clear datamosh/deepfried corruption
        prev_lock = getattr(self, '_last_lock_for_recovery', 100)
        self._last_lock_for_recovery = lock
        if prev_lock < 30 and lock >= 50:
            if getattr(self, 'mpeg_decoder', None):
                print("[RX] Signal restored. Restarting decoder to clear corruption.")
                self.decoder_start_time = now
                self.last_frame_recv = 0.0
                def recover_restored():
                    if self.mpeg_decoder:
                        try: self.mpeg_decoder._respawn('video_proc')
                        except Exception: pass
                        try: self.mpeg_decoder._respawn('audio_proc')
                        except Exception: pass
                threading.Thread(target=recover_restored, daemon=True).start()

        if lock == 0:
            if self.chk_enable_rx_preview.isChecked():
                self._draw_no_signal()
        else:
            is_hung = False
            if self.last_frame_recv == 0.0:
                # Give FFmpeg at least 12 seconds to start up and analyze headers before assuming it's hung
                if now - getattr(self, 'decoder_start_time', now) > 12.0:
                    if self.chk_enable_rx_preview.isChecked():
                        self._draw_no_signal()
                    is_hung = True
            else:
                # Once running, if no frame in 5.0s, assume crash/hang
                if now - self.last_frame_recv > 5.0:
                    if self.chk_enable_rx_preview.isChecked():
                        self._draw_no_signal()
                    is_hung = True

            # Active Watchdog: If we have a signal lock but FFmpeg is hung (heavy datamosh crash),
            # forcefully kill and respawn the decoder processes.
            if is_hung and getattr(self, 'mpeg_decoder', None):
                if now - getattr(self, 'last_recovery_time', 0.0) > 5.0:
                    print("[RX] Stream hung detected (FFmpeg stalled). Forcing decoder recovery...")
                    self.last_recovery_time = now
                    self.decoder_start_time = now  # Reset startup timer so it doesn't instantly trigger again
                    
                    def recover():
                        if self.mpeg_decoder:
                            try: self.mpeg_decoder._respawn('video_proc')
                            except Exception: pass
                            try: self.mpeg_decoder._respawn('audio_proc')
                            except Exception: pass
                            
                    threading.Thread(target=recover, daemon=True).start()

    def _get_font(self, name="DejaVuSans-Bold.ttf", size=12):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            for alt in [name, "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf"]:
                try: return ImageFont.truetype(alt, size)
                except Exception: pass
        return ImageFont.load_default()

    def _draw_no_signal(self):
        theme = self.theme_combo.currentIndex()
        model = self.box_model_combo.currentIndex() if hasattr(self, 'box_model_combo') else 0
        W, H  = 960, 540

        # Pre-configured color theme maps: (bg_hex, border_hex)
        theme_cfg = {
            0: ("#0f0f12", "#dc4646"),
            1: ("#0a0e17", "#00f0ff"),
            2: ("#1a1000", "#ffb000"),
            3: ("#000000", "#00ff00"),
            4: ("#f0f0f0", "#24292f")
        }

        # Pre-configured box model text maps: (title, message)
        model_cfg = {
            0: ("NO DIGITAL SIGNAL", "Check antenna & coaxial connection. Signal outage detected."),
            1: ("SYSTEM OFFLINE", "Re-establishing satellite uplink connection... Searching transponder."),
            2: ("SIGNAL DISCONNECTED", "Please check coaxial wall outlet and input connection cables.")
        }

        if theme in theme_cfg:
            bg_hex, border_hex = theme_cfg[theme]
            title_text, msg_text = model_cfg.get(model, model_cfg[0])
            pattern_idx = self.custom_pattern_combo.currentIndex() if hasattr(self, 'custom_pattern_combo') else 0
            img_path = self.custom_image_edit.text() if hasattr(self, 'custom_image_edit') else ""
            font_fam = "DejaVuSans"
            font_sz = 22
        else:  # Custom Theme (index 5)
            bg_hex = self.custom_bg_edit.text() if hasattr(self, 'custom_bg_edit') else "#0f0f12"
            border_hex = self.custom_border_edit.text() if hasattr(self, 'custom_border_edit') else "#dc4646"
            title_text = self.custom_title_edit.text() if hasattr(self, 'custom_title_edit') else "CUSTOM BOX  —  NO SIGNAL"
            msg_text = self.custom_msg_edit.text() if hasattr(self, 'custom_msg_edit') else ""
            pattern_idx = self.custom_pattern_combo.currentIndex() if hasattr(self, 'custom_pattern_combo') else 0
            img_path = self.custom_image_edit.text() if hasattr(self, 'custom_image_edit') else ""
            font_fam = self.custom_font_family_edit.text() if hasattr(self, 'custom_font_family_edit') else "DejaVuSans"
            try: font_sz = int(self.custom_font_size_edit.text()) if hasattr(self, 'custom_font_size_edit') else 22
            except Exception: font_sz = 22

        def hex_to_rgb(h):
            h = h.lstrip('#')
            try: return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
            except Exception: return (0, 0, 0)

        bg_color = hex_to_rgb(bg_hex)
        border_color = hex_to_rgb(border_hex)
        
        # Text color
        if theme == 4:
            text_color = (36, 41, 47)
        elif theme == 5:
            # Custom theme: calculate luminance of bg_color to decide on black or white text
            lum = 0.299 * bg_color[0] + 0.587 * bg_color[1] + 0.114 * bg_color[2]
            text_color = (36, 41, 47) if lum > 128 else (255, 255, 255)
        else:
            text_color = (255, 255, 255)

        def draw_color_bars(draw_obj, w, h):
            colors = [
                (255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
                (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)
            ]
            bar_w = w // len(colors)
            for i, col in enumerate(colors):
                draw_obj.rectangle([i*bar_w, 0, (i+1)*bar_w if i < len(colors)-1 else w, h], fill=col)

        def draw_smpte_bars(draw_obj, w, h):
            h1 = (h * 2) // 3
            colors = [
                (192, 192, 192), (192, 192, 0), (0, 192, 192), (0, 192, 0),
                (192, 0, 192), (192, 0, 0), (0, 0, 192)
            ]
            bar_w = w // len(colors)
            for i, col in enumerate(colors):
                draw_obj.rectangle([i*bar_w, 0, (i+1)*bar_w if i < len(colors)-1 else w, h1], fill=col)
            h2 = h1 + h // 12
            rev_colors = [
                (0, 0, 192), (19, 19, 19), (192, 0, 192), (19, 19, 19),
                (0, 192, 192), (19, 19, 19), (192, 192, 192)
            ]
            for i, col in enumerate(rev_colors):
                draw_obj.rectangle([i*bar_w, h1, (i+1)*bar_w if i < len(rev_colors)-1 else w, h2], fill=col)
            h3 = h
            draw_obj.rectangle([0, h2, bar_w, h3], fill=(255, 255, 255))
            draw_obj.rectangle([bar_w, h2, bar_w*2, h3], fill=(0, 0, 0))
            draw_obj.rectangle([bar_w*2, h2, bar_w*3, h3], fill=(0, 33, 79))
            draw_obj.rectangle([bar_w*3, h2, bar_w*4, h3], fill=(255, 255, 255))
            draw_obj.rectangle([bar_w*4, h2, bar_w*5, h3], fill=(51, 0, 114))
            draw_obj.rectangle([bar_w*5, h2, w, h3], fill=(19, 19, 19))

        def draw_grid(draw_obj, w, h):
            draw_obj.rectangle([0, 0, w, h], fill=(0, 0, 0))
            grid_size = 40
            for x in range(0, w, grid_size):
                draw_obj.line([x, 0, x, h], fill=(255, 255, 255), width=1)
            for y in range(0, h, grid_size):
                draw_obj.line([0, y, w, y], fill=(255, 255, 255), width=1)

        def draw_white_noise(w, h):
            arr = np.random.randint(0, 256, (h, w, 3), dtype=np.uint8)
            return Image.fromarray(arr, 'RGB')

        def draw_custom_image(w, h, path):
            if not path or not os.path.exists(path):
                return None
            cached_img = getattr(self, '_cached_rx_bg_img', None)
            cached_path = getattr(self, '_cached_rx_bg_path', None)
            cached_w = getattr(self, '_cached_rx_bg_w', None)
            cached_h = getattr(self, '_cached_rx_bg_h', None)
            if cached_img and cached_path == path and cached_w == w and cached_h == h:
                return cached_img
            try:
                img_loaded = Image.open(path).convert('RGB')
                resized = img_loaded.resize((w, h), Image.Resampling.LANCZOS)
                self._cached_rx_bg_img = resized
                self._cached_rx_bg_path = path
                self._cached_rx_bg_w = w
                self._cached_rx_bg_h = h
                return resized
            except Exception as e:
                print(f"[THEME] Failed to load custom image: {e}")
            return None

        img = None
        if pattern_idx == 1:
            img = Image.new('RGB', (W, H), (0, 0, 0))
            draw_smpte_bars(ImageDraw.Draw(img), W, H)
        elif pattern_idx == 2:
            img = Image.new('RGB', (W, H), (0, 0, 0))
            draw_color_bars(ImageDraw.Draw(img), W, H)
        elif pattern_idx == 3:
            img = Image.new('RGB', (W, H), (0, 0, 0))
            draw_grid(ImageDraw.Draw(img), W, H)
        elif pattern_idx == 4:
            img = draw_white_noise(W, H)
        elif pattern_idx == 5:
            img = draw_custom_image(W, H, img_path)

        if img is None:
            img = Image.new('RGB', (W, H), bg_color)

        draw = ImageDraw.Draw(img)
        fb = self._get_font(font_fam + "-Bold.ttf" if not font_fam.lower().endswith(('.ttf', '.otf')) else font_fam, int(font_sz * 1.36))
        fn = self._get_font(font_fam + ".ttf" if not font_fam.lower().endswith(('.ttf', '.otf')) else font_fam, font_sz)

        # Paste semi-transparent card box in the center
        overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        ol_draw = ImageDraw.Draw(overlay)
        ol_draw.rectangle([44, 44, W-44, H-44], fill=bg_color + (180,), outline=border_color + (255,), width=4)
        img.paste(overlay, (0, 0), overlay)

        draw.text((66, 66), title_text, fill=border_color, font=fb)
        draw.line([66, 110, W-66, 110], fill=border_color, width=4)

        words = msg_text.split(' ')
        lines = []
        cur_line = ""
        for word in words:
            test_line = cur_line + (" " if cur_line else "") + word
            if len(test_line) * (font_sz * 0.6) > W - 132:
                lines.append(cur_line)
                cur_line = word
            else:
                cur_line = test_line
        if cur_line:
            lines.append(cur_line)

        for i, line in enumerate(lines[:8]):
            draw.text((66, 130 + i * (font_sz + 12)), line, fill=text_color, font=fn)

        self.current_rx_frame = img.copy()
        qimg = QImage(img.tobytes('raw','RGB'), W, H, W * 3, QImage.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(qimg)
        
        scaled_pix = pixmap.scaled(self.recv_screen.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.recv_screen.setPixmap(scaled_pix)
        
        if self.fullscreen_target == 'rx' and self.fullscreen_view:
            fs_pix = pixmap.scaled(self.fullscreen_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.fullscreen_view.setPixmap(fs_pix)

    # ─────────────────────────────────────────────────────────────
    #  RECORDING
    # ─────────────────────────────────────────────────────────────
    def _toggle_rec(self, checked):
        self.recording = checked
        if checked:
            self.record_frame_count = 0
            self.record_start_time = time.time()
            self.timer_record.start(int(1000 / max(self.fps, 1)))
            self.btn_record.setText("Stop Recording")
            self.btn_record.setStyleSheet(
                "background:#da3633;color:white;font-weight:bold;")
            self.rec_lbl.setText("Recording RX (real-time encoding to disk)...")
            self.btn_save.setEnabled(False)

            # Start real-time background video encoder
            temp_video_path = os.path.join(APP_DATA_DIR, "rx_video_temp.mp4")
            if os.path.exists(temp_video_path):
                try: os.remove(temp_video_path)
                except Exception: pass

            cmd = [
                'ffmpeg', '-y', '-loglevel', 'error',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                '-framerate', str(max(self.fps, 1)),
                '-s', '960x540',
                '-i', 'pipe:0',
                '-c:v', 'libx264', '-crf', '22', '-preset', 'superfast',
                '-pix_fmt', 'yuv420p',
                temp_video_path
            ]
            try:
                self.record_ffmpeg_proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except Exception as e:
                print(f"[REC] Failed to start real-time FFmpeg process: {e}")
                self.record_ffmpeg_proc = None
            
            # Open the temporary TS capture file
            try:
                rec_path = os.path.join(APP_DATA_DIR, "rx_capture.ts")
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
            
            # Close real-time video encoder process
            if hasattr(self, 'record_ffmpeg_proc') and self.record_ffmpeg_proc:
                try:
                    if self.record_ffmpeg_proc.stdin:
                        self.record_ffmpeg_proc.stdin.close()
                    self.record_ffmpeg_proc.wait(timeout=10)
                except Exception as e:
                    print(f"[REC] Error closing real-time FFmpeg: {e}")
                self.record_ffmpeg_proc = None
            
            # Close the capture file
            if hasattr(self, '_rec_file') and self._rec_file:
                try:
                    if self.mpeg_decoder:
                        self.mpeg_decoder.recording_file = None
                    self._rec_file.close()
                except Exception:
                    pass
            self._rec_file = None
            
            # Check if files exist and have size
            rec_path = os.path.join(APP_DATA_DIR, "rx_capture.ts")
            temp_video_path = os.path.join(APP_DATA_DIR, "rx_video_temp.mp4")
            has_video = os.path.exists(temp_video_path) and os.path.getsize(temp_video_path) > 0
            if has_video:
                sz = os.path.getsize(temp_video_path) / 1024.0
                if os.path.exists(rec_path):
                    sz += os.path.getsize(rec_path) / 1024.0
                self.rec_lbl.setText(f"Stopped — {sz:.1f} KB captured, {getattr(self, 'record_frame_count', 0)} frames")
                self.btn_save.setEnabled(True)
            else:
                self.rec_lbl.setText("Stopped — no data captured")
                self.btn_save.setEnabled(False)

    def _record_tick(self):
        if self.recording and self.current_rx_frame:
            # Prevent memory/disk exhaustion by limiting to 1800 frames (~1 min at 30fps)
            if getattr(self, 'record_frame_count', 0) < 1800:
                img_bytes = self.current_rx_frame.tobytes()
                if hasattr(self, 'record_ffmpeg_proc') and self.record_ffmpeg_proc and self.record_ffmpeg_proc.stdin:
                    try:
                        self.record_ffmpeg_proc.stdin.write(img_bytes)
                        self.record_ffmpeg_proc.stdin.flush()
                        self.record_frame_count += 1
                        self.rec_lbl.setText(f"Recording...  {self.record_frame_count} frames")
                    except Exception as e:
                        print(f"[REC] Error writing frame to FFmpeg: {e}")
                        self._toggle_rec(False)
                        self.btn_record.setChecked(False)
            else:
                self._toggle_rec(False)
                self.btn_record.setChecked(False)
                self.rec_lbl.setText("Recording stopped (MAX FRAMES REACHED)")

    def _save_rec(self):
        temp_video_path = os.path.join(APP_DATA_DIR, "rx_video_temp.mp4")
        if not os.path.exists(temp_video_path) or os.path.getsize(temp_video_path) == 0:
            return
            
        rec_path = os.path.join(APP_DATA_DIR, "rx_capture.ts")
        has_audio_input = os.path.exists(rec_path) and os.path.getsize(rec_path) > 1880
        
        fname, _ = QFileDialog.getSaveFileName(
            self, "Save RX Recording", os.path.join(USER_HOME, "rx_recording.mp4"),
            "MP4 Video (*.mp4)")
        if not fname: return
        if not fname.lower().endswith('.mp4'):
            fname += '.mp4'

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

        self.rec_lbl.setText("Encoding to MP4 (transcoding TS with Audio)..." if has_audio_input else "Encoding to MP4 (video only)...")
        QtWidgets.QApplication.processEvents()
        
        # Transcode using FFmpeg in a background thread to keep UI responsive
        def transcode_worker():
            vf_filters = ['setpts=PTS-STARTPTS']
            
            # Apply aspect ratio
            aspect_idx = self.rec_aspect_combo.currentIndex() if hasattr(self, 'rec_aspect_combo') else 0
            if aspect_idx == 0: # 16:9
                vf_filters.append(f'scale={target_w}:{target_h}:flags=lanczos')
                vf_filters.append('setdar=16/9')
            elif aspect_idx == 1: # 4:3
                vf_filters.append(f'scale={target_w}:{target_h}:flags=lanczos')
                vf_filters.append('setdar=4/3')
            else: # Stretch or Follow
                vf_filters.append(f'scale={target_w}:{target_h}:flags=lanczos')

            if has_audio_input:
                cmd = [
                    'ffmpeg', '-y', '-loglevel', 'error',
                    '-i', temp_video_path,
                    '-vn', '-i', rec_path,
                    '-map', '0:v:0', '-map', '1:a:0?',
                    '-vf', ','.join(vf_filters),
                    '-af', 'asetpts=PTS-STARTPTS',
                    '-c:v', 'libx264', '-crf', '22', '-preset', 'superfast',
                    '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '128k',
                    '-shortest',
                    fname
                ]
            else:
                cmd = [
                    'ffmpeg', '-y', '-loglevel', 'error',
                    '-i', temp_video_path,
                    '-map', '0:v:0',
                    '-vf', ','.join(vf_filters),
                    '-c:v', 'libx264', '-crf', '22', '-preset', 'superfast',
                    '-pix_fmt', 'yuv420p',
                    '-shortest',
                    fname
                ]
            try:
                err_log_path = os.path.join(APP_DATA_DIR, 'transcode_stderr.log')
                with open(err_log_path, 'w') as err_log:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL, stderr=err_log)
                    proc.wait(timeout=120)
                
                if proc.returncode == 0:
                    QtCore.QMetaObject.invokeMethod(self.rec_lbl, "setText",
                        QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, f"Saved: {os.path.basename(fname)}"))
                else:
                    err_content = ""
                    try:
                        if os.path.exists(err_log_path):
                            with open(err_log_path, 'r') as f:
                                err_content = f.read()
                    except Exception:
                        pass
                    QtCore.QMetaObject.invokeMethod(self.rec_lbl, "setText",
                        QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, "Save failed: transcode error"))
                    print(f"[REC-SAVE] FFmpeg failed. Code: {proc.returncode}. Log:\n{err_content}")
            except Exception as e:
                QtCore.QMetaObject.invokeMethod(self.rec_lbl, "setText",
                    QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, f"Save failed: {e}"))

        threading.Thread(target=transcode_worker, daemon=True).start()

    # ─────────────────────────────────────────────────────────────
    #  CLOSE / CLEANUP / EVENTS
    # ─────────────────────────────────────────────────────────────
    def resizeEvent(self, event):
        # Trigger an immediate redraw of the screens to fit the new layout size
        if hasattr(self, 'orig_screen') and self.orig_screen.pixmap():
            self.media_loop()
        if hasattr(self, 'recv_screen') and self.recv_screen.pixmap():
            self.update_rx_display()
        super().resizeEvent(event)
        
    def closeEvent(self, event):
        print("Shutting down DTV Playground...")
        for t in [self.timer_tx, self.timer_metrics, self.timer_rx]:
            try: t.stop()
            except Exception: pass

        if self.preview_proc:
            try:
                if self.preview_proc.stdin: self.preview_proc.stdin.close()
            except Exception: pass
            try:
                if self.preview_proc.stdout: self.preview_proc.stdout.close()
            except Exception: pass
            try:
                self.preview_proc.terminate()
                self.preview_proc.wait(timeout=0.2)
            except Exception: pass
            self.preview_proc = None
        self._safe_stop_thread('preview_thread')

        self._safe_stop_thread('mpeg_encoder')
        self._safe_stop_thread('mpeg_decoder')
        if self.channel_relay:
            try: self.channel_relay.stop()
            except Exception: pass
        if self.aplay_proc:
            try: self.aplay_proc.terminate()
            except Exception: pass
        if self.gr_tb:
            try: self.gr_tb.stop()
            except Exception: pass

        event.accept()
        import os
        os._exit(0)


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
