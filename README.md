# UCSD Schedule Helper (UCSD 排课助手)

The new TSS system has made course planning painful for many students, and the school's official planner is... okay at best. So I built my own planner that satisfies my needs. I used this tool to plan my own Fall 2026 schedule — it works, it is genuinely useful, and I hope it helps you too.

A local-first course scheduling tool for UCSD students. It pulls real section data from UCSD Class Planner, walks you through lecture/discussion choices, generates every conflict-free schedule, and can ask DeepSeek for advice — all in your browser, no account required.

![Demo](docs/demo.png)

## Features

- **Real UCSD data** — sections, times, rooms, instructors, and seats from UCSD Class Planner / TSS
- **Step-by-step wizard** — pick a lecture first, then discussion/lab; the calendar updates live and conflicts show alternatives
- **All-schedules mode** — enumerates every conflict-free combination; compare, save, and export
- **Rate My Professor** — ratings shown next to instructor names (best-effort fetch)
- **Walking estimates** — between buildings using UCSD official Wayfinding routes, with a fallback
- **Final exams** — shown on a separate date-based calendar
- **Dynamic calendar** — starts about 1 hour before your first class instead of wasting space from 7:00
- **DeepSeek Q&A** — the current schedule and full section details are attached automatically
- **Light/dark theme** and **中文/English**, both persisted
- **Export** — PNG / SVG / PDF with a print-friendly layout
- **Zero-server data** — everything is local; no telemetry, no accounts

## Quick Start

Requirements: **Python 3.8+** and any modern browser.

**Windows:** double-click `start.bat` (uses port 8778 and prevents double-start).

**Manual / macOS / Linux:**

```bash
python scheduler.py --port 8778
# or python3 scheduler.py --port 8778
```

Then open <http://127.0.0.1:8778/>.

> First run is slower (usually under a minute): the app builds local caches for buildings, walking routes, and professor ratings. Later runs are fast.

## How to Use

1. **Add courses** — type a code like `MATH 100A` and click Add.
2. **Wizard mode** (default) — decide one course at a time: lecture → discussion/lab. The calendar preview shows your chosen blocks and remaining seats.
3. **All-schedules mode** — generate every conflict-free combination, browse them, compare up to 3, or pin/fix sections.
4. **Export** — PNG / SVG / PDF with the schedule title, week grid, and final exams.
5. **DeepSeek** (optional) — ask "is this schedule too heavy?"; the LLM will give response considering all course info and class combinations.

Courses, wizard progress, favorites, and chat history auto-save to `saved_data.json` in the same folder. Delete that file to reset.

## DeepSeek AI (Optional)

- Get your own API key at [platform.deepseek.com](https://platform.deepseek.com)
- The key stays in your browser's `localStorage` only — never written to disk, never sent anywhere except DeepSeek's API when you ask
- Without a key, every scheduling feature still works

## Privacy

- Local-only: the server listens on `127.0.0.1` and is not exposed to the internet
- No accounts, no analytics, no telemetry
- `buildings_cache.json`, `routes_cache.json`, and `rmp_cache.json` are regenerable caches; safe to delete

## Project Structure

```
scheduler.py   Python stdlib HTTP server + UCSD Class Planner proxy
index.html     Single-page UI (vanilla HTML/CSS/JS, no build step)
start.bat      Windows launcher (port 8778)
README.md      This file
使用说明.md     Chinese walkthrough
```

## Tech Stack

- Python standard library only (no pip installs)
- Vanilla HTML/CSS/JS (no frameworks, no bundler)
- One file to deploy: `scheduler.py` + `index.html`

## Troubleshooting

- **SSL certificate errors** — the app automatically retries UCSD public data without certificate verification; if it still fails, check your proxy/network.
- **No feasible schedule** — remove instructor locks or the "only open sections" filter, or drop a course.
- **Port already in use** — run `python scheduler.py --port 9000`.
- **PDF colors look gray** — if your browser still strips backgrounds, enable "Background graphics" in the print dialog.

## Disclaimer

Not affiliated with or endorsed by UC San Diego. Course data comes from UCSD Class Planner / WebReg and may change — always verify on WebReg/TSS before enrolling. Rate My Professor data is third-party and best-effort.

## License

MIT
