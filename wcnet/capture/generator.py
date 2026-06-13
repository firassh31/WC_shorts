"""Option A — copyright-safe GENERATED goal Short renderer.

Builds a 9:16 animated highlight Short entirely from match *data* — no broadcast
footage, no broadcast audio — so there is nothing for Content ID to match.

Visuals (all original): a team-coloured animated background, a popping "GOAL!"
headline, the scorer + minute, a lower-third scoreboard with stylised team
badges, and confetti. Audio is an original synthesised music bed (or your own
royalty-free track dropped in ``assets/music/``).

Output is H.264/AAC 1080x1920, <60s, ready for YouTube Shorts.
"""

from __future__ import annotations

import colorsys
import hashlib
import logging
import math
import random
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..config import Settings

log = logging.getLogger("wcnet.capture.generator")

W, H = 1080, 1920
FPS = 30

# ── fonts ────────────────────────────────────────────────────────────────
_FONT_CANDIDATES = {
    "impact": ["C:/Windows/Fonts/impact.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "bold": ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/bahnschrift.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "regular": ["C:/Windows/Fonts/arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key in _font_cache:
        return _font_cache[key]
    for path in _FONT_CANDIDATES.get(kind, []):
        if Path(path).exists():
            f = ImageFont.truetype(path, size)
            _font_cache[key] = f
            return f
    f = ImageFont.load_default()
    _font_cache[key] = f
    return f


# ── team metadata (colours + abbreviations) ───────────────────────────────
_TEAM_META: dict[str, dict] = {
    "Argentina": {"abbr": "ARG", "primary": (108, 172, 228), "secondary": (255, 255, 255)},
    "France":    {"abbr": "FRA", "primary": (33, 45, 92),     "secondary": (206, 17, 38)},
    "Brazil":    {"abbr": "BRA", "primary": (254, 223, 0),    "secondary": (0, 151, 57)},
    "Croatia":   {"abbr": "CRO", "primary": (200, 16, 46),    "secondary": (255, 255, 255)},
    "Morocco":   {"abbr": "MAR", "primary": (193, 18, 31),    "secondary": (0, 98, 51)},
    "Portugal":  {"abbr": "POR", "primary": (255, 0, 0),      "secondary": (0, 102, 0)},
    "England":   {"abbr": "ENG", "primary": (255, 255, 255),  "secondary": (0, 38, 84)},
    "Spain":     {"abbr": "ESP", "primary": (198, 12, 48),    "secondary": (255, 196, 0)},
    "Germany":   {"abbr": "GER", "primary": (20, 20, 20),     "secondary": (221, 0, 0)},
    "Netherlands": {"abbr": "NED", "primary": (255, 99, 0),   "secondary": (255, 255, 255)},
}


def team_meta(name: str) -> dict:
    if name in _TEAM_META:
        return _TEAM_META[name]
    # Deterministic vivid colour from the name; abbr = first 3 letters.
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360.0, 0.62, 0.85)
    return {
        "abbr": "".join(c for c in name.upper() if c.isalpha())[:3] or "TBD",
        "primary": (int(r * 255), int(g * 255), int(b * 255)),
        "secondary": (255, 255, 255),
    }


# ── easing helpers ─────────────────────────────────────────────────────────
def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _seg(t: float, a: float, b: float) -> float:
    """Normalised 0..1 progress of t within [a, b]."""
    return _clamp01((t - a) / (b - a)) if b > a else (1.0 if t >= b else 0.0)


def _ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def _ease_out_back(t: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


@dataclass
class GoalSpec:
    competition: str       # e.g. "WORLD CUP"
    round_name: str        # e.g. "FINAL"
    home_abbr: str
    away_abbr: str
    home_score: int
    away_score: int
    scorer: str            # e.g. "MESSI"
    minute: int
    primary: tuple[int, int, int]
    secondary: tuple[int, int, int]
    home_colors: tuple[tuple[int, int, int], tuple[int, int, int]]
    away_colors: tuple[tuple[int, int, int], tuple[int, int, int]]


class GoalClipGenerator:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    # ── public API ─────────────────────────────────────────────────────────
    def render(
        self,
        *,
        competition: str,
        round_name: str,
        home_team: str,
        away_team: str,
        home_score: int,
        away_score: int,
        scorer: str,
        minute: int,
        scoring_team: str,
        out_path: Path,
        duration: float = 6.0,
        music_path: Path | None = None,
    ) -> Path:
        hm, am = team_meta(home_team), team_meta(away_team)
        accent = team_meta(scoring_team)
        spec = GoalSpec(
            competition=competition.upper(),
            round_name=round_name.upper(),
            home_abbr=hm["abbr"], away_abbr=am["abbr"],
            home_score=home_score, away_score=away_score,
            scorer=scorer.upper(), minute=minute,
            primary=accent["primary"], secondary=accent["secondary"],
            home_colors=(hm["primary"], hm["secondary"]),
            away_colors=(am["primary"], am["secondary"]),
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Audio first (so we know its real path before piping video).
        audio = music_path or self._resolve_music()
        if audio is None:
            audio = out_path.with_suffix(".wav")
            self._synth_music(audio, duration)

        self._encode(spec, duration, out_path, audio)
        log.info("Generated goal Short → %s", out_path)
        return out_path

    # ── music ───────────────────────────────────────────────────────────────
    def _resolve_music(self) -> Path | None:
        assets = Path("assets/music")
        if assets.is_dir():
            for f in sorted(assets.iterdir()):
                if f.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac"}:
                    log.info("Using royalty-free music: %s", f)
                    return f
        return None

    def _synth_music(self, path: Path, duration: float, sr: int = 44100) -> None:
        """Synthesise an original, royalty-free upbeat bed (arpeggio + kick)."""
        n = int(sr * duration)
        t = np.arange(n) / sr
        out = np.zeros(n, dtype=np.float64)

        def midi(m: float) -> float:
            return 440.0 * 2 ** ((m - 69) / 12.0)

        # I–V–vi–IV vibe; each chord spans an equal slice of the timeline.
        chords = [[60, 64, 67, 72], [55, 59, 62, 67],
                  [57, 60, 64, 69], [53, 57, 60, 65]]
        slice_len = duration / len(chords)
        note_len = 0.25  # eighth-ish notes

        for ci, chord in enumerate(chords):
            c_start = ci * slice_len
            # Arpeggio
            k = 0
            nt = c_start
            while nt < c_start + slice_len - 1e-6:
                note = chord[k % len(chord)]
                self._add_note(out, sr, nt, note_len, midi(note), 0.16)
                k += 1
                nt += note_len
            # Soft sustained pad (root + fifth)
            self._add_note(out, sr, c_start, slice_len, midi(chord[0]) / 2, 0.05)
            self._add_note(out, sr, c_start, slice_len, midi(chord[2]) / 2, 0.04)

        # Four-on-the-floor kick.
        beat = 0.0
        while beat < duration:
            self._add_kick(out, sr, beat, 0.12)
            beat += 0.5

        # Normalise + global fades.
        peak = np.max(np.abs(out)) or 1.0
        out = out / peak * 0.85
        fi = int(sr * 0.08)
        fo = int(sr * 0.6)
        out[:fi] *= np.linspace(0, 1, fi)
        out[-fo:] *= np.linspace(1, 0, fo)

        pcm = (out * 32767).astype(np.int16)
        with wave.open(str(path), "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())

    @staticmethod
    def _add_note(buf: np.ndarray, sr: int, start: float, dur: float,
                  freq: float, amp: float) -> None:
        i0 = int(start * sr)
        i1 = min(len(buf), i0 + int(dur * sr))
        if i1 <= i0:
            return
        tt = np.arange(i1 - i0) / sr
        env = np.exp(-3.5 * tt / dur)  # plucky decay
        wave_ = np.sin(2 * math.pi * freq * tt)
        wave_ += 0.3 * np.sin(2 * math.pi * 2 * freq * tt)  # a little shimmer
        buf[i0:i1] += amp * env * wave_

    @staticmethod
    def _add_kick(buf: np.ndarray, sr: int, start: float, dur: float) -> None:
        i0 = int(start * sr)
        i1 = min(len(buf), i0 + int(dur * sr))
        if i1 <= i0:
            return
        tt = np.arange(i1 - i0) / sr
        env = np.exp(-30 * tt)
        freq = 120 * np.exp(-18 * tt) + 45  # pitch drop
        buf[i0:i1] += 0.5 * env * np.sin(2 * math.pi * freq * tt)

    # ── background ───────────────────────────────────────────────────────────
    def _build_background(self, spec: GoalSpec) -> Image.Image:
        pr = np.array(spec.primary, dtype=np.float64)
        top = pr * 0.45
        bottom = np.array((8, 10, 16), dtype=np.float64)
        ramp = np.linspace(0, 1, H)[:, None]
        grad = (top[None, :] * (1 - ramp) + bottom[None, :] * ramp)
        img = np.repeat(grad[:, None, :], W, axis=1)

        # Radial glow behind the headline.
        cy, cx = int(H * 0.36), W // 2
        yy, xx = np.ogrid[:H, :W]
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        glow = np.clip(1 - dist / (W * 0.85), 0, 1) ** 2
        img += (pr * 0.6)[None, None, :] * glow[:, :, None]

        # Subtle diagonal sheen.
        sheen = (np.sin((xx + yy) / 120.0) * 6)
        img += sheen[:, :, None]

        return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")

    # ── per-frame helpers ────────────────────────────────────────────────────
    @staticmethod
    def _spaced_text(draw: ImageDraw.ImageDraw, xy, text, font, fill, spacing):
        x, y = xy
        widths = [draw.textlength(ch, font=font) for ch in text]
        total = sum(widths) + spacing * (len(text) - 1)
        cx = x - total / 2
        for ch, w in zip(text, widths):
            draw.text((cx, y), ch, font=font, fill=fill, anchor="lm")
            cx += w + spacing

    def _badge(self, base: Image.Image, center, colors, alpha: int) -> None:
        primary, secondary = colors
        r = 66
        cx, cy = center
        tile = Image.new("RGBA", (r * 2 + 12, r * 2 + 12), (0, 0, 0, 0))
        d = ImageDraw.Draw(tile)
        c = r + 6
        d.ellipse((c - r, c - r, c + r, c + r), fill=(*secondary, alpha))
        d.ellipse((c - r + 7, c - r + 7, c + r - 7, c + r - 7), fill=(*primary, alpha))
        base.alpha_composite(tile, (int(cx - c), int(cy - c)))

    # ── frame renderer ───────────────────────────────────────────────────────
    def _frame(self, bg: Image.Image, spec: GoalSpec, t: float, dur: float,
               particles) -> Image.Image:
        base = bg.copy().convert("RGBA")
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        white = (255, 255, 255)

        # Confetti (intensifies after the GOAL pop).
        burst = _seg(t, 0.6, 3.5)
        for p in particles:
            life = (t * p["spd"] + p["off"]) % 1.0
            py = int(H * 0.30 + life * H * 0.8)
            px = int(p["x"] + math.sin(life * 6 + p["off"] * 10) * 30)
            a = int(200 * burst * (1 - life))
            if a <= 0:
                continue
            s = p["sz"]
            d.rectangle((px, py, px + s, py + int(s * 1.8)), fill=(*p["col"], a))

        # Competition label.
        la = int(255 * _ease_out_cubic(_seg(t, 0.2, 0.9)))
        if la > 0:
            label = f"{spec.competition} • {spec.round_name}"
            self._spaced_text(d, (W / 2, 235), label, _font("bold", 46),
                              (*spec.secondary, la), 8)
            d.line((W / 2 - 230, 285, W / 2 + 230, 285), fill=(*white, la), width=3)

        base = Image.alpha_composite(base, overlay)

        # "GOAL!" — scale-pop then gentle breathing pulse.
        gp = _seg(t, 0.45, 1.15)
        if gp > 0:
            scale = _ease_out_back(gp) if gp < 1 else 1 + 0.03 * math.sin((t - 1.15) * 4)
            ga = int(255 * _clamp01(gp * 1.5))
            self._stamp_text(base, "GOAL!", _font("impact", 360),
                             (W / 2, H * 0.36), (*white, ga), scale,
                             shadow=(*[int(c * 0.3) for c in spec.primary], ga))

        # Scorer name + minute, sliding up.
        sp = _seg(t, 1.1, 1.8)
        if sp > 0:
            off = (1 - _ease_out_cubic(sp)) * 60
            sa = int(255 * _ease_out_cubic(sp))
            ov2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d2 = ImageDraw.Draw(ov2)
            d2.text((W / 2, H * 0.50 + off), spec.scorer, font=_font("bold", 110),
                    fill=(*white, sa), anchor="mm")
            # minute pill
            pill = f"{spec.minute}'"
            pf = _font("bold", 54)
            tw = d2.textlength(pill, font=pf)
            px0, py0 = W / 2 - tw / 2 - 34, H * 0.50 + off + 90
            d2.rounded_rectangle((px0, py0, px0 + tw + 68, py0 + 84), radius=42,
                                 fill=(*spec.primary, sa))
            d2.text((W / 2, py0 + 42), pill, font=pf, fill=(*white, sa), anchor="mm")
            base = Image.alpha_composite(base, ov2)

        # Lower-third scoreboard, sliding up.
        bp = _seg(t, 1.9, 2.6)
        if bp > 0:
            off = (1 - _ease_out_cubic(bp)) * 120
            ba = int(255 * _ease_out_cubic(bp))
            y0 = H * 0.74 + off
            bar = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            bd = ImageDraw.Draw(bar)
            bx0, bx1 = 70, W - 70
            bd.rounded_rectangle((bx0, y0, bx1, y0 + 250), radius=46,
                                 fill=(12, 14, 22, int(ba * 0.92)))
            bd.rounded_rectangle((bx0, y0, bx0 + 16, y0 + 250), radius=8,
                                 fill=(*spec.primary, ba))
            cyc = y0 + 125
            self._badge(bar, (bx0 + 115, cyc), spec.home_colors, ba)
            self._badge(bar, (bx1 - 115, cyc), spec.away_colors, ba)
            bd.text((bx0 + 220, cyc), spec.home_abbr, font=_font("bold", 60),
                    fill=(255, 255, 255, ba), anchor="lm")
            bd.text((bx1 - 220, cyc), spec.away_abbr, font=_font("bold", 60),
                    fill=(255, 255, 255, ba), anchor="rm")
            score = f"{spec.home_score} - {spec.away_score}"
            bd.text((W / 2, cyc), score, font=_font("impact", 96),
                    fill=(255, 255, 255, ba), anchor="mm")
            base = Image.alpha_composite(base, bar)

        # Global fade in / out.
        if t < 0.25:
            base = self._fade_black(base, 1 - t / 0.25)
        if t > dur - 0.45:
            base = self._fade_black(base, (t - (dur - 0.45)) / 0.45)

        return base.convert("RGB")

    def _stamp_text(self, base, text, font, center, fill, scale, shadow=None):
        # Render the text to its own tile, scale, then composite (smooth pop).
        pad = 60
        tmp = Image.new("RGBA", (1200, 520), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        if shadow:
            td.text((600 + 6, 260 + 8), text, font=font, fill=shadow, anchor="mm")
        td.text((600, 260), text, font=font, fill=fill, anchor="mm")
        if scale != 1.0:
            nw, nh = int(tmp.width * scale), int(tmp.height * scale)
            tmp = tmp.resize((max(1, nw), max(1, nh)), Image.LANCZOS)
        cx, cy = center
        base.alpha_composite(tmp, (int(cx - tmp.width / 2), int(cy - tmp.height / 2)))

    @staticmethod
    def _fade_black(base: Image.Image, amount: float) -> Image.Image:
        a = int(255 * _clamp01(amount))
        black = Image.new("RGBA", base.size, (0, 0, 0, a))
        return Image.alpha_composite(base, black)

    # ── encode ───────────────────────────────────────────────────────────────
    def _encode(self, spec: GoalSpec, duration: float, out_path: Path,
                audio: Path) -> None:
        bg = self._build_background(spec)
        rng = random.Random(spec.minute * 7 + spec.home_score + spec.away_score)
        cols = [spec.secondary, spec.primary,
                spec.home_colors[0], spec.away_colors[0]]
        particles = [{
            "x": rng.randint(40, W - 40),
            "spd": rng.uniform(0.18, 0.5),
            "off": rng.random(),
            "sz": rng.randint(10, 22),
            "col": rng.choice(cols),
        } for _ in range(70)]

        n_frames = int(duration * FPS)
        cmd = [
            self._s.ffmpeg_binary, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
            "-r", str(FPS), "-i", "pipe:0",
            "-i", str(audio),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-shortest", "-movflags", "+faststart",
            str(out_path),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        assert proc.stdin is not None
        try:
            for i in range(n_frames):
                frame = self._frame(bg, spec, i / FPS, duration, particles)
                proc.stdin.write(frame.tobytes())
        finally:
            proc.stdin.close()
            rc = proc.wait()
        if rc != 0:
            raise subprocess.SubprocessError(f"ffmpeg encode failed ({rc})")
