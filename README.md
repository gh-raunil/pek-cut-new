# PeakCut (with Power)

Turn long videos and live streams into captioned vertical clips, ready to post. Free and open source: run it on your own computer or server, with your own AI key.

Paste a YouTube, Twitch or Kick link (or upload a video file) and PeakCut finds the moments worth clipping, cuts them to 9:16, adds word-by-word animated captions and hook titles, and can post them to your connected accounts. It also features a fully interactive visual timeline editor with multi-trim, zoom, speed controls, and live review.

---

## ✨ Features

- **🎬 Interactive Visual Timeline**:
  - Drag handles to trim start and end points accurately.
  - Drag the range bar to slide the entire selected clip window across the source video.
  - **Fine Stepping**: Quick buttons for `±0.1s` and `±1.0s` adjustments or direct manual input.
  - **Clean White UI**: Modern, clean, high-contrast timeline layout.

- **🔍 Zoom Controls (Ctrl + Scroll Wheel)**:
  - Hold <kbd>Ctrl</kbd> (or <kbd>Cmd</kbd> on macOS) and scroll up/down over the timeline to smoothly zoom between `1.0x` and `30.0x`.
  - Centers dynamically on your mouse pointer with adaptive time-tick rulers.
  - Dedicated buttons for **Zoom In**, **Zoom Out**, and **Fit Clip to View**.

- **✂️ Multi-Trim & Cut Deletion**:
  - **Split at Playhead**: Scrub to any point and click `✂️ Split at [time]` to divide the clip into separate segments.
  - **Delete Unwanted Parts**: Click `✕ Delete` on any segment chip to cut out dead air, boring stretches, or pauses.
  - **Live Cut-Skipping**: The review player automatically leaps over deleted segments in real time so you hear and see the final result before rendering.
  - **Restore Anytime**: One-click `↺ Restore` or `Reset cuts` to undo any splits.
  - **Seamless FFmpeg Concat**: When re-making the clip, FFmpeg extracts only the kept segments, concatenates video and audio seamlessly with loudness normalization, and automatically remaps subtitle timings.

- **⚡ Video Speed Controls**:
  - Select speeds: `-2x`, `-1x`, `0.5x`, `0.75x`, `1x`, `1.25x`, `1.5x`, `2x`.
  - Review player supports smooth reverse playback (`-1x`, `-2x`) and rate switching.
  - FFmpeg applies video filters (`setpts`, `reverse`) and audio filters (`atempo`, `areverse`), scaling subtitle durations to match.

- **🤖 AI-Powered Clipping**:
  - Supports Google Gemini, OpenAI, Grok (xAI), Groq, OpenRouter, Anthropic Claude, or local Ollama models.
  - Loudness-based fallback if no AI key is provided.

- **👀 Vision-Based Clipping**:
  - Automatically identifies gameplay highlights, action sequences, or silent video segments by analyzing small frame grids on your machine.

- **🎨 Word-by-Word Captions & Facecam Layouts**:
  - Animated captions with 3 styles: `Bounce`, `Clean`, and `Hype`.
  - Automatic webcam/facecam detection for gaming split-screen or PiP (Picture-in-Picture).

- **📢 Multi-Platform Publishing**:
  - One-click posting and scheduling for YouTube Shorts, Instagram Reels, and TikTok.

---

## 🚀 Quick Start & How to Run

### 1. Prerequisites
- **Python 3.10+**: Ensure Python 3.10, 3.11, or 3.12 is installed and added to your `PATH`.
- **FFmpeg**: Required for media processing.
  - **Windows**: `winget install Gyan.FFmpeg` (or download from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/))
  - **macOS**: `brew install ffmpeg`
  - **Linux**: `sudo apt update && sudo apt install ffmpeg`

> [!NOTE]
> On Windows, keep the project in a short path (e.g. `C:\peakcut` or `D:\peakcut`) to avoid the Windows 260-character path limit for speech models and caption fonts.

---

### 2. Clone the Repository

```bash
git clone https://github.com/gh-raunil/peek-cut-with-power.git
cd peek-cut-with-power
```

---

### 3. Create and Activate a Virtual Environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows (Command Prompt):**
```cmd
python -m venv .venv
.\.venv\Scripts\activate.bat
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

---

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

---

### 5. Download Speech Model (Optional Pre-fetch)

The English speech recognition model (~480 MB) downloads automatically on first run. You can also pre-fetch it ahead of time:

```bash
python -m studio.fetch_model
```

*(For Arabic videos, download with: `python -m studio.fetch_model ar`)*

---

### 6. Start the Server

```bash
python -m studio.server
```

PeakCut starts listening at:
👉 **http://127.0.0.1:8765**

Open this address in your browser (Chrome, Edge, Firefox, or Safari). No sign-in is required for local single-user mode.

---

## ⚙️ AI Setup (Google Gemini, OpenAI, Ollama, etc.)

You can configure your AI provider in two ways:

### Option A: From the Dashboard (Recommended)
1. Open **http://127.0.0.1:8765** in your browser.
2. Go to **Settings → AI**.
3. Select your provider (e.g. `Google Gemini`, `OpenAI`, `Groq`, `Ollama`).
4. Paste your API key, click **Find models**, and press **Save and test**.

### Option B: Using a `.env` File
Create a `.env` file in the project root:

```ini
PEAKCUT_AI_PROVIDER=gemini
PEAKCUT_AI_MODELS=gemini-2.5-flash
PEAKCUT_AI_KEY=your_gemini_api_key_here
```

| Provider | Setting | Key URL |
|---|---|---|
| **Google Gemini** | `gemini` | https://aistudio.google.com/apikey |
| **OpenAI** | `openai` | https://platform.openai.com/api-keys |
| **Grok (xAI)** | `xai` | https://console.x.ai |
| **Groq** | `groq` | https://console.groq.com/keys |
| **OpenRouter** | `openrouter` | https://openrouter.ai/keys |
| **Anthropic** | `anthropic` | https://console.anthropic.com |
| **Ollama (Local)** | `ollama` | Run locally on http://localhost:11434 (No key needed) |

---

## 🧪 Running Tests

To verify your environment and ensure all features pass:

```bash
# Run quick static screen and video tests
python tests/test_static.py

# Run full end-to-end integration test suite
python tests/run.py
```

---

## 📁 Project Structure

| Directory / File | Description |
|---|---|
| `studio/server.py` | FastAPI web server, routes, streaming media endpoints, and security gate |
| `studio/jobs.py` | Video processing pipeline, queue management, multi-trim segment validation |
| `studio/render.py` | FFmpeg rendering engine: cutting, concat filters, speed transforms, word remapping, captions |
| `studio/ai.py` | Unified client for AI providers (Gemini, OpenAI, Groq, Ollama, Claude) |
| `studio/brain.py` | Clip detection prompts, title and hook generation |
| `studio/vision.py` | Visual highlight detection and frame-grid generation |
| `studio/transcribe.py` | Speech-to-text engine using Sherpa-ONNX |
| `web/index.html` | Single-page UI with visual timeline, zoom, speed pills, and player preview |
| `tests/` | Comprehensive test suite for permissions, clipping, audio, and limits |

---

## 🛡️ License

This project is licensed under the [MIT License](LICENSE).
Caption fonts in `studio/fonts/` (Anton, Archivo Black, Poppins) are licensed under the SIL Open Font License.
