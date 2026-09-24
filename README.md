# ⚡ Boltrip Media Downloader Bot

A fast, clean, and 100% ad-free Telegram media downloader bot built with Python, `python-telegram-bot`, `yt-dlp`, and `aiohttp`.

Downloads high-resolution videos (up to 1080p Full HD) and extracts crystal-clear 320kbps MP3 audio with **no watermarks, no ads, and no subscription walls**.

---

## 🌟 Key Features

- **Multi-Platform Support:**
  - YouTube (Videos & Shorts)
  - TikTok (Without watermarks)
  - Instagram (Reels & Posts)
  - X / Twitter
  - Pinterest
  - Facebook & Reddit
- **Quality & Format Options:**
  - 🎬 1080p Full HD Video
  - 📱 720p Fast Video
  - 🎵 320kbps MP3 Audio Extraction
- **Bypasses Telegram 50 MB Cap:**
  - **Direct Phone Download:** Serves files directly to mobile browser via built-in `aiohttp` web server.
  - **Smart Video Compression:** In-chat two-pass FFmpeg compression to fit under 50 MB.
- **Universal 1-Tap Cancellation:**
  - Every step (analyzing, format selection, downloading, compressing, uploading) features an active `[ ❌ Cancel ]` button that safely halts tasks and purges temporary files.
- **Real-Time Progress Reporting:**
  - Live progress bar, download speed in MB/s, ETA, and byte counters.
- **Group & Inline Mode:**
  - Add to any Telegram group and use `/dl <link>`, `/video <link>`, or `/audio <link>`.
  - Type `@Boltrip_bot <link>` in any chat to share download buttons.
- **Multi-Channel Creator Support:**
  - Seamless tipping support via Polar (Global), Paystack (Africa), Telegram Stars, and Crypto.

---

## 🚀 Quick Start (Local Setup)

### 1. Clone the repository
```bash
git clone https://github.com/silas-inegbe/boltrip-bot.git
cd boltrip-bot
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```
> Note: Ensure `ffmpeg` is installed and accessible in your system PATH (or `imageio-ffmpeg` is installed).

### 3. Configure environment
Copy the example environment file and add your Telegram Bot Token:
```bash
cp .env.example .env
```
Edit `.env`:
```env
BOT_TOKEN=your_bot_token_from_botfather
PORT=8080
```

### 4. Run the Bot
```bash
python bot.py
```

---

## 🐳 Docker & Cloud Deployment

Boltrip is containerized and ready for 1-click deployment on Render, Railway, Fly.io, or any Linux VPS.

See [DEPLOYMENT.md](DEPLOYMENT.md) for step-by-step guides.

---

## 📜 License
MIT License. Built for speed and simplicity.
