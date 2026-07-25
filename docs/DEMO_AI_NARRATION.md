# AI Narration Workflow (no speaking)

You generate the voice-over first, then cut the screen recording to fit it.
**This is the opposite of the normal order, and it's much easier** — TTS timing
is fixed and can't be nudged, so the audio becomes your timeline and the video
bends around it.

---

## 1. Generate the narration

```powershell
cd C:\python\sarmaya
.\.venv\Scripts\python.exe _demo_narrate.py
```

Writes nine MP3s to `demo_assets/narration/` — **one per scene**, not one long
file. That's deliberate: per-scene clips let you align each section
independently, so a slow OCR response in Scene 1 doesn't shove everything after
it out of sync.

**Actual durations (measured):**

| Clip | Length | Scene |
|---|---|---|
| `00_hook.mp3` | 0:27 | Hook |
| `01_extraction.mp3` | 0:43 | Upload & AI extraction |
| `02_duplicate.mp3` | 0:25 | Duplicate detection |
| `03_inbox.mp3` | 0:34 | Decision Inbox |
| `04_escalation.mp3` | 0:38 | SLA escalation |
| `05_agent.mp3` | 0:43 | The AI agent |
| `06_governance.mp3` | 0:31 | SoD + vendor gates |
| `07_audit.mp3` | 0:47 | Live Audit Mode |
| `08_close.mp3` | 0:22 | Close |
| **Total** | **5:14** | |

With cuts and breathing room, the finished video lands around **6 minutes**.

### Pick a different voice
```powershell
.\.venv\Scripts\python.exe _demo_narrate.py --list-voices     # browse English voices
.\.venv\Scripts\python.exe _demo_narrate.py --voice en-US-BrianNeural
.\.venv\Scripts\python.exe _demo_narrate.py --rate -10%       # slower
```

Worth auditioning: `en-US-AndrewNeural` (default — warm, natural),
`en-US-BrianNeural` (crisp, corporate), `en-GB-RyanNeural` (British),
`en-US-AriaNeural` / `en-US-JennyNeural` (female). Generate the hook with two or
three and pick by ear before committing.

### Why edge-tts
It's free, runs locally, needs no API key, and the neural voices are genuinely
good. If you want the very best quality, **ElevenLabs** is a step up (free tier
covers ~10k characters/month, which is enough for this script) — paste the text
from `_demo_narrate.py` scene by scene. The workflow below is identical either way.

---

## 2. Record the screen — silent

- [ ] In OBS, **mute or remove the microphone source** (you don't want room noise)
- [ ] Follow `DEMO_RECORDING_STEPS.md`, but **ignore every `[SAY]` step**
- [ ] **Listen to each narration clip before recording that scene** so you know
      how long you have to fill. This is the whole trick.
- [ ] Move deliberately — slow cursor, pause on anything the narration mentions
- [ ] **Record a bit long.** Extra footage is trimmable; missing footage means a reshoot.
- [ ] Pause ~2 seconds between scenes so you have clean cut points

**Per-scene footage targets** (aim slightly over the clip length):

| Scene | Narration | Shoot at least |
|---|---|---|
| 0 Hook | 0:27 | 0:35 |
| 1 Extraction | 0:43 | 0:55 |
| 2 Duplicate | 0:25 | 0:35 |
| 3 Inbox | 0:34 | 0:45 |
| 4 Escalation | 0:38 | 0:45 |
| 5 Agent | 0:43 | 0:55 |
| 6 Governance | 0:31 | 0:45 |
| 7 Audit | 0:47 | 1:00 |
| 8 Close | 0:22 | 0:30 |

> Scenes 1 and 5 wait on live API calls (OCR/Claude), so their timing varies —
> that's why they get the biggest buffer.

---

## 3. Merge in an editor (recommended)

**DaVinci Resolve** (free) or **CapCut** (free, simpler) or **Shotcut**.

1. Drop the silent screen recording on video track 1
2. Drop `00_hook.mp3` … `08_close.mp3` on audio track 1, **in order, end to end**
3. For each scene, working left to right:
   - Trim the video so the on-screen action lines up with what the voice is saying
   - Use **speed adjustment** on dead time (a spinner, a slow scroll) rather than
     cutting it — a 2× ramp over a loading spinner looks intentional
   - Leave ~0.5 s of silence between clips so it doesn't feel rushed
4. Add the title card over the first 3 seconds
5. Export **1080p, 30fps, MP4**

**Alignment rule of thumb:** the action should happen *just before* the narration
mentions it — click, beat, then the voice explains. Never the reverse.

---

## 4. Or merge with ffmpeg (fast, no editor)

Only if your screen recording already matches the narration timing closely.

```powershell
# 1. Join the narration clips into one track
cd C:\python\sarmaya\demo_assets\narration
(Get-ChildItem *.mp3 | Sort-Object Name | ForEach-Object { "file '$($_.Name)'" }) | Set-Content -Encoding utf8 list.txt
ffmpeg -f concat -safe 0 -i list.txt -c copy narration_full.mp3

# 2. Mux it onto the silent screen recording
ffmpeg -i screen.mp4 -i narration_full.mp3 -c:v copy -map 0:v:0 -map 1:a:0 -shortest demo_final.mp4
```

Add background music at low volume (optional):
```powershell
ffmpeg -i demo_final.mp4 -i music.mp3 -filter_complex "[1:a]volume=0.06[m];[0:a][m]amix=inputs=2:duration=first" -c:v copy demo_final_music.mp4
```

Keep music **under 8% volume** — it should be barely perceptible under speech.

---

## 5. Editing the words

All narration text lives in the `SCENES` dict in `_demo_narrate.py`. Edit it
there and re-run — don't hand-edit the MP3s.

**It's written for a speech engine, not a reader**, which is why it looks odd on
the page:

| Written as | Because |
|---|---|
| `Sarmaya O S`, `Fast A P I`, `O C R`, `S L A`, `C F O` | spaced letters make the engine spell them out instead of attempting a word |
| `Postgres` (not PostgreSQL) | "PostgreSQL" gets mangled by most engines |
| `two hundred and sixty one tests` | digits get read inconsistently |
| `forty eight hour`, `seventy two hours` | same reason |
| short sentences, blank lines between paragraphs | blank lines become natural pauses |

If you add a new line, follow those conventions or it will sound robotic.

---

## 6. Quality checklist before you export

- [ ] Every claim in the voice-over matches what's visible on screen at that moment
- [ ] No dead air longer than ~2 seconds
- [ ] The three money shots are clearly visible while being described:
      the duplicate warning, both 403s, and `"verified": true`
- [ ] Text is readable at 1080p (that's why the browser is at 125%)
- [ ] Audio doesn't clip; music (if any) sits under the voice
- [ ] Total runtime is **6 minutes or under**
- [ ] Watch it once start to finish without touching anything
