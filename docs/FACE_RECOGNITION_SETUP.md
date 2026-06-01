# Phase G: Face Recognition — Setup

ピコ can recognize whether the person on camera **resembles you (カイニット)** by
comparing 128-dimensional face vectors. The result is fed to ピコ as a *hint only*:
on each `see()` the view text gets an extra line like
`[Face recognition] The person in view resembles kainit (confidence 0.82)…`.
The final "it's カイニット / it's someone else" decision is left to ピコ's own
judgment (Problem-1 autonomy + the existing identity-uncertainty guidance).

> The feature **degrades to a no-op** without the library, without face encodings,
> or when no face is detected: the ToM default stays `unknown_person`, `see()` works
> exactly as before, and nothing breaks. It is **off by default**.

## 1. Install the library (NOT bundled — heavy dlib build)

```bash
uv pip install "face-recognition>=1.3.0"     # or: uv sync --extra face_recognition
```

`face_recognition` builds **dlib** from source, which can take **30+ minutes on a
Raspberry Pi 5** and needs build tools:

```bash
sudo apt-get install -y build-essential cmake libopenblas-dev
```

(On non-aarch64 hosts the extra installs nothing and face recognition stays a no-op.
If the library is absent, ピコ logs one warning and runs normally.)

## 2. Prepare your face photos (NOT bundled — your private data)

Put a few clear, front-facing photos of yourself in a per-person folder:

```
~/.familiar_ai/face_samples/kainit/
    photo1.jpg
    photo2.jpg
    photo3.jpg
```

- 3–5 photos, good lighting, only your face clearly visible, works best.
- `.jpg`, `.jpeg`, and `.png` are accepted.
- The folder name (`kainit`) becomes the identity name ピコ sees. Keep it to one
  person for now (multi-person is out of scope).

## 3. Enroll (compute the face vector)

```bash
uv run python scripts/enroll_face.py            # name = kainit (default)
# or a different name:
uv run python scripts/enroll_face.py alice
```

This reads the sample photos, computes + averages their encodings, and writes:

```
~/.familiar_ai/face_encodings/kainit.npy
```

Re-run any time you add/replace photos. To "forget" a face, just delete its `.npy`.

## 4. Enable it (.env)

```
FACE_RECOGNITION_ENABLED=true        # master switch (default false)
FACE_RECOGNITION_TOLERANCE=0.6       # match strictness; LOWER = stricter (fewer
                                     #   false positives, more misses). dlib default 0.6
FACE_ENCODINGS_DIR=~/.familiar_ai/face_encodings   # where *.npy live
FACE_SAMPLES_DIR=~/.familiar_ai/face_samples       # where enrollment photos live
```

With the flag **off** (default), behavior is identical to before Phase G:
ピコ never asserts who a seen person is and uses the `unknown_person` label.

## How it behaves

- Recognition runs **only on `see()`** (when a camera frame already exists) — never
  per-frame/streaming, so it adds no idle cost.
- If `face_locations()` finds no face, nothing is added (no false "someone is here").
- If the best match distance is within `FACE_RECOGNITION_TOLERANCE`, a hedged hint
  with a `confidence` value is appended; otherwise nothing is added.
- ピコ is explicitly told this is only a hint and not to hard-assert identity from it
  alone — so a false positive becomes "this looks like カイニット, but…", not a
  confident misidentification.

## Privacy

Face encodings are plain numeric `.npy` arrays under `~/.familiar_ai/face_encodings/`
that you own and can delete at any time. Sample photos never leave the Pi, and the
encodings are not written to `observations.db`. Identity only ever enters memory as
ordinary `see()` observation text on **new** observations; past observations are
never rewritten.

## Tuning false positives / misses

- Misidentifying others as you → **lower** `FACE_RECOGNITION_TOLERANCE` (e.g. 0.5).
- Failing to recognize you → **raise** it slightly (e.g. 0.65) or add more/better
  sample photos and re-enroll.

## Swapping the backend (future)

The recognizer is isolated behind `pico_agent/adapters/face_recognition._get_recognizer()`.
To move off dlib (e.g. to InsightFace/ONNX, lighter on Pi5), replace that one function
to return a module exposing `load_image_file`, `face_locations`, `face_encodings`, and
`face_distance`; nothing else changes.
