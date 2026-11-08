# ytgrab

Lightweight YouTube transcript + metadata grabber. Give it a **video**, **channel**,
**playlist**, or **search** link (or a text file full of them); it saves each video's
transcript and metadata to a folder you choose.

- No video/audio download, no ffmpeg. About 60–80 MB RAM, under 1 s CPU per video.
- One video at a time, with a pause between them (gentle on your box and on YouTube's rate limits).
- Resumable: videos already saved are skipped, so re-running with a bigger `-n` continues.

## Setup (once)

Needs Linux (tested on Ubuntu 22.04) with Python 3.10+ and git.

```bash
git clone https://github.com/SamNowak/ytgrab.git
cd ytgrab && ./install.sh
```

Optional, to type `ytgrab` from anywhere (run inside the `ytgrab` folder):

```bash
sudo ln -sf "$PWD/ytgrab" /usr/local/bin/ytgrab
```

## Usage

Interactive (it asks for the link or links file, folder, and how many videos):

```bash
./ytgrab
```

Or pass everything on the command line:

```bash
./ytgrab "https://www.youtube.com/watch?v=jNQXAC9IVRw" -o ~/transcripts
./ytgrab "https://www.youtube.com/@TED" -n 25 -o ~/transcripts
./ytgrab "https://www.youtube.com/playlist?list=PL..." -n all -o ~/transcripts
```

Several links at once also work: `./ytgrab URL1 URL2 URL3 -n 5 -o DIR`.

### A file of links

Put one link per line in a text file (see [links.example.txt](links.example.txt)). Blank lines
and lines starting with `#` are skipped, duplicates are removed, and files saved from Windows
Notepad work too. Then:

```bash
./ytgrab -f links.txt -n 5 -o ~/transcripts
```

`./ytgrab links.txt ...` works too, and so does typing the file path at the interactive prompt.
`-n` applies to each channel, playlist, and search in the file.

### YouTube searches

Search on YouTube in your browser and copy the address bar URL
(`https://www.youtube.com/results?search_query=funny+cat+videos`). Any filters you picked there,
such as sort by upload date, are kept. Or skip the URL and write `search: funny cat videos`.
Looser forms like `youtube.com/search="Funny cat videos"` are understood too.
`-n` picks the top N results. A search never runs out, so `-n all` means the top 100.

| Option | Meaning |
|---|---|
| `-f FILE` | Text file with one link per line (can be given more than once) |
| `-o DIR` | Output folder (default `$YTGRAB_DIR` or `~/yt_transcripts`) |
| `-n N` | How many videos per channel/playlist/search (`all` = every video; default 10) |
| `-l en,es` | Preferred transcript languages, in order. Falls back to any available language |
| `-t` | Timestamped lines (`[00:01:23] text`) instead of paragraphs |
| `-s 1.5` | Seconds to pause between videos |
| `--flat` | Don't create a channel/playlist subfolder |
| `--overwrite` | Re-grab videos that were already saved |
| `--cookies FILE` | cookies.txt, for age-restricted or members-only videos |
| `-v` | Show yt-dlp's messages (for debugging) |

Channel links get the newest uploads from the Videos tab. For Shorts or live streams,
pass the tab directly, e.g. `https://www.youtube.com/@name/shorts`.
A `watch?v=...&list=...` link grabs just that one video.

## Output

```
<output>/
  TED/                                              <- channel name, playlist title, or "Search - <words>"
    2026-11-02_How to Make Parenting Less Lonely ..._[Gswo90kocHU].txt
    2026-11-02_How to Make Parenting Less Lonely ..._[Gswo90kocHU].json
```

- **.txt**: a short header (title, channel, URL, date, duration, language), then the transcript.
- **.json**: id, title, url, channel, channel id/url, subscriber count, upload date, duration,
  views, likes, comments, categories, tags, description, chapters, live status, thumbnail,
  plus transcript info (language, auto-generated or not, word count) and where/when it was grabbed.

A video with no captions gets only the .json, shown as `nosub`. It is retried on the next run.

## Troubleshooting

- **`block ... (rate-limited by YouTube)` / `STOPPED`**: YouTube is limiting transcript
  requests from your IP (HTTP 429). ytgrab waits 30 s after each block and stops after 3 in a
  row so it doesn't make things worse. Metadata is still saved. Re-run the same command later
  (blocks usually lift within a few hours) and it continues where it left off. Use a bigger
  pause such as `-s 5` for big jobs.
- **"Sign in to confirm you're not a bot"**: same cause. Wait, or pass `--cookies`.
- **Things that used to work stop working**: update yt-dlp with `./install.sh`.
  YouTube changes often.
- yt-dlp warns that Python 3.10 is deprecated. That's harmless for now (warnings are hidden).
  If a future update stops working on Ubuntu 22.04's Python 3.10, install a newer Python
  and rebuild the venv:
  `sudo apt install python3.11 python3.11-venv && rm -rf .venv && python3.11 -m venv .venv && ./install.sh`

## License

MIT. See [LICENSE](LICENSE).
