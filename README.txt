# DTV SDR Modulation & Link Budget Playground

Welcome to the Digital Television (DTV) SDR Modulation & Link Budget Playground. This application simulates a complete digital television broadcasting system - including a transmitter, RF channel, propagation impairments, and a hardware-less receiver - running entirely in software on your PC.

The playground is compatible with Windows, macOS, and Linux, utilizing native subprocesses for high-performance video encoding and decoding.

---

## Key Features

*   Authentic Signal Outages: Unlike simpler tools, this simulation does not apply green overlay scripts or fake static. Instead, it corrupts actual MPEG-2 Transport Stream (MPEG-TS) packets at the byte level. The receiver's FFmpeg decoder processes this damaged stream natively, producing authentic motion smears, macroblock freezing, and audio dropouts.
*   Real-time Link Budget DSP: Adjust transmit power, receiver distance, and LNA pre-amplification. The signal quality calculation uses real free-space path loss (FSPL), atmospheric rain fade, and foliage terrain losses.
*   Flicker-Free Interlaced Scan: Toggle interlaced scan mode to see true temporal combing/weaving artifacts without 30Hz screens.
*   Interactive Playlist Control: Add and organize multiple files, seek smoothly, play, pause, and navigate tracks.
*   Double-Click Fullscreen: Double-click either the TX Preview or RX Display screens to view the video in frameless fullscreen mode. Press the Escape key (or double-click again) to exit.
*   Hardware Acceleration (Auto-Detect): Probes your GPU at startup. It will try Intel QuickSync Video (mpeg2_qsv) first, then VAAPI (mpeg2_vaapi - Linux standard for AMD/Intel), and fall back to Software if unsupported.

---

## System Requirements

*   Python: Version 3.7 or higher.
*   Python Packages: PyQt5, Pillow, numpy.
*   Media Tools (Required in System PATH):
    *   ffmpeg: Used to transcode, encode, and decode video streams.
    *   ffplay: Used for audio playback and stream verification.
    *   aplay (Optional, Linux-only): Standard ALSA audio player.

---

## Installation & Setup

### 1. Install Media Tools
Ensure ffmpeg and ffplay are installed and added to your system's environment variables (PATH).
*   Windows: Download builds from Gyan.dev or use "winget install Gyan.FFmpeg".
*   macOS: Install via Homebrew: "brew install ffmpeg".
*   Linux: Install via your package manager:
    *   Debian/Ubuntu: sudo apt update && sudo apt install ffmpeg alsa-utils
    *   Fedora: sudo dnf install ffmpeg alsa-utils
    *   Arch Linux: sudo pacman -S ffmpeg alsa-utils

### 2. Install Python Dependencies
Run the following command in your terminal or command prompt:
pip install PyQt5 Pillow numpy

### 3. Run the Playground
Launch the application:
python3 dtv_playground.py

---

## Documentation

For a detailed walkthrough, including a quick presets guide, troubleshooting tips, and hardware acceleration details, see:
*   manual.md (Markdown Version)
*   manual.txt (Plain Text Version)

---

## License

This project is licensed under the GNU General Public License v3.0. See the LICENSE file for the full text.
