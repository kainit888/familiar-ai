# B4: Environment-Sound Recognition (YAMNet) — Setup

Pico can recognize **non-speech environment sounds** (doorbell, glass break, alarm,
…) on the Pi5 using Google's **YAMNet** (521 AudioSet classes). It reuses the
always-on STT path: when Whisper returns empty for a segment (no speech), the audio
is sent to YAMNet; important sounds boost the `audio_concern` drive and are stored
in memory, which Pico uses as judgment material at heartbeat time. There is **no
dedicated audio self-speech path** — sounds only inform Pico, they don't make it talk.

> The feature **degrades to a no-op** without the runtime or the model: Pico's STT
> and everything else keep working. Nothing here is required for normal operation.

## What you install (NOT bundled in the repo)

1. **tflite-runtime** (Pi5 aarch64):
   ```bash
   uv pip install "tflite-runtime>=2.14.0"      # or: uv sync --extra audio_event
   ```
   (On non-aarch64 the extra installs nothing; YAMNet stays a no-op.)

2. **YAMNet model + class map** → place in `~/yamnet_models/`:
   - `yamnet.tflite` — the TFLite model
   - `yamnet_class_map.csv` — the 521 ordered class display names (`index,mid,display_name`)

   Download the TFLite export of YAMNet from Kaggle Models / TF Hub:
   - https://www.kaggle.com/models/google/yamnet/tfLite  (Apache-2.0)
   The class map ships with the model export; if you only have the model, fetch
   `yamnet_class_map.csv` from the same source.
   ```bash
   mkdir -p ~/yamnet_models
   # copy yamnet.tflite and yamnet_class_map.csv into ~/yamnet_models/
   ```

## Configure (.env)

```
YAMNET_ENABLED=true                # default true (but no-op without the model)
YAMNET_MODEL_PATH=~/yamnet_models/yamnet.tflite
YAMNET_CLASS_MAP_PATH=             # default: <model dir>/yamnet_class_map.csv
YAMNET_CONFIDENCE_THRESHOLD=0.5    # importance threshold
YAMNET_IMPORTANT_LABELS=doorbell,glass,alarm,fire alarm,smoke detector,scream,shout,siren
YAMNET_TOP_K=5                     # how many top labels to record
YAMNET_BOOST_AMOUNT=0.3            # audio_concern boost per important sound
```

`YAMNET_IMPORTANT_LABELS` is a comma list matched case-insensitively as a substring
against the AudioSet display name (e.g. `alarm` matches `Alarm`, `Fire alarm`,
`Smoke detector, smoke alarm`). Default is a conservative safety set.

## How it flows (architecture)

```
RTSP audio → VAD segment → Whisper transcribe
   ├─ non-empty text → existing speech path (on_speech), YAMNet NOT called
   └─ empty (no speech) → audio_event.classify (YAMNet) →
        AudioEvent → TUI _on_audio_event →
          • scene.record_audio_event(label)   (scene_events, surfaces in prompt)
          • memory.save(kind="audio_event")   (recallable)
          • if important → desires.boost("audio_concern")   (NO say())
   → next heartbeat / desire turn: Pico recalls the sound and decides
```

Two-layer separation: inference (`pico_agent/adapters/audio_event.py`) does not
import `familiar_agent`; the callback wiring lives in `familiar_agent` (tui.py).

## Verify on the device (Stage D)

Unit tests use a mock interpreter (no real model). To confirm real recognition:

```bash
tail -f ~/.cache/familiar-ai/app.log | grep -E "audio_event|heard"
```
- Play a real **doorbell / hand clap / glass clink** near the Tapo mic while idle.
- Expect a `🔊 heard <Label> (<conf>)` log line.
- For an important label above the threshold, `audio_concern` is boosted (Pico may
  bring it up on its next idle/heartbeat turn).
- Tune `YAMNET_CONFIDENCE_THRESHOLD` / `YAMNET_IMPORTANT_LABELS` if a TV/pet causes
  false positives.

## Removing / disabling

Set `YAMNET_ENABLED=false`, or simply don't install the model — the feature
silently no-ops with a single startup warning.
