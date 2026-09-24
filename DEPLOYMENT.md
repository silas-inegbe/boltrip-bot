# Deploying @Boltrip_bot to the Cloud (24/7 Hosting)

Your bot code is fully containerized and cloud-ready with Docker, FFmpeg, and Python 3.11.

---

## Option 1: Deploy on Render (Easiest - Free Tier Available)

1. Create a free account at [render.com](https://render.com).
2. Push your `boltrip_bot` code to a private or public GitHub repository.
3. On Render Dashboard:
   * Click **New +** ➔ **Background Worker** (or **Web Service**).
   * Connect your GitHub repository.
   * Runtime: Choose **Docker** (Render will automatically detect your `Dockerfile`).
   * Environment Variables: Add `BOT_TOKEN` with your token:
     `<YOUR_BOT_TOKEN>`
4. Click **Deploy**. Render will build the container, install FFmpeg, and keep the bot running 24/7!

---

## Option 2: Deploy on Railway (Ultra Fast 1-Click)

1. Go to [railway.com](https://railway.com).
2. Click **New Project** ➔ **Deploy from GitHub repo**.
3. Select your `boltrip_bot` repository.
4. Go to **Variables** and add:
   * `BOT_TOKEN`: `<YOUR_BOT_TOKEN>`
5. Railway automatically builds from your `Dockerfile` and starts the bot immediately.

---

## Option 3: Deploy on a Linux VPS (DigitalOcean / Hetzner / Oracle Cloud)

If you have a Linux server (Ubuntu/Debian):

```bash
# 1. Clone your repo or copy the files
git clone <your-repo-url> boltrip_bot
cd boltrip_bot

# 2. Build and run with Docker
docker build -t boltrip-bot .
docker run -d --name boltrip --restart always -e BOT_TOKEN="<YOUR_BOT_TOKEN>" boltrip-bot
```

The `--restart always` flag ensures the bot automatically restarts if the server reboots.
