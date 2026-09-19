# tv-rip-identifier

> Written by Claude (Opus 4.8), Anthropic's AI model, in collaboration with the repo owner.

Identify unlabeled TV episode rips by their **subtitle dialogue** and rename them to
`SxxEyy - Title.mkv`.

When you rip a DVD or Blu-ray box set with MakeMKV, you get files with meaningless
names like `Show- Season 1- Disc 1_t00.mkv`. This tool figures out which episode each
one actually is — by reading its subtitles and matching the dialogue against a folder
of correctly-labeled reference subtitles — and renames it accordingly. Titles it can't
identify (extras, bloopers, a "play all") are moved aside.

It's **content-based**, so it doesn't trust disc/title order: it catches discs ripped
out of order and ignores non-episode titles instead of misnumbering them.

## How it works

For each `Season N` folder, it builds a reference index from your `Subtitles/` folder,
then for every rip:

1. Extracts an embedded **text** subtitle track if the rip has one (fast).
2. Otherwise **OCRs** the image subtitle (Blu-ray PGS / DVD VobSub) by rendering each
   subtitle onto a black canvas — crisp white-on-black, no video behind it — de-duping
   repeated frames and reading them with tesseract over a short sample.
3. Chops that dialogue into overlapping 6-word phrases and counts how many appear
   verbatim in each candidate episode's reference subtitle.
4. The episode with by far the most matches wins. A match is accepted when it clears
   `--min-hits` (default 8) and either beats the runner-up 2× or is decisive on its own
   (`--strong-hits`); OCR matches must also clear a hits/words ratio (`--min-ratio`).
   Anything short of that is treated as unidentified.

Transcripts are cached under `<show>/.idcache/`, so re-runs and the `--apply` pass
never repeat the slow OCR.

**Two-part episodes.** Box sets often author a double-length premiere or finale as a
single title (Part 1 + Part 2 in one file). The tool flags any rip that runs far longer
than the season's typical episode, samples a second window near its end to identify the
later part, and names it as a span — e.g. `S02E01-E02 - Title.mkv`, which Plex reads
natively. Both episodes count as covered, so a two-parter isn't reported as a missing
episode.

## Requirements

- **ffmpeg + ffprobe** on your `PATH` — always required.
- **tesseract + pillow + pytesseract** — only for rips whose subtitles are images
  (Blu-ray/DVD with no embedded text track).

```bash
# macOS
brew install ffmpeg tesseract
python3 -m pip install --break-system-packages pillow pytesseract

# Debian/Ubuntu
sudo apt install ffmpeg tesseract-ocr
pip3 install pillow pytesseract
```

## Folder layout

```
Show/
├── Season 1/
│   ├── Show- Season 1- Disc 1_t00.mkv
│   └── ...
├── Season 2/
│   └── ...
└── Subtitles/
    ├── Show - season 1/    Show - 1x01 - Pilot.srt, Show - 1x02 - ....srt, ...
    └── Show - season 2/    Show - 2x01 - ....srt, ...
```

- Rip files live in `Season N` subfolders (the season number is read from the folder name).
- Reference subtitles go under `Subtitles/`, grouped so each file's season is discoverable
  from its folder name **or** its own filename.
- Reference filenames must contain the episode as `1x01` or `S01E01`. Everything after the
  title (release tags like `.720p HDTV.GROUP`) is stripped automatically.
- Reference subs are plain-text `.srt`. A good source is
  [tvsubtitles.net](https://www.tvsubtitles.net/) — download the season pack for your show.

## Usage

```bash
# dry-run: identify everything, write id_report.md, rename nothing
python3 identify_and_rename.py --show-root "/path/to/Show"

# review id_report.md, then actually rename + move extras
python3 identify_and_rename.py --show-root "/path/to/Show" --apply
```

Run it from inside a show folder and `--show-root` defaults to the current directory.
The dry-run writes `id_report.md` (every `raw name → SxxEyy - Title.mkv`, plus anything
unidentified) and `id_results.json`. **Always review the dry-run before `--apply`.**

### Options

| flag | default | meaning |
|------|---------|---------|
| `--show-root PATH` | `.` | the show folder |
| `--subs PATH` | `<show-root>/Subtitles` | reference subtitles folder |
| `--apply` | off | perform renames/moves (otherwise dry-run) |
| `--season N` | all | only process this season number |
| `--no-ocr` | off | skip image-subtitle OCR (text-track rips only) |
| `--span-ratio F` | `1.55` | a rip this many × the season's median runtime is treated as a possible two-parter |
| `--start N` | `180` | OCR sample start, seconds into the episode |
| `--dur N` | `240` | OCR sample length, seconds |
| `--fps F` | `1.0` | OCR sample frame rate |
| `--min-hits N` | `8` | minimum dialogue hits to accept a match |
| `--min-ratio F` | `0.12` | minimum hits / OCR-words ratio to accept an OCR match (guards against garbage OCR) |
| `--strong-hits N` | `40` | hit count that confirms a match even if a neighbor shares a lot of dialogue |

## Reading the output

Each line ends with `(best/runner-up)` — the number of 6-word phrases the rip shared
with its best-matching episode vs. the second-best. `(727/4)` is an unmistakable match;
`(150/130)` would mean the dialogue fit two episodes almost equally and should be checked.
OCR'd shows show smaller numbers than text-track shows, but the ratio is what matters.

## Notes & limitations

- It matches **within the season** given by the folder name, which keeps things fast and
  unambiguous. Keep your `Season N` folders and `Subtitles/` set in place.
- The release-tag cleaner has a list of scene-group names (`GROUPS` near the top of the
  script). If your subtitle pack uses a group it doesn't know, a title may keep a stray
  `.TAG` suffix — add the group name to that set.
- OCR quality varies with the source; stylized on-screen text can lower it. The
  best/runner-up ratio still separates episodes reliably in practice, but skim the report.
- Nothing is deleted. `--apply` only renames files and moves unidentified ones into an
  `Extras/` subfolder.

## License

MIT — see [LICENSE](LICENSE). No warranty; review the dry-run before applying.
