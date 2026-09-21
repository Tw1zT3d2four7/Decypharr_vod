import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from django.db import transaction
from django.utils import timezone
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.urls import path

from apps.m3u.models import M3UAccount
from apps.vod.models import (
    M3UMovieRelation,
    M3UEpisodeRelation,
    M3USeriesRelation,
    M3UVODCategoryRelation,
)
from apps.vod.models import VODCategory, Movie, Series, Episode, VODLogo

LOG = logging.getLogger("decypharr_vod")
PLUGIN_KEY = "decypharr_vod"
ACCOUNT_NAME = "Decypharr VOD"
SOURCE_ROOT = "/mnt/decypharr/__all__"
LIBRARY_ROOT = "/mnt/decypharr_vods"
STATE_DIR = "/data/plugins/decypharr_vod"
CACHE_DIR = os.path.join(STATE_DIR, "metadata")
SECRET_FILE = os.path.join(STATE_DIR, ".secret")
MARKER = "decypharr_vod"
PREFIX = "decypharr-"
ROUTE_INSTALLED = False
PATCHED = False
SCAN_LOCK = threading.Lock()

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".wmv", ".flv", ".strm"}
# ================================================================
# Canonical Arr-style title parser
#
# Principle:
#   1. Identify the structural TV marker first.
#   2. Everything before SxxExx / NxN is the series title.
#   3. Everything after the episode marker is episode title ONLY until
#      the first release/technical boundary.
#   4. Movie titles use a year as a structural boundary when available.
#   5. Technical tokens are boundaries, never globally deleted from
#      arbitrary title text.
#
# This deliberately avoids the old "strip every known word" approach.
# ================================================================

EP_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:S(?P<s>\d{1,3})[ ._-]*E(?P<e>\d{1,3})(?:[ ._-]*E\d{1,3})*"
    r"|(?P<xs>\d{1,3})x(?P<xe>\d{1,3}))"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)

SEASON_RE = re.compile(
    r"(?ix)"
    r"(?<![A-Za-z0-9])"
    r"(?:Season|Saison|Series|Stagione|Sezon)"
    r"[ ._-]*"
    r"(?P<s>\d{1,3})"
    r"(?![A-Za-z0-9])"
)

YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

# These are STRUCTURAL RELEASE BOUNDARIES.
# They are not globally removed from titles.
ARR_RELEASE_BOUNDARY_RE = re.compile(
    r"(?ix)"
    r"(?:"
    r"4320p|2160p|1440p|1080p|1080i|720p|576p|576i|480p|480i|360p|"
    r"8k|4k|uhd|"
    r"web[ ._-]?dl|web[ ._-]?rip|webrip|webdl|"
    r"bluray|blu[ ._-]?ray|brrip|br[ ._-]?rip|"
    r"hdtv|pdtv|dsr|dvdrip|dvdscr|remux|"
    r"x264|x265|h264|h265|hevc|av1|vp9|vp8|"
    r"aac|ac3|eac3|dts(?:[ ._-]?hd)?|ddp|truehd|atmos|"
    r"avc|hdr10[+]?|hdr|dolby[ ._-]?vision|"
    r"10bit|8bit|hi10p|"
    r"netflix|nf|amzn|amazon|hulu|disney(?:[ ._-]?plus)?|"
    r"appletv|apple[ ._-]?tv|hbo|"
    r"criterion|imax|"
    r"proper|repack|remastered|extended|unrated|theatrical|"
    r"director(?:s)?[ ._-]?cut|"
    r"multi|multilang|multilingual|dubbed|dual[ ._-]?audio|"
    r"complete|collection|season[ ._-]*pack|"
    r"10bit|12bit|sdr|hybrid|"
    r"(?<!\d)(?:5[ ._-]+1|7[ ._-]+1)(?!\d)"
    r")"
    r"(?![A-Za-z0-9])"
)

# Release groups are separate from the title in Arr parsing.
# Requiring whitespace around the separator prevents:
#   Spider-Man
#   9-1-1
# from being damaged.
TRAILING_GROUP_RE = re.compile(
    r"(?i)\s+-\s+[A-Za-z0-9][A-Za-z0-9._-]{1,40}\s*$"
)


def _release_normalize(value):
    value = str(value or "").strip()

    # Only remove an actual known media-file extension.
    # Never use Path.stem here: release titles can legitimately end
    # in ".Day", ".One", ".Part", etc.
    suffix = Path(value).suffix.lower()
    if suffix in VIDEO_EXTS:
        value = value[:-len(suffix)]

    value = value.replace("_", " ")
    value = value.replace(".", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()

def _strip_release_group(value):
    value = str(value or "").strip()

    # Only remove a conventional spaced release-group separator.
    value = TRAILING_GROUP_RE.sub("", value)

    # Bracketed release groups at the very end are also safe when they
    # look like a single group token, e.g. [YTS], [RARBG].
    value = re.sub(
        r"(?i)\s+\[[A-Za-z0-9][A-Za-z0-9._-]{1,30}\]\s*$",
        "",
        value,
    )

    return value.strip()


def _boundary_position(value):
    """
    Return the first technical/release boundary.

    Boundary matching is deliberately allowed at position zero.
    This is essential for:
        S08E01.1080p.WEB-DL
        S01E01.2160p
        S02E03.HEVC
    where the episode has no title.
    """
    value = str(value or "")

    best = None

    for match in ARR_RELEASE_BOUNDARY_RE.finditer(value):
        pos = match.start()

        # A release token must begin a token. Since the regex itself
        # enforces this, simply accept it.
        if best is None or pos < best:
            best = pos

    return best


def _clean_title(value):
    value = str(value or "").strip()

    value = re.sub(r"^[\s._\-–—]+", "", value)
    value = re.sub(r"[\s._\-–—]+$", "", value)

    # Remove only empty release punctuation.
    value = re.sub(r"\s+", " ", value)

    # Normalize Unicode dash variants but preserve meaningful hyphens.
    value = value.replace("–", "-").replace("—", "-")

    return value.strip()


def _parse_episode_structure(value):
    """
    Parse the first genuine TV episode marker.

    Returns:
        (match, season, episode)
    """
    value = str(value or "")

    candidates = []

    m = EP_RE.search(value)
    if m:
        season = m.group("s") or m.group("xs")
        episode = m.group("e") or m.group("xe")
        candidates.append((m, int(season), int(episode)))

    if not candidates:
        return None

    return min(candidates, key=lambda x: x[0].start())


def _parse_release(value):
    """
    Canonical parser shared by movie/TV title extraction.

    Returns:
        {
            "title": ...,
            "year": ...,
            "season": ...,
            "episode": ...,
            "episode_title": ...,
            "is_tv": bool,
        }
    """
    raw = _release_normalize(value)
    raw = _strip_release_group(raw)

    result = {
        "title": "",
        "year": None,
        "season": None,
        "episode": None,
        "episode_title": "",
        "is_tv": False,
    }

    if not raw:
        return result

    # ------------------------------------------------------------
    # TV: structural episode marker wins.
    # ------------------------------------------------------------
    tv = _parse_episode_structure(raw)

    if tv:
        match, season, episode = tv

        result["is_tv"] = True
        result["season"] = season
        result["episode"] = episode

        series_part = raw[:match.start()]
        after = raw[match.end():]

        # The series title is BEFORE the episode marker.
        series_part = _clean_title(series_part)

        # A release may contain a year immediately before the episode
        # marker. Preserve legitimate numeric titles:
        #
        #   1923 2022 S01E01
        #       -> title 1923, year 2022
        #
        #   The.Office.2005.S01E01
        #       -> title The Office, year 2005
        years = list(YEAR_RE.finditer(series_part))

        if years:
            last_year = years[-1]
            before_year = _clean_title(series_part[:last_year.start()])
            after_year = _clean_title(series_part[last_year.end():])

            if before_year:
                # A normal title followed by a release/series year.
                if not after_year:
                    series_part = before_year
                    result["year"] = int(last_year.group(1))
            else:
                # Numeric title. Keep it unless another year follows.
                if len(years) >= 2:
                    first = years[0]
                    second = years[1]

                    if first.start() == 0:
                        between = _clean_title(
                            series_part[first.end():second.start()]
                        )

                        if not between:
                            series_part = _clean_title(
                                series_part[:first.end()]
                            )
                            result["year"] = int(second.group(1))

        result["title"] = series_part

        # --------------------------------------------------------
        # Episode title is AFTER SxxExx and BEFORE the first
        # technical/release boundary.
        # --------------------------------------------------------
        after = re.sub(r"^[\s._\-–—]+", "", after)

        boundary = _boundary_position(after)

        if boundary is not None:
            episode_part = after[:boundary]
        else:
            episode_part = after

        episode_part = _strip_release_group(episode_part)
        episode_part = _clean_title(episode_part)

        # A standalone year after SxxExx/NxN is metadata, not an episode title.
        if re.fullmatch(r"(?:19|20)\d{2}", episode_part):
            episode_part = ""

        # A pure technical suffix means NO episode title.
        if ARR_RELEASE_BOUNDARY_RE.fullmatch(episode_part):
            episode_part = ""

        result["episode_title"] = episode_part
        return result

    # ------------------------------------------------------------
    # Season-only release.
    # ------------------------------------------------------------
    season_match = SEASON_RE.search(raw)

    if season_match:
        result["is_tv"] = True
        result["season"] = int(season_match.group("s"))

        title = _clean_title(raw[:season_match.start()])

        years = list(YEAR_RE.finditer(title))
        if years and years[-1].end() == len(title):
            y = years[-1]
            before = _clean_title(title[:y.start()])
            if before:
                title = before
                result["year"] = int(y.group(1))

        result["title"] = title
        return result

    # ------------------------------------------------------------
    # Movie.
    #
    # Year is structural when it occurs in the release title.
    # Numeric-only titles such as 1923 / 2012 remain valid titles.
    # ------------------------------------------------------------
    years = list(YEAR_RE.finditer(raw))

    if years:
        first = years[0]

        before = _clean_title(raw[:first.start()])
        after = _clean_title(raw[first.end():])

        if before:
            # Standard:
            #   Movie.Title.2024.1080p.WEB-DL
            result["title"] = before
            result["year"] = int(first.group(1))
        else:
            # Numeric title:
            #   1923.2022.1080p
            #   2012.2009.REMASTERED
            if len(years) >= 2:
                second = years[1]
                between = _clean_title(
                    raw[first.end():second.start()]
                )

                if not between:
                    result["title"] = first.group(1)
                    result["year"] = int(second.group(1))
                else:
                    result["title"] = first.group(1)
            else:
                # A release consisting of the year alone is itself a
                # legitimate movie title.
                if not after or _boundary_position(after) == 0:
                    result["title"] = first.group(1)
                    result["year"] = None
                else:
                    result["title"] = first.group(1)
                    result["year"] = None

            return result

        return result

    # No year: stop at the first technical release boundary.
    boundary = _boundary_position(raw)

    if boundary is not None:
        result["title"] = _clean_title(raw[:boundary])
    else:
        result["title"] = _clean_title(raw)

    return result


def _norm(s):
    """
    Matching-only normalization.

    This is NOT title parsing. It exists only for database comparison.
    """
    s = str(s or "")
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()


def _title(path):
    info = _parse_release(Path(path).stem)
    return info["title"] or _clean_title(Path(path).stem)


def _year(path):
    info = _parse_release(str(path))
    return info["year"]


def _episode(path):
    info = _parse_release(Path(path).stem)

    if info["season"] is None or info["episode"] is None:
        return None

    return info["season"], info["episode"]


def _episode_title(path):
    return _parse_release(Path(path).stem)["episode_title"]


def _release_title(value):
    return _parse_release(value)["title"]







def _safe(s, fallback="Unknown"):
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", str(s or "")).strip(" .")
    return (s[:230] or fallback)


def _fingerprint(path):
    return hashlib.sha256(os.path.realpath(path).encode("utf-8", "ignore")).hexdigest()


def _json_cache(key, value=None, ttl_days=30):
    os.makedirs(CACHE_DIR, exist_ok=True)
    fn = os.path.join(CACHE_DIR, hashlib.sha256(key.encode()).hexdigest() + ".json")
    if value is not None:
        tmp = fn + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "value": value}, f, ensure_ascii=False)
        os.replace(tmp, fn)
        return value
    try:
        with open(fn, encoding="utf-8") as f:
            x = json.load(f)
        if time.time() - float(x.get("time", 0)) <= ttl_days * 86400:
            return x.get("value")
    except Exception:
        pass
    return None


def _tmdb(api_key, endpoint, params):
    q = dict(params)
    q["api_key"] = api_key
    url = "https://api.themoviedb.org/3/" + endpoint.lstrip("/") + "?" + urllib.parse.urlencode(q)
    key = url.split("&api_key=", 1)[0]
    cached = _json_cache(key)
    if cached is not None:
        return cached
    req = urllib.request.Request(url, headers={"User-Agent": "Decypharr-VOD/0.2.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            return _json_cache(key, data)
        except Exception as e:
            if attempt == 2:
                LOG.warning("TMDB request failed: %s", e)
            else:
                time.sleep(1 + attempt)
    return None


def _tmdb_movie(api_key, title, year):
    if not api_key:
        return None
    q = _norm(title)
    cached = _json_cache("movie-search|%s|%s" % (q, year or ""))
    if cached is None:
        cached = _tmdb(api_key, "search/movie", {"query": title, **({"year": year} if year else {})}) or {}
        _json_cache("movie-search|%s|%s" % (q, year or ""), cached)
    results = cached.get("results", [])
    if not results:
        return None
    best = sorted(results, key=lambda x: (0 if _norm(x.get("title")) == q else 1, abs((x.get("release_date", "")[:4] and int(x["release_date"][:4]) or 0) - (year or 0)) if x.get("release_date") and year else 9999))[0]
    return _tmdb(api_key, "movie/%s" % best["id"], {"append_to_response": "credits,external_ids"}) or best


def _tmdb_tv(api_key, title, year):
    if not api_key:
        return None
    q = _norm(title)
    key = "tv-search|%s|%s" % (q, year or "")
    cached = _json_cache(key)
    if cached is None:
        cached = _tmdb(api_key, "search/tv", {"query": title}) or {}
        _json_cache(key, cached)
    results = cached.get("results", [])
    if not results:
        return None
    def score(x):
        y = (x.get("first_air_date") or "")[:4]
        yd = abs(int(y) - year) if y.isdigit() and year else 9999
        return (0 if _norm(x.get("name")) == q else 1, yd)
    best = sorted(results, key=score)[0]
    return _tmdb(api_key, "tv/%s" % best["id"], {"append_to_response": "credits,external_ids"}) or best


def _ffprobe(path, exe):
    if not shutil.which(exe) and not os.path.exists(exe):
        return {"error": "ffprobe not found"}
    try:
        p = subprocess.run([exe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path], capture_output=True, text=True, timeout=90)
        if p.returncode:
            return {"error": p.stderr[-500:]}
        raw = json.loads(p.stdout or "{}")
        streams = raw.get("streams", [])
        videos, audios, subs = [], [], []
        for s in streams:
            x = {"index": s.get("index"), "codec": s.get("codec_name"), "language": (s.get("tags") or {}).get("language"), "title": (s.get("tags") or {}).get("title")}
            if s.get("codec_type") == "video":
                x.update({"width": s.get("width"), "height": s.get("height"), "fps": s.get("r_frame_rate"), "pix_fmt": s.get("pix_fmt"), "hdr": s.get("color_transfer"), "profile": s.get("profile")})
                videos.append(x)
            elif s.get("codec_type") == "audio":
                x.update({"channels": s.get("channels"), "sample_rate": s.get("sample_rate"), "bitrate": s.get("bit_rate")})
                audios.append(x)
            elif s.get("codec_type") == "subtitle":
                subs.append(x)
        return {"duration": float((raw.get("format") or {}).get("duration") or 0), "bitrate": int((raw.get("format") or {}).get("bit_rate") or 0), "format": (raw.get("format") or {}).get("format_name"), "video": videos, "audio": audios, "subtitles": subs}
    except Exception as e:
        return {"error": str(e)}


def _genres(data):
    return ", ".join(g.get("name") for g in (data or {}).get("genres", []) if g.get("name"))


def _logo(url, name):
    if not url:
        return None
    try:
        obj, _ = VODLogo.objects.get_or_create(name=name, defaults={"url": url})
        if getattr(obj, "url", None) != url:
            obj.url = url
            obj.save(update_fields=["url"])
        return obj
    except Exception:
        return None


def _get_secret():
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(SECRET_FILE) as f:
            s = f.read().strip()
        if s:
            return s
    except Exception:
        pass
    import secrets
    s = secrets.token_urlsafe(32)
    tmp = SECRET_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(s)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SECRET_FILE)
    return s


def _play(kind, rel, ext):
    return "http://127.0.0.1:9191/decypharr-vod/%s/%s/%s.%s" % (_get_secret(), kind, rel, ext.lstrip("."))


def _stream_file(request, kind, rid):
    Model = M3UMovieRelation if kind == "movie" else M3UEpisodeRelation
    try:
        rel = Model.objects.select_related("m3u_account").get(id=int(rid), m3u_account__name=ACCOUNT_NAME)
    except Exception:
        raise Http404
    path = (rel.custom_properties or {}).get("decypharr_path")
    if not path:
        raise Http404
    path = os.path.realpath(path)
    if not os.path.isfile(path):
        raise Http404
    size = os.path.getsize(path)
    rng = request.headers.get("Range")
    if not rng:
        resp = FileResponse(open(path, "rb"), content_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
        resp["Content-Length"] = str(size)
        return resp
    m = re.match(r"bytes=(\d*)-(\d*)", rng)
    if not m:
        return HttpResponse(status=416)
    start = int(m.group(1) or 0)
    end = int(m.group(2) or size - 1)
    end = min(end, size - 1)
    if start > end or start >= size:
        return HttpResponse(status=416)
    length = end - start + 1
    f = open(path, "rb")
    f.seek(start)
    def chunks():
        left = length
        try:
            while left:
                b = f.read(min(1024 * 1024, left))
                if not b:
                    break
                left -= len(b)
                yield b
        finally:
            f.close()
    r = StreamingHttpResponse(chunks(), status=206, content_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
    r["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    r["Content-Length"] = str(length)
    r["Accept-Ranges"] = "bytes"
    return r


def _install_route():
    global ROUTE_INSTALLED
    if ROUTE_INSTALLED:
        return
    try:
        import dispatcharr.urls as urls
        token = _get_secret()
        def movie_view(request, token_in, rid, extension):
            if token_in != token: raise Http404
            return _stream_file(request, "movie", rid)
        def episode_view(request, token_in, rid, extension):
            if token_in != token: raise Http404
            return _stream_file(request, "episode", rid)
        urls.urlpatterns.insert(0, path("decypharr-vod/<str:token_in>/movie/<int:rid>.<str:extension>", movie_view))
        urls.urlpatterns.insert(0, path("decypharr-vod/<str:token_in>/episode/<int:rid>.<str:extension>", episode_view))
        ROUTE_INSTALLED = True
    except Exception as e:
        LOG.exception("Failed to install Decypharr VOD routes: %s", e)


def _patch_relations():
    global PATCHED
    if PATCHED:
        return
    def movie_url(self, profile=None):
        if getattr(self.m3u_account, "name", "") == ACCOUNT_NAME:
            return _play("movie", self.id, self.container_extension or "mp4")
        return self._decypharr_original_url(profile)
    def episode_url(self, profile=None):
        if getattr(self.m3u_account, "name", "") == ACCOUNT_NAME:
            return _play("episode", self.id, self.container_extension or "mp4")
        return self._decypharr_original_url(profile)
    if not hasattr(M3UMovieRelation, "_decypharr_original_url"):
        M3UMovieRelation._decypharr_original_url = M3UMovieRelation.get_stream_url
        M3UMovieRelation.get_stream_url = movie_url
    if not hasattr(M3UEpisodeRelation, "_decypharr_original_url"):
        M3UEpisodeRelation._decypharr_original_url = M3UEpisodeRelation.get_stream_url
        M3UEpisodeRelation.get_stream_url = episode_url
    PATCHED = True


def _account():
    a = M3UAccount.objects.filter(name=ACCOUNT_NAME).first()
    if a:
        return a
    a = M3UAccount.objects.create(name=ACCOUNT_NAME, account_type=M3UAccount.Types.XC, server_url="http://127.0.0.1", username="decypharr", password="disabled", file_path=SOURCE_ROOT, is_active=False, priority=10000, max_streams=0, user_agent="Dispatcharr-Decypharr-VOD", custom_properties={MARKER: True})
    return a


def _category(account, name, kind):
    c, _ = VODCategory.objects.get_or_create(name=name, category_type=kind)
    M3UVODCategoryRelation.objects.get_or_create(m3u_account=account, category=c, defaults={"enabled": True, "custom_properties": {MARKER: True}})
    return c


def _find_movie(name, year, tmdb_id=None):
    qs = Movie.objects.all()
    if tmdb_id:
        x = qs.filter(tmdb_id=tmdb_id).first()
        if x: return x
    n = _norm(name)
    exact = [x for x in qs if _norm(x.name) == n]
    same = [x for x in exact if year and x.year == year]
    return (same or exact or [None])[0]


def _series_match_key(value):
    """Normalize series names aggressively enough to match common
    filename variations such as Gabby's Dollhouse / Gabbys Dollhouse.
    """
    value = str(value or "")
    value = re.sub(r"(?i)(?<=\w)['’]s\b", "s", value)
    value = value.replace("'", "").replace("’", "")
    value = re.sub(r"[\._]+", " ", value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip().casefold()
    return value


def _clean_series_title(value):
    """Turn a release-style filename/directory into a usable series title."""
    value = os.path.basename(str(value or ""))

    # Remove extension.
    value = os.path.splitext(value)[0]

    # Remove episode marker first, including multi-episode forms
    # such as S03E75E76.
    value = EP_RE.sub(" ", value)

    # Remove year and common release/codec noise.
    value = YEAR_RE.sub(" ", value)
    value = NOISE_RE.sub(" ", value)

    value = re.sub(
        r"(?i)\b(?:"
        r"netflix|amzn|amazon|hulu|disney|disneyplus|"
        r"web[- .]?dl|web[- .]?rip|bluray|brrip|hdtv|"
        r"proper|repack|complete|collection|"
        r"season|specials?|extras?"
        r")\b",
        " ",
        value,
    )

    value = re.sub(r"[\._]+", " ", value)
    value = re.sub(r"[\[\{\(][^\]\}\)]*[\]\}\)]", " ", value)
    value = re.sub(r"[-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()

    return value


def _series_title_from_path(path):
    p = Path(path)

    try:
        release = _release_root(p, SOURCE_ROOT)
        raw = release.name
    except Exception:
        raw = p.parent.name

    raw = os.path.splitext(os.path.basename(str(raw or "")))[0]
    raw = raw.replace("_", " ")

    # Find the earliest season boundary.  A release may contain both
    # "Season 2" and "S02", so the first marker is the hard boundary.
    season_matches = []

    m = re.search(
        r"(?i)(?:^|[ ._-])S\d{1,3}(?:E\d{1,3})*(?:$|[ ._-])",
        raw,
    )
    if m:
        # A leading "S4." can be part of a title/release name rather
        # than a season marker. Treat Sxx at position 0 as a boundary
        # only when it is an actual episode marker such as S04E01.
        if not (
            m.start() == 0
            and not re.match(r"(?i)^S\d{1,3}E", raw)
        ):
            season_matches.append(m)

    m = re.search(
        r"(?i)(?:^|[ ._-])Season[ ._-]*\d{1,3}\b",
        raw,
    )
    if m:
        season_matches.append(m)

    if season_matches:
        m = min(season_matches, key=lambda x: x.start())
        raw = raw[:m.start()]
    else:
        # Some releases have no season marker because the leading token
        # is part of the actual title, e.g. "S4.The.Bob.Lazar.Story.2026".
        # In that case, use the first technical/release token as the
        # structural boundary while preserving the title itself.
        technical = re.search(
            r"(?i)(?:^|[ ._-])"
            r"(?:2160p|1080p|720p|576p|480p|"
            r"4k|8k|"
            r"WEB[- .]?DL|WEB[- .]?RIP|"
            r"BLURAY|BLU[- .]?RAY|BRRIP|HDTV|DVDRIP|REMUX|"
            r"X264|X265|H264|H265|HEVC|AV1|"
            r"AAC|AC3|EAC3|DTS|DDP|TRUEHD|ATMOS|"
            r"HDR|DV|DOLBY.?VISION|"
            r"NETFLIX|AMZN|AMAZON|HULU|DISNEY|DISNEYPLUS|"
            r"DBTV|PROPER|REPACK)"
            r"(?=$|[ ._-])",
            raw,
        )
        if technical:
            raw = raw[:technical.start()]

    raw = raw.strip(" ._-")

    # Remove a release year, including (), [] and {} wrappers.
    # Preserve a leading four-digit numeric title such as "1923".
    def remove_release_year(match):
        before = raw[:match.start()]
        if not before.strip() and match.group(0).strip("()[]{}") == raw.strip():
            return match.group(0)
        return ""

    years = list(
        re.finditer(
            r"(?<!\d)[\(\[\{]?(?:19|20)\d{2}[\)\]\}]?(?!\d)",
            raw,
        )
    )

    if years:
        # A leading 4-digit token can itself be the series title.
        first = years[0]
        token = first.group(0).strip("()[]{}")

        if first.start() == 0 and len(token) == 4:
            # Keep it if another year follows; e.g. 1923.2022.S01.
            remaining = raw[first.end():]
            if re.search(
                r"(?<!\d)(?:19|20)\d{2}(?!\d)",
                remaining,
            ):
                keep_prefix = first.group(0)
                rest = raw[first.end():]
                rest = re.sub(
                    r"(?<!\d)[\(\[\{]?(?:19|20)\d{2}[\)\]\}]?(?!\d)",
                    "",
                    rest,
                    count=1,
                )
                raw = keep_prefix + rest
            else:
                # No second year: don't assume the leading number is a year.
                pass
        else:
            # Remove the last release-year token.  This handles titles such
            # as "A Million Little Things (2018)" and "[2018]".
            y = years[-1]
            raw = raw[:y.start()] + raw[y.end():]

    # Release separators become spaces.
    raw = re.sub(r"[._]+", " ", raw)

    # Preserve legitimate numeric title hyphens such as 9-1-1.
    raw = re.sub(r"\s*[-–—]\s*", " ", raw)
    raw = re.sub(r"(?<!\d)(\d)\s+(?=\d)", r"\1-", raw)

    # Remove punctuation left behind by a wrapped year.
    raw = re.sub(r"\s*[\(\[\{]\s*$", "", raw)
    raw = re.sub(r"\s*[\)\]\}]\s*", " ", raw)

    raw = re.sub(r"\s+", " ", raw).strip(" ._-")

    return raw

def _find_series(name, year, tmdb_id=None):
    qs = Series.objects.all()

    # Strongest match: TMDB ID.
    if tmdb_id:
        x = qs.filter(tmdb_id=tmdb_id).first()
        if x:
            return x

    key = _series_match_key(name)

    # Exact normalized match.
    for x in qs:
        if _series_match_key(x.name) == key:
            if year and x.year == year:
                return x

    for x in qs:
        if _series_match_key(x.name) == key:
            return x

    return None

def _update_obj(obj, data, kind):
    if not data: return
    cp = dict(obj.custom_properties or {})
    cp[MARKER + "_metadata"] = {"tmdb_id": data.get("id"), "genres": [g.get("name") for g in data.get("genres", []) if g.get("name")], "poster_path": data.get("poster_path"), "backdrop_path": data.get("backdrop_path"), "overview": data.get("overview")}
    changed = ["custom_properties"]
    if not getattr(obj, "description", None) and data.get("overview"):
        obj.description = data["overview"]; changed.append("description")
    if data.get("genres"):
        obj.genre = _genres(data); changed.append("genre")
    if data.get("vote_average") is not None and not getattr(obj, "rating", None):
        obj.rating = data.get("vote_average"); changed.append("rating")
    if not getattr(obj, "tmdb_id", None) and data.get("id"):
        try: obj.tmdb_id = data["id"]; changed.append("tmdb_id")
        except Exception: pass
    poster = data.get("poster_path")
    if poster and not getattr(obj, "logo_id", None):
        logo = _logo("https://image.tmdb.org/t/p/w500" + poster, "TMDB %s %s" % (kind, data.get("id")))
        if logo: obj.logo = logo; changed.append("logo")
    obj.custom_properties = cp
    obj.save(update_fields=list(dict.fromkeys(changed)))


def _make_strm(path, url):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# %s\n%s\n" % (MARKER, url))
    os.replace(tmp, path)





def _release_root(path, root):
    """Return the top-level Decypharr release directory for a source file.

    The release directory is the media identity. Internal Blu-ray stream
    filenames such as 00003.m2ts and BDMVSTREAM00294.m2ts are never used
    as media titles.
    """
    try:
        rel = Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root)))
    except ValueError:
        return Path(os.path.realpath(path)).parent

    parts = rel.parts
    if len(parts) >= 2:
        return Path(os.path.realpath(root)) / parts[0]

    return Path(os.path.realpath(path)).parent


def _internal_stream_name(path):
    """True for Blu-ray/internal stream filenames that must not become titles."""
    name = Path(path).stem

    if re.fullmatch(r"(?i)\d{5}", name):
        return True

    if re.fullmatch(r"(?i)BDMVSTREAM\d+", name):
        return True

    if re.search(r"(?i)(?:^|[._-])(?:sample|trailer|preview)(?:[._-]|$)", name):
        return True

    return False


def _is_blu_ray_release(release_dir, files):
    """Detect a release that contains Blu-ray transport streams."""
    if any(Path(x).suffix.lower() == ".m2ts" for x in files):
        return True

    name = Path(release_dir).name
    if re.search(r"(?i)\b(?:bluray|blu-ray|uhd|bd25|bd50|bdmv)\b", name):
        return True

    return False


def _release_season(release_dir):
    """Extract an explicit season number from a release directory."""
    name = Path(release_dir).name

    m = re.search(r"(?i)\bS(\d{1,3})(?:E\d{1,3})*", name)
    if m:
        return int(m.group(1))

    m = re.search(r"(?i)\bSeason[ ._-]?(\d{1,3})\b", name)
    if m:
        return int(m.group(1))

    m = re.search(r"(?i)(?<!\d)(\d{1,3})x\d{1,3}(?!\d)", name)
    if m:
        return int(m.group(1))

    return None


def _is_tv_release(release_dir, files):
    """Determine whether a release directory represents TV content."""
    name = Path(release_dir).name

    if re.search(r"(?i)\bS\d{1,3}(?:E\d{1,3})*\b", name):
        return True

    if re.search(r"(?i)\bSeason[ ._-]?\d{1,3}\b", name):
        return True

    if re.search(r"(?i)(?<!\d)\d{1,3}x\d{1,3}(?!\d)", name):
        return True

    for f in files:
        if _episode(f):
            return True

    return False


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _numeric_stream_number(path):
    """Return the internal numeric Blu-ray stream number, if present."""
    stem = Path(path).stem

    m = re.fullmatch(r"(?i)(\d{5})", stem)
    if m:
        return int(m.group(1))

    m = re.fullmatch(r"(?i)BDMVSTREAM(\d+)", stem)
    if m:
        return int(m.group(1))

    return None


def _usable_blu_ray_streams(files):
    """Return plausible feature/episode streams.

    Small Blu-ray menu/bonus/sample streams are filtered before FFprobe.
    The threshold is deliberately conservative because the examples in the
    Decypharr library have multi-GB feature/episode streams and tiny
    auxiliary streams.
    """
    candidates = []

    for f in files:
        if Path(f).suffix.lower() != ".m2ts":
            continue

        if _internal_stream_name(f) and re.search(
            r"(?i)(?:sample|trailer|preview)", Path(f).stem
        ):
            continue

        size = _file_size(f)

        # Do not spend 90 seconds probing tiny Blu-ray menu/bonus streams.
        # 512 MiB is intentionally conservative for TV/movie features.
        if size < 512 * 1024 * 1024:
            continue

        candidates.append(f)

    return candidates


def _movie_source_for_release(files):
    """Choose the main movie stream from a Blu-ray release.

    For Blu-ray movie releases the main feature is normally the largest
    transport stream by a substantial margin. This avoids probing every
    M2TS file and avoids menu/trailer streams becoming movies.
    """
    candidates = _usable_blu_ray_streams(files)

    if not candidates:
        return None

    return max(candidates, key=_file_size)


def _tv_blu_ray_episode_candidates(files):
    """Return numbered Blu-ray TV streams in deterministic episode order.

    Typical Decypharr TV Blu-ray releases look like:

        00000.m2ts
        00001.m2ts
        ...
        00012.m2ts

    The tiny menu/bonus streams are filtered out. Episode numbers are
    assigned sequentially starting at 1 according to the internal stream
    number.

    Explicit SxxExx filenames always remain authoritative elsewhere.
    """
    candidates = _usable_blu_ray_streams(files)

    numbered = []
    for f in candidates:
        n = _numeric_stream_number(f)
        if n is not None:
            numbered.append((n, f))

    numbered.sort(key=lambda x: x[0])

    return [f for _, f in numbered]


def _discover_media(root):
    """Discover logical media objects instead of raw source files.

    Returns dictionaries of the form:

        {
            "kind": "movie" | "episode",
            "path": source_path,
            "episode": (season, number) | None,
        }

    Release directories are treated as media identities for Blu-ray
    structures. Individual M2TS files are never independently scanned as
    movies.
    """
    root = os.path.realpath(root)

    release_files = {}
    direct_files = []

    for base, dirs, files in os.walk(root):
        for fn in files:
            p = os.path.realpath(os.path.join(base, fn))

            try:
                ext = Path(fn).suffix.lower()
            except Exception:
                continue

            if ext not in VIDEO_EXTS:
                continue

            if not os.path.isfile(p):
                continue

            release = str(_release_root(p, root))

            release_files.setdefault(release, []).append(p)

    discovered = []

    for release, files in sorted(release_files.items()):
        has_m2ts = any(Path(f).suffix.lower() == ".m2ts" for f in files)

        # --------------------------------------------------------
        # Blu-ray / M2TS release handling
        # --------------------------------------------------------
        if has_m2ts and _is_blu_ray_release(release, files):
            tv = _is_tv_release(release, files)

            if tv:
                season = _release_season(release)

                # If the release itself supplies a season and the files are
                # numbered internal streams, map them sequentially.
                candidates = _tv_blu_ray_episode_candidates(files)

                if season and candidates:
                    for number, source in enumerate(candidates, start=1):
                        discovered.append({
                            "kind": "episode",
                            "path": source,
                            "episode": (season, number),
                        })

                # Also preserve any explicitly episode-labelled files that
                # may coexist with the Blu-ray streams.
                for source in sorted(files):
                    if not os.path.isfile(source):
                        continue
                    epi = _episode(source)
                    if epi and source not in {x["path"] for x in discovered}:
                        discovered.append({
                            "kind": "episode",
                            "path": source,
                            "episode": epi,
                        })

            else:
                source = _movie_source_for_release(files)

                # No usable main feature means this release is incomplete
                # or consists only of samples/menus. Skip it safely.
                if source:
                    discovered.append({
                        "kind": "movie",
                        "path": source,
                        "episode": None,
                    })

            continue

        # --------------------------------------------------------
        # Normal non-Blu-ray media
        # --------------------------------------------------------
        for source in sorted(files):
            if _internal_stream_name(source):
                # Internal numeric/BMDV stream names without a Blu-ray
                # release context must never become standalone movies.
                if Path(source).suffix.lower() == ".m2ts":
                    continue

            epi = _episode(source)

            if epi:
                discovered.append({
                    "kind": "episode",
                    "path": source,
                    "episode": epi,
                })
            else:
                discovered.append({
                    "kind": "movie",
                    "path": source,
                    "episode": None,
                })

    return discovered


def _cleanup_library(lib, seen_files):
    if not os.path.isdir(lib):
        return
    for base, dirs, files in os.walk(lib, topdown=False):
        for fn in files:
            if not fn.lower().endswith(".strm"):
                continue
            p = os.path.realpath(os.path.join(base, fn))
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    first = f.readline().strip()
                if first == "# %s" % MARKER and p not in seen_files:
                    os.remove(p)
            except OSError:
                pass
        for d in dirs:
            dp = os.path.join(base, d)
            try:
                if os.path.isdir(dp) and not os.listdir(dp): os.rmdir(dp)
            except OSError:
                pass


def _quality_full(probe, source_hint=None):
    """Build the locked technical-quality suffix from FFprobe data.

    Examples:
      2160p BluRay HEVC DTS-HD MA 5.1
      1080p WEB-DL H264 AAC 2.0
      720p HDTV H264 AAC 2.0

    The function intentionally uses only technical metadata and never
    copies release-group/source garbage from Decypharr filenames.
    """
    probe = probe or {}
    video = (probe.get("video") or [{}])[0]
    audio = (probe.get("audio") or [{}])[0]

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)

    if height >= 2160 or width >= 3840:
        resolution = "2160p"
    elif height >= 1440:
        resolution = "1440p"
    elif height >= 1080:
        resolution = "1080p"
    elif height >= 720:
        resolution = "720p"
    elif height >= 576:
        resolution = "576p"
    elif height >= 480:
        resolution = "480p"
    else:
        resolution = ""

    fmt = str(probe.get("format") or "").lower()
    codec = str(video.get("codec") or "").lower()

    if codec in ("hevc", "h265"):
        vcodec = "HEVC"
    elif codec in ("h264", "avc"):
        vcodec = "H264"
    elif codec == "av1":
        vcodec = "AV1"
    elif codec:
        vcodec = codec.upper()
    else:
        vcodec = ""

    hint = str(source_hint or "").lower()

    if hint == "bluray":
        source = "BluRay"
    elif hint == "web":
        source = "WEB-DL"
    elif "bluray" in fmt or "bd" in fmt:
        source = "BluRay"
    elif "webm" in fmt:
        source = "WEB"
    elif "matroska" in fmt:
        source = ""
    elif "mpegts" in fmt:
        source = "MPEGTS"
    else:
        source = ""

    acodec = str(audio.get("codec") or "").lower()
    if acodec in ("eac3", "ec-3"):
        acodec_name = "EAC3"
    elif acodec in ("ac3",):
        acodec_name = "AC3"
    elif acodec in ("dts",):
        acodec_name = "DTS"
    elif acodec in ("truehd",):
        acodec_name = "TrueHD"
    elif acodec in ("aac",):
        acodec_name = "AAC"
    elif acodec:
        acodec_name = acodec.upper()
    else:
        acodec_name = ""

    channels = int(audio.get("channels") or 0)
    if channels == 1:
        channel_name = "1.0"
    elif channels == 2:
        channel_name = "2.0"
    elif channels == 6:
        channel_name = "5.1"
    elif channels == 8:
        channel_name = "7.1"
    elif channels > 0:
        channel_name = "%sch" % channels
    else:
        channel_name = ""

    parts = [x for x in (resolution, source, vcodec, acodec_name, channel_name) if x]

    # Avoid an empty/meaningless suffix if FFprobe failed.
    return " ".join(parts) if parts else "Unknown Quality"


def _movie_output_name(movie, probe):
    """Locked normalized movie filename."""
    base = movie.name
    if movie.year:
        base += " (%s)" % movie.year
    return "%s %s" % (base, _quality_full(probe, getattr(movie, "_decypharr_source_hint", None)))


def _episode_air_date(epdata, ep):
    """Return a real YYYY-MM-DD air date whenever available.

    TMDB season metadata is preferred. Existing Dispatcharr metadata
    is the fallback. TBA is only used when neither contains a valid date.
    """
    candidates = (
        (epdata or {}).get("air_date"),
        getattr(ep, "air_date", None),
    )

    for value in candidates:
        if value in (None, ""):
            continue

        value = str(value).strip()

        m = re.match(r"^(\d{4}-\d{2}-\d{2})$", value)
        if m:
            return m.group(1)

        m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ]", value)
        if m:
            return m.group(1)

    return "TBA"

def _episode_output_name(series, ep, epdata, probe, source_hint=None):
    """Locked normalized TV filename."""
    series_name = series.name
    air_date = _episode_air_date(epdata, ep)
    episode_name = (
        (epdata or {}).get("name")
        or getattr(ep, "name", None)
        or "Episode %02d" % int(ep.episode_number)
    )

    base = "%s - %s - %s" % (
        series_name,
        air_date,
        episode_name,
    )

    return "%s %s" % (base, _quality_full(probe, source_hint))


def _canonical_movie_stream_id(movie):
    """One Dispatcharr relation per logical movie."""
    return "%s-movie-%s" % (PREFIX, movie.id)


def _canonical_episode_stream_id(series, season, number):
    """One Dispatcharr relation per logical episode."""
    return "%s-episode-%s-%s-%s" % (
        PREFIX,
        series.id,
        int(season),
        int(number),
    )


def _media_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _dedupe_discovered_media(media):
    """Collapse duplicate logical discoveries before database processing.

    When multiple physical source files map to the same logical episode,
    prefer the largest usable source. This prevents duplicate relations and
    prevents scan order from deciding which source backs the .strm file.
    """
    selected = {}

    for item in media:
        path = item.get("path")
        if not path:
            continue

        kind = item.get("kind")
        epi = item.get("episode")

        release = _release_root(path, SOURCE_ROOT)
        release_name = _release_title(release.name)

        if kind == "episode" and epi:
            season, number = epi
            key = (
                "episode",
                _series_match_key(release_name),
                int(season),
                int(number),
            )
        else:
            key = (
                "movie",
                _series_match_key(release_name),
                _year(release_name) or 0,
            )

        old = selected.get(key)
        if old is None or _media_size(path) > _media_size(old["path"]):
            selected[key] = item

    return list(selected.values())


def _scan(plugin):
    if not SCAN_LOCK.acquire(blocking=False):
        return {"status": "busy"}

    try:
        settings = getattr(plugin, "_settings", {}) or {}
        cfg = {
            "root": settings.get("root_path") or SOURCE_ROOT,
            "library": settings.get("library_root") or LIBRARY_ROOT,
            "tmdb_key": settings.get("tmdb_api_key") or "",
            "metadata": settings.get("metadata_enabled", True),
            "ffprobe": settings.get("ffprobe_path") or "/usr/local/bin/ffprobe",
        }
        root = cfg["root"]
        lib = cfg["library"]

        if not os.path.isdir(root):
            return {
                "status": "error",
                "message": "Decypharr root not found: %s" % root,
            }

        os.makedirs(lib, exist_ok=True)

        account = _account()
        movie_cat = _category(account, "Decypharr Movies", "movie")
        tv_cat = _category(account, "Decypharr TV", "series")

        seen_movies = set()
        seen_eps = set()
        seen_series = set()
        seen_files = set()

        counts = {
            "movies": 0,
            "episodes": 0,
            "skipped": 0,
        }

        media = _discover_media(root)
        media = _dedupe_discovered_media(media)

        LOG.info(
            "Decypharr discovery found %d logical media objects after dedupe",
            len(media),
        )

        for item in media:
            p = item["path"]

            if not os.path.isfile(p):
                counts["skipped"] += 1
                continue

            try:
                epi = item.get("episode")

                # ========================================================
                # TV EPISODE
                # ========================================================
                if epi:
                    season, number = epi

                    release = _release_root(p, root)

                    try:
                        release_files = [
                            os.path.realpath(os.path.join(str(release), fn))
                            for fn in os.listdir(str(release))
                            if os.path.isfile(os.path.join(str(release), fn))
                        ]
                    except OSError:
                        release_files = []

                    # Always derive the initial candidate from the
                    # source path.  _series_title_from_path() handles
                    # normal SxxEyy files AND Blu-ray release directories.
                    series_name = _series_title_from_path(p)

                    if not series_name:
                        series_name = _release_title(release.name)

                    year = _year(str(release)) or _year(p)

                    data = (
                        _tmdb_tv(cfg["tmdb_key"], series_name, year)
                        if cfg["metadata"]
                        else None
                    )

                    if data and data.get("name"):
                        series_name = data["name"]

                    series = _find_series(
                        series_name,
                        year,
                        data.get("id") if data else None,
                    )

                    if series is None:
                        series = Series.objects.create(
                            name=series_name,
                            year=year,
                            custom_properties={MARKER: True},
                        )

                    _update_obj(series, data, "series")

                    rel, _ = M3USeriesRelation.objects.get_or_create(
                        m3u_account=account,
                        series=series,
                        external_series_id="%s-series-%s" % (
                            MARKER,
                            series.id,
                        ),
                        defaults={
                            "category": tv_cat,
                            "custom_properties": {MARKER: True},
                        },
                    )

                    rel.category = tv_cat
                    rel.custom_properties = dict(rel.custom_properties or {})
                    rel.custom_properties.update({
                        MARKER: True,
                        "decypharr_path": p, "episodes_fetched": True, "detailed_fetched": True,
                    })
                    rel.last_episode_refresh = timezone.now()
                    rel.save()

                    seen_series.add(series.id)

                    # ----------------------------------------------------
                    # Episode metadata
                    # ----------------------------------------------------
                    ep = Episode.objects.filter(
                        series=series,
                        season_number=season,
                        episode_number=number,
                    ).first()

                    epdata = None

                    if (
                        data
                        and data.get("id")
                        and cfg["metadata"]
                    ):
                        sd = _tmdb(
                            cfg["tmdb_key"],
                            "tv/%s/season/%s" % (
                                data["id"],
                                season,
                            ),
                            {},
                        ) or {}

                        epdata = next(
                            (
                                e for e in sd.get("episodes", [])
                                if e.get("episode_number") == number
                            ),
                            None,
                        )

                    if ep is None:
                        ep = Episode.objects.create(
                            series=series,
                            season_number=season,
                            episode_number=number,
                            name=(
                                (epdata or {}).get("name")
                                or _episode_title(p)
                            ),
                            custom_properties={MARKER: True},
                        )

                    ec = dict(ep.custom_properties or {})
                    ec[MARKER] = True

                    probe = _ffprobe(p, cfg["ffprobe"])

                    ec.update({
                        "decypharr_path": p, "episodes_fetched": True, "detailed_fetched": True,
                        "ffprobe": probe,
                    })

                    fields = ["custom_properties"]

                    if epdata:
                        for field, val in (
                            ("name", epdata.get("name")),
                            ("description", epdata.get("overview")),
                            ("air_date", epdata.get("air_date")),
                            ("rating", epdata.get("vote_average")),
                            ("tmdb_id", epdata.get("id")),
                        ):
                            if val not in (None, ""):
                                setattr(ep, field, val)
                                fields.append(field)

                        # Explicitly persist a valid TMDB air date.
                        tmdb_air_date = str(
                            epdata.get("air_date") or ""
                        ).strip()

                        if re.match(
                            r"^\d{4}-\d{2}-\d{2}$",
                            tmdb_air_date,
                        ):
                            ep.air_date = tmdb_air_date
                            if "air_date" not in fields:
                                fields.append("air_date")

                    tech = ec.get("ffprobe") or {}

                    if tech.get("duration"):
                        ep.duration_secs = int(float(tech["duration"]))
                        fields.append("duration_secs")

                    ep.custom_properties = ec

                    ep.save(
                        update_fields=list(dict.fromkeys(fields))
                    )

                    # ----------------------------------------------------
                    # ONE relation per canonical episode
                    # ----------------------------------------------------
                    stream_id = _canonical_episode_stream_id(
                        series,
                        season,
                        number,
                    )

                    er, _ = M3UEpisodeRelation.objects.get_or_create(
                        m3u_account=account,
                        stream_id=stream_id,
                        defaults={
                            "episode": ep,
                            "series_relation": rel,
                            "container_extension": (
                                Path(p).suffix.lstrip(".") or "mp4"
                            ),
                            "custom_properties": {MARKER: True},
                        },
                    )

                    er.episode = ep
                    er.series_relation = rel
                    er.container_extension = (
                        Path(p).suffix.lstrip(".") or "mp4"
                    )

                    er.custom_properties = dict(
                        er.custom_properties or {}
                    )

                    er.custom_properties.update({
                        MARKER: True,
                        "decypharr_path": p, "episodes_fetched": True, "detailed_fetched": True,
                        "ffprobe": probe,
                    })

                    er.save()

                    # ----------------------------------------------------
                    # Locked TV .strm path
                    # ----------------------------------------------------
                    out_series = _safe(
                        series.name
                        + (
                            " (%s)" % series.year
                            if series.year
                            else ""
                        )
                    )

                    source_hint = (
                        "bluray"
                        if (
                            Path(p).suffix.lower() == ".m2ts"
                            or _is_blu_ray_release(
                                str(release),
                                release_files,
                            )
                        )
                        else None
                    )

                    out_name = _safe(
                        _episode_output_name(
                            series,
                            ep,
                            epdata,
                            probe,
                            source_hint,
                        )
                    )

                    out = os.path.join(
                        lib,
                        "shows",
                        out_series,
                        "Season %02d" % int(season),
                        out_name + ".strm",
                    )

                    _make_strm(
                        out,
                        _play(
                            "episode",
                            er.id,
                            er.container_extension,
                        ),
                    )

                    seen_files.add(os.path.realpath(out))
                    seen_eps.add(er.id)

                    counts["episodes"] += 1

                # ========================================================
                # MOVIE
                # ========================================================
                else:
                    release = _release_root(p, root)

                    try:
                        release_files = [
                            os.path.realpath(os.path.join(str(release), fn))
                            for fn in os.listdir(str(release))
                            if os.path.isfile(os.path.join(str(release), fn))
                        ]
                    except OSError:
                        release_files = []

                    is_blu = (
                        Path(p).suffix.lower() == ".m2ts"
                        or _is_blu_ray_release(
                            str(release),
                            release_files,
                        )
                    )

                    if is_blu:
                        title_source = release.name
                        name = _release_title(title_source)
                    else:
                        title_source = p
                        name = _title(title_source)

                    year = _year(str(release)) or _year(p)

                    if _internal_stream_name(title_source):
                        counts["skipped"] += 1
                        LOG.warning(
                            "Skipping internal Blu-ray stream without "
                            "release identity: %s",
                            p,
                        )
                        continue

                    data = (
                        _tmdb_movie(
                            cfg["tmdb_key"],
                            name,
                            year,
                        )
                        if cfg["metadata"]
                        else None
                    )

                    if data and data.get("title"):
                        name = data["title"]

                    movie = _find_movie(
                        name,
                        year,
                        data.get("id") if data else None,
                    )

                    if movie is None:
                        movie = Movie.objects.create(
                            name=name,
                            year=year,
                            custom_properties={MARKER: True},
                        )

                    _update_obj(movie, data, "movie")

                    # ----------------------------------------------------
                    # Probe once; use same metadata for relation + filename
                    # ----------------------------------------------------
                    probe = _ffprobe(p, cfg["ffprobe"])

                    # ----------------------------------------------------
                    # ONE relation per canonical movie
                    # ----------------------------------------------------
                    stream_id = _canonical_movie_stream_id(movie)

                    mr, _ = M3UMovieRelation.objects.get_or_create(
                        m3u_account=account,
                        stream_id=stream_id,
                        defaults={
                            "movie": movie,
                            "category": movie_cat,
                            "container_extension": (
                                Path(p).suffix.lstrip(".") or "mp4"
                            ),
                            "custom_properties": {MARKER: True},
                        },
                    )

                    mr.movie = movie
                    mr.category = movie_cat
                    mr.container_extension = (
                        Path(p).suffix.lstrip(".") or "mp4"
                    )

                    mr.custom_properties = dict(
                        mr.custom_properties or {}
                    )

                    mr.custom_properties.update({
                        MARKER: True,
                        "decypharr_path": p, "episodes_fetched": True, "detailed_fetched": True,
                        "ffprobe": probe,
                    })

                    mr.save()

                    tech = mr.custom_properties.get("ffprobe") or {}

                    if tech.get("duration") and not movie.duration_secs:
                        movie.duration_secs = int(
                            float(tech["duration"])
                        )
                        movie.save(
                            update_fields=["duration_secs"]
                        )

                    # ----------------------------------------------------
                    # Locked movie .strm path
                    # ----------------------------------------------------
                    movie._decypharr_source_hint = (
                        "bluray" if is_blu else None
                    )

                    out_name = _safe(
                        _movie_output_name(
                            movie,
                            probe,
                        )
                    )

                    movie_dir_name = _safe(
                        movie.name
                        + (
                            " (%s)" % movie.year
                            if movie.year
                            else ""
                        )
                    )

                    out = os.path.join(
                        lib,
                        "movies",
                        movie_dir_name,
                        out_name + ".strm",
                    )

                    _make_strm(
                        out,
                        _play(
                            "movie",
                            mr.id,
                            mr.container_extension,
                        ),
                    )

                    seen_files.add(os.path.realpath(out))
                    seen_movies.add(mr.id)

                    counts["movies"] += 1

            except Exception:
                counts["skipped"] += 1
                LOG.exception(
                    "Failed processing %s",
                    p,
                )

        # ================================================================
        # Normalize all Decypharr series as locally episode-fetched.
        # Dispatcharr must not call the synthetic XC provider for episodes.
        # ================================================================
        for rel in M3USeriesRelation.objects.filter(m3u_account=account):
            props = dict(rel.custom_properties or {})
            props.update({
                MARKER: True,
                "episodes_fetched": True,
                "detailed_fetched": True,
            })
            rel.custom_properties = props
            rel.last_episode_refresh = timezone.now()
            rel.save(update_fields=["custom_properties", "last_episode_refresh"])

        # ================================================================
        # Remove stale normalized .strm files
        # ================================================================
        _cleanup_library(lib, seen_files)

        # ================================================================
        # Remove stale plugin-owned relations
        #
        # This also removes the old fingerprint-based duplicate relations
        # left by versions <= 0.3.0.
        # ================================================================
        M3UMovieRelation.objects.filter(
            m3u_account=account,
        ).exclude(
            id__in=seen_movies,
        ).delete()

        M3UEpisodeRelation.objects.filter(
            m3u_account=account,
        ).exclude(
            id__in=seen_eps,
        ).delete()

        # ================================================================
        # Remove plugin-owned orphan objects
        # ================================================================
        for obj in Movie.objects.filter(
            custom_properties__decypharr_vod=True
        ):
            if not M3UMovieRelation.objects.filter(
                movie=obj
            ).exists():
                obj.delete()

        for obj in Episode.objects.filter(
            custom_properties__decypharr_vod=True
        ):
            if not M3UEpisodeRelation.objects.filter(
                episode=obj
            ).exists():
                obj.delete()

        for obj in Series.objects.filter(
            custom_properties__decypharr_vod=True
        ):
            if not M3USeriesRelation.objects.filter(
                series=obj
            ).exists():
                obj.delete()

        return {
            "status": "ok",
            "message": (
                "Scan complete: %d movies, %d episodes, %d skipped"
                % (
                    counts["movies"],
                    counts["episodes"],
                    counts["skipped"],
                )
            ),
            "counts": counts,
            "discovered": len(media),
        }

    finally:
        SCAN_LOCK.release()


class Plugin:
    name = "Decypharr VOD"
    version = "0.4.7"
    description = "Imports Decypharr media as native Dispatcharr VOD with .strm presentation, FFprobe technical metadata, and optional TMDB metadata."
    author = "Tw1zT3d4"
    fields = [
        {"id": "root_path", "label": "Decypharr Root", "type": "string", "default": SOURCE_ROOT},
        {"id": "library_root", "label": "Normalized Library", "type": "string", "default": LIBRARY_ROOT},
        {"id": "tmdb_api_key", "label": "TMDB API Key", "type": "string", "default": ""},
        {"id": "metadata_enabled", "label": "TMDB Metadata", "type": "boolean", "default": True},
        {"id": "ffprobe_path", "label": "FFprobe Path", "type": "string", "default": "/usr/local/bin/ffprobe"},
        {"id": "scan_interval", "label": "Auto Scan Interval (seconds)", "type": "number", "default": 60},
    ]
    actions = [
        {"id": "scan", "label": "Scan Decypharr", "description": "Scan Decypharr and rebuild the normalized presentation library.", "button_label": "Scan Now", "button_variant": "filled", "button_color": "blue"},
        {"id": "repair", "label": "Repair Integration", "description": "Reinstall routes and relation hooks and ensure the synthetic account exists.", "button_label": "Repair", "button_variant": "outlined", "button_color": "gray"},
    ]

    def __init__(self):
        self._settings = {}
        _install_route(); _patch_relations(); _account()

    def run(self, action, params, context):
        self._settings = (context or {}).get("settings") or getattr(self, "_settings", {}) or {}
        if action == "repair":
            _install_route(); _patch_relations(); _account()
            return {"status": "ok", "message": "Decypharr VOD integration repaired.", "route_installed": ROUTE_INSTALLED, "patched": PATCHED}
        if action == "scan":
            return _scan(self)
        return {"status": "error", "message": "Unknown action: %s" % action}