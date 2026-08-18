# Security and privacy

## Data handling

The scripts process local text, image, audio and video files explicitly supplied through command-line arguments. They do not include analytics, account login, browser-cookie access or media-upload code.

`scripts/prepare_env.py` uses pip to install the packages required for local rendering. Rendering and validation invoke local Python, FFmpeg and FFprobe processes.

## Before publishing your own project

Do not commit:

- `.env` files, API keys, tokens or credentials
- TTS model weights or virtual environments
- private scripts, source images, narration audio or generated videos
- absolute paths containing local user or organization names

The repository `.gitignore` excludes common secret, model, cache and generated-media files, but it is not a substitute for reviewing `git status` before every push.

## Reporting a vulnerability

Please open a GitHub issue containing a minimal reproduction. Do not include real credentials, private media or personal information in the report.
