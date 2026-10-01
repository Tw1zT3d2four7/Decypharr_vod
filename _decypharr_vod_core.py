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
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Dispatcharr runs on Linux
    fcntl = None

from django.db import connection, transaction, close_old_connections
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.urls import path
from django.utils import timezone

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
SOURCE_ROOT = "/tmp/decypharr_vod_virtual"
LIBRARY_ROOT = "/data/plugins/decypharr_vod/library"
DEFAULT_API_URL = "http://192.168.1.11:8282"
STATE_DIR = "/data/plugins/decypharr_vod"
API_CACHE_FILE = os.path.join(STATE_DIR, "api_inventory.json")
API_PAGE_SIZE = 50
API_WORKERS = 4
API_TOKEN = ""
API_BASE_URL = DEFAULT_API_URL
CACHE_DIR = os.path.join(STATE_DIR, "metadata")
SECRET_FILE = os.path.join(STATE_DIR, ".secret")
MARKER = "decypharr_vod"
PREFIX = "decypharr-"
ROUTE_INSTALLED = False
PATCHED = False
SCAN_LOCK = threading.Lock()
SCAN_PROCESS_LOCK_PATH = os.path.join(STATE_DIR, "scan.lock")
INVENTORY_STATE_FILE = os.path.join(STATE_DIR, "inventory_state.json")
METADATA_STATE_FILE = os.path.join(STATE_DIR, "metadata_state.json")
AUTO_SCAN_THREAD = None
AUTO_SCAN_STOP = threading.Event()
AUTO_SCAN_START_LOCK = threading.Lock()
REFRESH_GUARDS_PATCHED = False

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
    req = urllib.request.Request(url, headers={"User-Agent": "Decypharr-VOD/1.0.1"})
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


def _preferred_audio_code(settings=None):
    """Return the configured ISO-639 language code; English is the public default."""
    settings = settings if isinstance(settings, dict) else {}
    value = str(settings.get("preferred_audio_language") or "eng").strip().lower()
    if value in ("", "original", "source", "first"):
        return "original"
    aliases = {
        "english": "eng", "spanish": "spa", "french": "fra", "german": "deu",
        "italian": "ita", "portuguese": "por", "japanese": "jpn", "korean": "kor",
        "chinese": "zho", "hindi": "hin", "arabic": "ara",
    }
    return aliases.get(value, value[:8])


def _preferred_audio_stream(audio_tracks, preferred="eng"):
    """Choose the preferred audio stream while retaining every source track."""
    tracks = list(audio_tracks or [])
    if not tracks:
        return None
    wanted = str(preferred or "eng").strip().lower()
    aliases = {
        "eng": "en", "spa": "es", "fra": "fr", "deu": "de", "ita": "it",
        "por": "pt", "jpn": "ja", "kor": "ko", "zho": "zh", "hin": "hi",
        "ara": "ar",
    }
    if wanted not in ("original", "source", "first"):
        for track in tracks:
            lang = str(track.get("language") or "").lower()
            if lang == wanted or lang == aliases.get(wanted):
                return track
    for track in tracks:
        if track.get("default"):
            return track
    return tracks[0]


def _ffprobe(path, exe):
    if not shutil.which(exe) and not os.path.exists(exe):
        return {"error": "ffprobe not found"}
    try:
        cmd = [exe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams"]
        if str(path).startswith(("http://", "https://")) and API_TOKEN:
            cmd += ["-headers", "Authorization: Bearer %s\r\n" % API_TOKEN]
        cmd.append(path)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if p.returncode:
            return {"error": p.stderr[-500:]}
        raw = json.loads(p.stdout or "{}")
        streams = raw.get("streams", [])
        videos, audios, subs = [], [], []
        for st in streams:
            x = {"index": st.get("index"), "codec": st.get("codec_name"), "language": (st.get("tags") or {}).get("language"), "title": (st.get("tags") or {}).get("title")}
            if st.get("codec_type") == "video":
                x.update({"width": st.get("width"), "height": st.get("height"), "fps": st.get("r_frame_rate"), "pix_fmt": st.get("pix_fmt"), "hdr": st.get("color_transfer"), "profile": st.get("profile")})
                videos.append(x)
            elif st.get("codec_type") == "audio":
                disposition = st.get("disposition") or {}
                x.update({
                    "channels": st.get("channels"),
                    "sample_rate": st.get("sample_rate"),
                    "bitrate": st.get("bit_rate"),
                    "default": bool(disposition.get("default")),
                    "forced": bool(disposition.get("forced")),
                    "original": bool(disposition.get("original")),
                    "dub": bool(disposition.get("dub")),
                })
                audios.append(x)
            elif st.get("codec_type") == "subtitle":
                subs.append(x)
        preferred = _preferred_audio_stream(audios, _preferred_audio_code(_fresh_plugin_settings()))
        return {
            "duration": float((raw.get("format") or {}).get("duration") or 0),
            "bitrate": int((raw.get("format") or {}).get("bit_rate") or 0),
            "format": (raw.get("format") or {}).get("format_name"),
            "title": ((raw.get("format") or {}).get("tags") or {}).get("title") or "",
            "video": videos,
            "audio": audios,
            "preferred_audio": preferred,
            "subtitles": subs,
        }
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
    """
    Generate a native Dispatcharr VOD URL.

    `rel` is the native VOD content UUID, not the relation ID.
    The Decypharr API token is never placed in the .strm URL.
    """
    return "http://127.0.0.1:9191/proxy/vod/%s/%s" % (kind, rel)


def _current_api_token():
    """Load the current Decypharr API token from the active plugin settings.

    Playback must not depend on the module-global API_TOKEN because the
    Django worker may have loaded the playback route before a plugin scan
    populated that global.
    """
    try:
        from apps.plugins.models import PluginConfig

        config = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        if config:
            settings = getattr(config, "settings", {}) or {}
            token = settings.get("api_token") or ""
            if token:
                return str(token).strip()
    except Exception:
        LOG.exception("Failed to load Decypharr API token from PluginConfig")

    return API_TOKEN or ""


def _proxy_api_file(request, api_url):
    api_token = _current_api_token()
    if not api_token:
        LOG.error("Decypharr playback attempted without an API token")
        return HttpResponse(status=503)

    headers = {
        "Authorization": "Bearer %s" % api_token,
        "User-Agent": "Dispatcharr-Decypharr-VOD/1.0.1",
    }
    rng = request.headers.get("Range")
    if rng:
        headers["Range"] = rng
    req = urllib.request.Request(api_url, headers=headers)
    try:
        upstream = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        return HttpResponse(status=e.code)
    except Exception as e:
        LOG.exception("Decypharr playback request failed: %s", e)
        return HttpResponse(status=502)

    status = getattr(upstream, "status", 200)
    content_type = upstream.headers.get("Content-Type") or "application/octet-stream"

    def chunks():
        try:
            while True:
                data = upstream.read(1024 * 1024)
                if not data:
                    break
                yield data
        finally:
            upstream.close()

    response = StreamingHttpResponse(chunks(), status=status, content_type=content_type)
    for name in ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition", "ETag", "Last-Modified"):
        value = upstream.headers.get(name)
        if value:
            response[name] = value
    response["Accept-Ranges"] = upstream.headers.get("Accept-Ranges", "bytes")
    return response


def _stream_file(request, kind, rid):
    Model = M3UMovieRelation if kind == "movie" else M3UEpisodeRelation
    try:
        rel = Model.objects.select_related("m3u_account").get(id=int(rid), m3u_account__name=ACCOUNT_NAME)
    except Exception:
        raise Http404
    props = rel.custom_properties or {}
    api_url = props.get("decypharr_api_url")
    if not api_url:
        raise Http404
    return _proxy_api_file(request, api_url)


def _install_route():
    """
    Retained for compatibility with the existing Repair action.

    Decypharr VOD no longer installs a dynamic Django URL route.
    Playback uses Dispatcharr's native /proxy/vod/ endpoint.
    """
    global ROUTE_INSTALLED
    ROUTE_INSTALLED = False
    LOG.info("Decypharr VOD: native Dispatcharr VOD routing enabled.")

def _patch_relations():
    """
    Install native Dispatcharr VOD hooks.

    Decypharr relations remain normal XC relations for Dispatcharr's
    selection machinery, but native VOD URL construction is overridden
    for relations owned by this plugin.
    """
    global PATCHED
    if PATCHED:
        return

    from apps.proxy.vod_proxy import views as vod_views
    from apps.proxy.vod_proxy import multi_worker_connection_manager as mwcm

    # --------------------------------------------------------
    # Native VOD URL builder
    # --------------------------------------------------------
    if not hasattr(vod_views, "_decypharr_original_build_vod_stream_url"):
        vod_views._decypharr_original_build_vod_stream_url = (
            vod_views._build_vod_stream_url
        )

        original_build = vod_views._build_vod_stream_url

        def decypharr_build_vod_stream_url(
            relation,
            m3u_profile,
            content_type,
        ):
            props = getattr(relation, "custom_properties", {}) or {}

            if props.get(MARKER) or getattr(
                getattr(relation, "m3u_account", None),
                "name",
                "",
            ) == ACCOUNT_NAME:
                api_url = props.get("decypharr_api_url")

                if api_url:
                    LOG.info(
                        "[NATIVE-VOD] Decypharr URL selected for relation %s",
                        getattr(relation, "id", "?"),
                    )
                    return api_url

                LOG.error(
                    "[NATIVE-VOD] Decypharr relation %s has no "
                    "decypharr_api_url",
                    getattr(relation, "id", "?"),
                )
                return None

            return original_build(
                relation,
                m3u_profile,
                content_type,
            )

        vod_views._build_vod_stream_url = decypharr_build_vod_stream_url

    # --------------------------------------------------------
    # Native Redis connection header injection
    # --------------------------------------------------------
    RedisBackedVODConnection = mwcm.RedisBackedVODConnection

    if not hasattr(
        RedisBackedVODConnection,
        "_decypharr_original_create_connection",
    ):
        RedisBackedVODConnection._decypharr_original_create_connection = (
            RedisBackedVODConnection.create_connection
        )

        original_create = RedisBackedVODConnection.create_connection

        def decypharr_create_connection(
            self,
            stream_url,
            headers,
            m3u_profile_id=None,
            content_obj_type=None,
            content_uuid=None,
            content_name=None,
            client_ip=None,
            client_user_agent=None,
            utc_start=None,
            utc_end=None,
            offset=None,
            worker_id=None,
            user=None,
        ):
            headers = dict(headers or {})

            if "/api/browse/download/" in str(stream_url):
                token = _current_api_token()

                if not token:
                    LOG.error(
                        "[NATIVE-VOD] Decypharr connection requested "
                        "without an API token"
                    )
                else:
                    headers["Authorization"] = "Bearer %s" % token
                    headers.setdefault(
                        "User-Agent",
                        "Dispatcharr-Decypharr-VOD/1.0.1",
                    )

                    LOG.info(
                        "[NATIVE-VOD] Decypharr Authorization header "
                        "attached to Redis connection"
                    )

            return original_create(
                self,
                stream_url,
                headers,
                m3u_profile_id,
                content_obj_type,
                content_uuid,
                content_name,
                client_ip,
                client_user_agent,
                utc_start,
                utc_end,
                offset,
                worker_id,
                user,
            )

        RedisBackedVODConnection.create_connection = (
            decypharr_create_connection
        )

    PATCHED = True
    LOG.info("Decypharr VOD native Dispatcharr VOD hooks installed.")

def _account():
    """
    Ensure the synthetic XC account is always active.

    Dispatcharr's VOD relation layer requires an XC account, but this account
    is not a real upstream provider and must never be allowed to refresh
    against the placeholder 127.0.0.1 server.
    """
    a = M3UAccount.objects.filter(name=ACCOUNT_NAME).first()
    if a:
        changed = []
        props = dict(a.custom_properties or {})
        if not props.get(MARKER):
            props[MARKER] = True
            a.custom_properties = props
            changed.append("custom_properties")
        if not a.is_active:
            a.is_active = True
            changed.append("is_active")
        if changed:
            a.save(update_fields=list(dict.fromkeys(changed)))
        return a

    return M3UAccount.objects.create(
        name=ACCOUNT_NAME,
        account_type=M3UAccount.Types.XC,
        server_url="http://127.0.0.1",
        username="decypharr",
        password="disabled",
        file_path=LIBRARY_ROOT,
        is_active=True,
        priority=10000,
        max_streams=0,
        custom_properties={MARKER: True},
    )


def _category(account, name, kind):
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name:
        return None

    # Reuse an existing category case-insensitively so repeated scans never
    # create Action/action-style duplicates.
    c = VODCategory.objects.filter(
        category_type=kind,
        name__iexact=name,
    ).first()
    if c is None:
        c = VODCategory.objects.create(name=name, category_type=kind)

    rel, _ = M3UVODCategoryRelation.objects.get_or_create(
        m3u_account=account,
        category=c,
        defaults={
            "enabled": True,
            "custom_properties": {MARKER: True},
        },
    )
    props = dict(rel.custom_properties or {})
    if not props.get(MARKER) or not rel.enabled:
        props[MARKER] = True
        rel.enabled = True
        rel.custom_properties = props
        rel.save(update_fields=["enabled", "custom_properties"])
    return c


def _tmdb_genres(data):
    if not data:
        return []
    names = []
    seen = set()
    for genre in data.get("genres") or []:
        name = re.sub(r"\s+", " ", str((genre or {}).get("name") or "")).strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _genre_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").casefold()).strip("-")
    return slug or "genre"


def _genre_stream_id(base_id, genre_name):
    suffix = "--genre-" + _genre_slug(genre_name)
    if len(base_id) + len(suffix) <= 255:
        return base_id + suffix
    digest = hashlib.sha1(genre_name.encode("utf-8", "ignore")).hexdigest()[:12]
    return base_id[:255 - len(suffix) - 13] + "-" + digest + suffix


def _genre_categories(account, data, kind):
    names = _tmdb_genres(data)
    return [(name, _category(account, name, kind)) for name in names]


def _genre_relation_queryset(model, account, base_id, content_field, content_obj):
    """Return legacy genre-copy relations for one canonical content item."""
    qs = model.objects.filter(m3u_account=account)

    # Newer legacy rows carry an explicit marker.
    marked = qs.filter(
        custom_properties__decypharr_genre_relation=True,
        **{content_field: content_obj},
    )

    # Older v1.0.0 rows can predate the marker, so also catch their
    # historical --genre-* stream/external IDs.  The ID prefix changed
    # between generations (decypharr--movie-* vs decypharr-movie-*).
    if model is M3UMovieRelation:
        historical = qs.filter(
            stream_id__contains="--genre-",
        ).filter(
            stream_id__contains=base_id,
        )
    elif model is M3UEpisodeRelation:
        historical = qs.filter(
            stream_id__contains="--genre-",
            stream_id__contains=base_id,
        )
    else:
        historical = qs.filter(
            external_series_id__contains="-genre-",
        ).filter(
            external_series_id__contains=base_id,
        )

    return (marked | historical).exclude(id=getattr(content_obj, "id", None)).distinct()


def _sync_movie_genre_relations(account, movie, base_rel, genre_categories):
    """Keep one canonical movie relation; genres are metadata/category data."""
    names = [name for name, category in genre_categories if category]
    categories = [category for name, category in genre_categories if category]

    props = dict(base_rel.custom_properties or {})
    props[MARKER] = True
    props["decypharr_genres"] = names
    base_rel.category = categories[0] if categories else None
    base_rel.custom_properties = props
    base_rel.save(update_fields=["category", "custom_properties"])

    # Never create one relation per genre.  Remove both the marked legacy
    # rows and the older ID-based rows, regardless of which ID generation
    # produced them.
    canonical = base_rel.stream_id
    _genre_relation_queryset(
        M3UMovieRelation,
        account,
        canonical,
        "movie",
        movie,
    ).delete()

    return [base_rel.id]


def _sync_series_genre_relations(account, series, base_rel, genre_categories):
    """Keep one canonical series relation; genres are metadata/category data."""
    names = [name for name, category in genre_categories if category]
    categories = [category for name, category in genre_categories if category]

    props = dict(base_rel.custom_properties or {})
    props[MARKER] = True
    props["decypharr_genres"] = names
    base_rel.category = categories[0] if categories else None
    base_rel.custom_properties = props
    base_rel.save(update_fields=["category", "custom_properties"])

    canonical = base_rel.external_series_id
    _genre_relation_queryset(
        M3USeriesRelation,
        account,
        canonical,
        "series",
        series,
    ).delete()

    return [(names[0], base_rel)] if names else [(None, base_rel)]


def _sync_episode_genre_relations(
    account,
    episode,
    base_rel,
    series_relations,
    path,
    api_url,
    item,
    probe,
    season,
    number,
    seen_eps,
):
    """Keep one canonical episode relation; genres are metadata only."""
    names = [name for name, _ in series_relations if name]
    props = dict(base_rel.custom_properties or {})
    props[MARKER] = True
    props["decypharr_genres"] = names
    base_rel.custom_properties = props
    base_rel.save(update_fields=["custom_properties"])

    canonical = base_rel.stream_id
    _genre_relation_queryset(
        M3UEpisodeRelation,
        account,
        canonical,
        "episode",
        episode,
    ).delete()

    seen_eps.add(base_rel.id)


def _cleanup_legacy_categories(account):
    for name in ("Decypharr Movies", "Decypharr TV"):
        for category in VODCategory.objects.filter(name=name):
            M3UVODCategoryRelation.objects.filter(
                m3u_account=account,
                category=category,
            ).delete()
            if not M3UVODCategoryRelation.objects.filter(category=category).exists():
                category.delete()


def _is_synthetic_account(account_id):
    try:
        return M3UAccount.objects.filter(
            id=account_id,
            custom_properties__decypharr_vod=True,
        ).exists()
    except Exception:
        return False


def _patch_refresh_guards():
    """
    Keep the synthetic XC account active for VOD relations while making every
    normal Dispatcharr refresh path a successful no-op for that account.
    """
    global REFRESH_GUARDS_PATCHED
    if REFRESH_GUARDS_PATCHED:
        return True

    try:
        from apps.m3u import tasks as m3u_tasks

        def guard_task(task, account_arg="account_id", profile_arg=None, result=None, label="refresh"):
            original = getattr(task, "run", None)
            if original is None:
                return
            attr = "_decypharr_original_run_" + label
            if hasattr(task, attr):
                return
            setattr(task, attr, original)

            def guarded(*args, **kwargs):
                value = kwargs.get(account_arg)
                if value is None and args:
                    value = args[0]
                if profile_arg is not None:
                    try:
                        from apps.m3u.models import M3UAccountProfile
                        profile_id = kwargs.get(profile_arg)
                        if profile_id is None and args:
                            profile_id = args[0]
                        profile = M3UAccountProfile.objects.select_related("m3u_account").filter(id=profile_id).first()
                        if profile and _is_synthetic_account(profile.m3u_account_id):
                            LOG.info("Decypharr VOD: intercepted synthetic XC %s for profile %s", label, profile_id)
                            return result() if callable(result) else result
                    except Exception:
                        LOG.exception("Decypharr VOD: refresh guard profile lookup failed")
                elif value is not None and _is_synthetic_account(value):
                    LOG.info("Decypharr VOD: intercepted synthetic XC %s for account %s", label, value)
                    return result() if callable(result) else result
                return original(*args, **kwargs)

            task.run = guarded

        guard_task(
            m3u_tasks.refresh_single_m3u_account,
            result=lambda: "Decypharr VOD synthetic account refresh skipped.",
            label="refresh_single_m3u_account",
        )
        guard_task(
            m3u_tasks.refresh_m3u_groups,
            result=lambda: ("Decypharr VOD synthetic account group refresh skipped.", None),
            label="refresh_m3u_groups",
        )
        guard_task(
            m3u_tasks.refresh_account_info,
            profile_arg="profile_id",
            result=lambda: "Decypharr VOD synthetic account information refresh skipped.",
            label="refresh_account_info",
        )

        REFRESH_GUARDS_PATCHED = True
        LOG.info("Decypharr VOD: synthetic XC refresh guards installed.")
        return True
    except Exception:
        LOG.exception("Decypharr VOD: could not install synthetic XC refresh guards")
        return False


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


def _api_json(url, token, params=None, timeout=30):
    q = urllib.parse.urlencode(params or {})
    full = url + (("&" if "?" in url else "?") + q if q else "")
    req = urllib.request.Request(full, headers={"Authorization": "Bearer %s" % token, "Accept": "application/json", "User-Agent": "Dispatcharr-Decypharr-VOD/1.0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _api_items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "entries", "results", "files", "torrents", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _api_items(value)
            if nested:
                return nested
    return []


def _api_has_more(payload, page, count):
    if not isinstance(payload, dict):
        return count >= API_PAGE_SIZE
    total_pages = payload.get("total_pages") or payload.get("pages")
    if total_pages is not None:
        try: return page < int(total_pages)
        except Exception: pass
    total = payload.get("total") or payload.get("count")
    if total is not None:
        try: return page * API_PAGE_SIZE < int(total)
        except Exception: pass
    return count >= API_PAGE_SIZE


def _api_paginated(url, token):
    all_items = []
    page = 1
    while True:
        payload = _api_json(url, token, {"page": page, "page_size": API_PAGE_SIZE})
        items = _api_items(payload)
        if not items:
            break
        all_items.extend(items)
        if not _api_has_more(payload, page, len(items)):
            break
        page += 1
        if page > 10000:
            raise RuntimeError("Decypharr pagination exceeded safety limit")
    return all_items


def _api_torrent_id(entry):
    if not isinstance(entry, dict): return ""
    return str(entry.get("info_hash") or entry.get("hash") or entry.get("torrent") or entry.get("id") or "")


def _api_file_name(entry):
    if not isinstance(entry, dict): return ""
    return str(entry.get("name") or entry.get("filename") or entry.get("file") or entry.get("path") or "")


def _api_file_path(entry):
    if not isinstance(entry, dict): return ""
    return str(entry.get("api_path") or entry.get("download_path") or entry.get("path") or entry.get("url") or entry.get("file") or "")


def _api_release_name(entry):
    if not isinstance(entry, dict): return ""
    return str(entry.get("name") or entry.get("title") or entry.get("torrent_name") or entry.get("path") or "")


def _api_download_url(base, info_hash, path):
    """
    Build the exact Decypharr FileBrowser download URL.

    Decypharr's web UI uses only the final two path components:
      /api/browse/download/<parent>/<filename>

    The info_hash and __all__ path components are NOT part of the
    download endpoint.
    """
    parts = [part for part in str(path or "").split("/") if part]
    if len(parts) < 2:
        return ""

    parent = urllib.parse.quote(parts[-2], safe="")
    filename = urllib.parse.quote(parts[-1], safe="")

    return (
        base.rstrip("/")
        + "/api/browse/download/"
        + parent
        + "/"
        + filename
    )


def _api_inventory_load():
    try:
        with open(API_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"releases": {}, "files": {}}


def _api_inventory_save(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = API_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, API_CACHE_FILE)


def _api_child_files(base, token, release):
    release_path = _api_file_path(release)
    if not release_path:
        return []

    url = base.rstrip("/") + "/api/browse" + release_path
    return _api_paginated(url, token)


def _discover_media(root, api_url=None, api_token=None, batch_callback=None, batch_size=200, collect=True, inventory_digest_callback=None):
    if not api_url or not api_token:
        return []

    state = _api_inventory_load()
    cached_files = state.get("files") or {}
    releases = _api_paginated(api_url.rstrip("/") + "/api/browse/__all__", api_token)
    current = {}
    cached_meta = state.get("release_meta") or {}

    def signature(release):
        if not isinstance(release, dict):
            return ""
        keys = ("updated_at", "modified_at", "mtime", "size", "file_count", "files_count", "name", "path")
        return json.dumps({k: release.get(k) for k in keys if k in release}, sort_keys=True, default=str)

    def fetch(release):
        h = _api_torrent_id(release)
        if not h:
            return None, []
        try:
            return h, _api_child_files(api_url, api_token, release)
        except Exception:
            LOG.exception("Failed reading Decypharr release %s", h)
            return h, []

    # Reuse child inventories when the release entry is unchanged. New or
    # changed releases are fetched in parallel, avoiding thousands of child
    # API requests on every scan.
    missing = []
    for release in releases:
        h = _api_torrent_id(release)
        if not h:
            continue
        current[h] = release
        sig = signature(release)
        if h not in cached_files or cached_meta.get(h) != sig:
            missing.append(release)
            cached_meta[h] = sig

    if missing:
        with ThreadPoolExecutor(max_workers=max(1, min(8, int(API_WORKERS or 4)))) as pool:
            futures = [pool.submit(fetch, release) for release in missing]
            for future in as_completed(futures):
                h, files = future.result()
                if h:
                    cached_files[h] = files

    # Remove deleted releases from the persistent inventory.
    cached_files = {h: files for h, files in cached_files.items() if h in current}
    cached_meta = {h: meta for h, meta in cached_meta.items() if h in current}
    state["files"] = cached_files
    state["releases"] = current
    state["release_meta"] = cached_meta
    _api_inventory_save(state)

    discovered = []
    virtual_root = os.path.realpath(root)
    os.makedirs(virtual_root, exist_ok=True)
    pending_batch = []

    for h, release in current.items():
        release_name = _api_release_name(release) or h
        files = cached_files.get(h) or []
        release_items = []
        for child in files:
            name = _api_file_name(child)
            if not name:
                continue
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_EXTS:
                continue
            file_path = _api_file_path(child) or name
            api_path = file_path if file_path.startswith("http") else _api_download_url(api_url, h, file_path)
            safe_release = _safe(release_name) or h
            virtual = os.path.join(virtual_root, safe_release, os.path.basename(name))
            epi = _episode(name)
            tv_release = _is_tv_release(
                os.path.join(virtual_root, safe_release),
                [_api_file_name(x) for x in files if _api_file_name(x)],
            )
            kind = "episode" if epi else ("season_file" if tv_release else "movie")
            item = {
                "kind": kind,
                "path": virtual,
                "api_url": api_path,
                "episode": epi,
                "info_hash": h,
                "file_path": file_path,
                "release_name": release_name,
                "file_name": name,
            }
            release_items.append(item)

        release_items = _normalize_season_pack_items(release_items)
        for item in release_items:
            pending_batch.append(item)
            if inventory_digest_callback:
                inventory_digest_callback(_inventory_item_digest(item))
            if collect:
                discovered.append(item)
            if batch_callback and batch_size > 0 and len(pending_batch) >= int(batch_size):
                batch_callback(pending_batch)
                pending_batch = []

    if batch_callback and pending_batch:
        batch_callback(pending_batch)

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


def _release_group(value):
    """Return a conventional trailing release group from the release title."""
    value = str(value or "").strip()
    m = TRAILING_GROUP_RE.search(value)
    if m:
        return m.group(0).strip()[2:].strip()
    m = re.search(r"(?i)\s+\[([A-Za-z0-9][A-Za-z0-9._-]{1,30})\]\s*$", value)
    return m.group(1) if m else ""


def _release_source_hint(value):
    value = str(value or "").lower()
    if re.search(r"\b(?:bluray|blu[ ._-]?ray|brrip|bd25|bd50|uhd)\b", value):
        return "bluray"
    if re.search(r"\b(?:web[ ._-]?dl|webdl|web[ ._-]?rip|webrip)\b", value):
        return "web"
    if re.search(r"\b(?:hdtv|pdtv|dsr)\b", value):
        return "hdtv"
    return None


def _movie_output_name(movie, probe):
    """TRaSH-style normalized movie filename."""
    base = movie.name
    if movie.year:
        base += " (%s)" % movie.year
    quality = _quality_full(probe, getattr(movie, "_decypharr_source_hint", None))
    group = _release_group(getattr(movie, "_decypharr_release_name", ""))
    suffix = "[%s]" % quality
    if group:
        suffix += "-%s" % group
    return "%s %s" % (base, suffix)


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

def _episode_output_name(series, ep, epdata, probe, source_hint=None, release_name=None):
    """TRaSH-style normalized TV filename using SxxExx."""
    series_name = series.name
    if getattr(series, "year", None):
        series_name += " (%s)" % series.year
    episode_name = (
        (epdata or {}).get("name")
        or getattr(ep, "name", None)
        or "Episode %02d" % int(ep.episode_number)
    )
    base = "%s - S%02dE%02d - %s" % (
        series_name,
        int(ep.season_number),
        int(ep.episode_number),
        episode_name,
    )
    quality = _quality_full(probe, source_hint)
    suffix = "[%s]" % quality
    group = _release_group(release_name)
    if group:
        suffix += "-%s" % group
    return "%s %s" % (base, suffix)


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


def _normalize_season_pack_items(media):
    """
    Resolve season-pack items before any fast import or movie fallback.

    A Decypharr release can contain obfuscated child filenames (for example
    numbered .m2ts files) that have no SxxExx marker.  Those items are marked
    as season_file during discovery.  They must never be allowed to fall
    through the movie path simply because FFprobe/TMDB has not run yet.

    The release's explicit season marker is authoritative.  If it is absent,
    use an already-classified episode from the same release when available.
    Only then assign deterministic episode numbers to the remaining members.
    Unresolved TV-looking items stay season_file and are deferred.
    """
    groups = {}

    for item in media:
        if item.get("kind") != "season_file":
            continue

        key = str(
            item.get("info_hash")
            or item.get("release_name")
            or _release_root(item.get("path") or "", SOURCE_ROOT)
        )
        groups.setdefault(key, []).append(item)

    for group in groups.values():
        group.sort(
            key=lambda x: str(
                x.get("file_path")
                or x.get("file_name")
                or x.get("path")
                or ""
            )
        )

        season = _release_season(group[0].get("release_name") or "")

        if season is None:
            # Some releases have explicit SxxExx members mixed with
            # obfuscated members. Reuse the season from those members.
            for candidate in media:
                if str(candidate.get("info_hash") or "") != str(
                    group[0].get("info_hash") or ""
                ):
                    continue
                epi = candidate.get("episode")
                if epi:
                    season = int(epi[0])
                    break

        if season is None:
            # Do NOT turn an unresolved TV release into a movie.
            continue

        # Preserve explicit episode numbers if present and assign the
        # remaining season-pack members deterministically around them.
        used = {
            int(item["episode"][1])
            for item in group
            if item.get("episode") and int(item["episode"][0]) == int(season)
        }

        next_number = 1
        for item in group:
            if item.get("episode"):
                item["kind"] = "episode"
                item["season_pack"] = True
                continue

            while next_number in used:
                next_number += 1

            item["episode"] = (int(season), next_number)
            item["kind"] = "episode"
            item["season_pack"] = True
            used.add(next_number)
            next_number += 1

    return media


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
        elif kind == "season_file":
            key = (
                "season_file",
                str(item.get("info_hash") or release_name),
                str(item.get("file_path") or path),
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


def _fresh_plugin_settings():
    try:
        from apps.plugins.models import PluginConfig
        config = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        if config:
            settings = getattr(config, "settings", {}) or {}
            return dict(settings) if isinstance(settings, dict) else {}
    except Exception:
        LOG.exception("Decypharr VOD: failed to read live plugin settings")
    return {}


def _media_state_key(item):
    return hashlib.sha1(
        json.dumps({
            "kind": item.get("kind"),
            "api_url": item.get("api_url") or "",
            "info_hash": item.get("info_hash") or "",
            "file_path": item.get("file_path") or "",
            "path": os.path.realpath(str(item.get("path") or "")),
        }, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _inventory_item_digest(item):
    row = {
        "kind": item.get("kind"),
        "path": os.path.realpath(str(item.get("path") or "")),
        "api_url": item.get("api_url") or "",
        "info_hash": item.get("info_hash") or "",
        "file_path": item.get("file_path") or "",
        "file_name": item.get("file_name") or "",
        "release_name": item.get("release_name") or "",
        "episode": list(item.get("episode") or []),
    }
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).digest()


def _inventory_signature_from_digests(digests):
    accumulator = bytearray(32)
    for digest in digests:
        for index, value in enumerate(digest):
            accumulator[index] ^= value
    return bytes(accumulator).hex()


METADATA_QUEUE_FILE = os.path.join(STATE_DIR, "metadata_queue.jsonl")


def _metadata_queue_reset():
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = METADATA_QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8"):
        pass
    os.replace(tmp, METADATA_QUEUE_FILE)


def _metadata_queue_append(item):
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = {k: item.get(k) for k in ("kind", "path", "api_url", "episode", "info_hash", "file_path", "release_name", "file_name", "season_pack")}
    with open(METADATA_QUEUE_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _metadata_queue_batch(offset=0, limit=10):
    items=[]
    try:
        with open(METADATA_QUEUE_FILE, encoding="utf-8") as handle:
            handle.seek(max(0, int(offset or 0)))
            while len(items) < max(1, int(limit or 1)):
                line=handle.readline()
                if not line: return items, handle.tell()
                try: item=json.loads(line)
                except Exception: continue
                if isinstance(item, dict) and item.get("api_url"): items.append(item)
            return items, handle.tell()
    except FileNotFoundError:
        return [], 0


def _metadata_queue_has_items(offset=0):
    try: return os.path.getsize(METADATA_QUEUE_FILE) > max(0, int(offset or 0))
    except OSError: return False


def _metadata_state_load():
    try:
        with open(METADATA_STATE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _metadata_state_save(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = METADATA_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, METADATA_STATE_FILE)


def _fast_import_item(account, item, lib, seen_files, seen_movies, seen_eps, seen_series, seen_series_relations):
    p = item["path"]
    api_media_url = item.get("api_url")
    if not api_media_url:
        return False

    epi = item.get("episode")
    if epi:
        season, number = epi
        release = _release_root(p, SOURCE_ROOT)
        series_name = _series_title_from_path(p) or _release_title(release.name)
        year = _year(str(release)) or _year(p)
        series = _find_series(series_name, year, None)
        if series is None:
            series = Series.objects.create(name=series_name, year=year, custom_properties={MARKER: True})

        rel, _ = M3USeriesRelation.objects.get_or_create(
            m3u_account=account,
            series=series,
            external_series_id="%s-series-%s" % (MARKER, series.id),
            defaults={"category": None, "custom_properties": {MARKER: True}},
        )
        props = dict(rel.custom_properties or {})
        props.update({
            MARKER: True,
            "decypharr_path": p,
            "decypharr_api_url": api_media_url,
            "decypharr_info_hash": item.get("info_hash"),
            "decypharr_file_path": item.get("file_path"),
            "fast_import": True,
        })
        rel.custom_properties = props
        rel.last_episode_refresh = timezone.now()
        rel.save(update_fields=["custom_properties", "last_episode_refresh"])
        seen_series.add(series.id)
        seen_series_relations.add(rel.id)

        ep = Episode.objects.filter(series=series, season_number=season, episode_number=number).first()
        if ep is None:
            ep = Episode.objects.create(
                series=series,
                season_number=season,
                episode_number=number,
                name=_episode_title(p) or "Episode %02d" % int(number),
                custom_properties={MARKER: True, "fast_import": True},
            )
        ec = dict(ep.custom_properties or {})
        ec.update({
            MARKER: True,
            "decypharr_path": p,
            "decypharr_api_url": api_media_url,
            "decypharr_info_hash": item.get("info_hash"),
            "decypharr_file_path": item.get("file_path"),
            "fast_import": True,
        })
        ep.custom_properties = ec
        ep.save(update_fields=["custom_properties"])

        stream_id = _canonical_episode_stream_id(series, season, number)
        er, _ = M3UEpisodeRelation.objects.get_or_create(
            m3u_account=account,
            stream_id=stream_id,
            defaults={
                "episode": ep,
                "series_relation": rel,
                "container_extension": Path(p).suffix.lstrip(".") or "mp4",
                "custom_properties": {MARKER: True},
            },
        )
        er.episode = ep
        er.series_relation = rel
        er.container_extension = Path(p).suffix.lstrip(".") or "mp4"
        ep_props = dict(er.custom_properties or {})
        ep_props.update({
            MARKER: True,
            "decypharr_path": p,
            "decypharr_api_url": api_media_url,
            "decypharr_info_hash": item.get("info_hash"),
            "decypharr_file_path": item.get("file_path"),
            "fast_import": True,
        })
        er.custom_properties = ep_props
        er.save()
        seen_eps.add(er.id)

        out_series = _safe(series.name + (" (%s)" % series.year if series.year else ""))
        out_name = _safe(
            "%s - S%02dE%02d - %s [Pending Metadata]" % (
                out_series, int(season), int(number),
                _episode_title(p) or "Episode %02d" % int(number),
            )
        )
        out = os.path.join(lib, "shows", out_series, "Season %02d" % int(season), out_name + ".strm")
        _make_strm(out, _play("episode", er.id, er.container_extension))
        seen_files.add(os.path.realpath(out))
        return True

    release = _release_root(p, SOURCE_ROOT)
    name = _release_title(release.name)
    year = _year(str(release)) or _year(p)
    if not name or _internal_stream_name(p):
        return False
    movie = _find_movie(name, year, None)
    if movie is None:
        movie = Movie.objects.create(name=name, year=year, custom_properties={MARKER: True, "fast_import": True})
    mr, _ = M3UMovieRelation.objects.get_or_create(
        m3u_account=account,
        stream_id=_canonical_movie_stream_id(movie),
        defaults={
            "movie": movie,
            "category": None,
            "container_extension": Path(p).suffix.lstrip(".") or "mp4",
            "custom_properties": {MARKER: True},
        },
    )
    mr.movie = movie
    mr.category = None
    mr.container_extension = Path(p).suffix.lstrip(".") or "mp4"
    props = dict(mr.custom_properties or {})
    props.update({
        MARKER: True,
        "decypharr_path": p,
        "decypharr_api_url": api_media_url,
        "decypharr_info_hash": item.get("info_hash"),
        "decypharr_file_path": item.get("file_path"),
        "fast_import": True,
    })
    mr.custom_properties = props
    mr.save()
    seen_movies.add(mr.id)

    movie_dir_name = _safe(name + (" (%s)" % year if year else ""))
    out_name = _safe("%s [Pending Metadata]" % movie_dir_name)
    out = os.path.join(lib, "movies", movie_dir_name, out_name + ".strm")
    _make_strm(out, _play("movie", mr.id, mr.container_extension))
    seen_files.add(os.path.realpath(out))
    return True


def _inventory_signature(media):
    rows = []
    for item in media:
        rows.append({
            "kind": item.get("kind"),
            "path": os.path.realpath(str(item.get("path") or "")),
            "api_url": item.get("api_url") or "",
            "info_hash": item.get("info_hash") or "",
            "file_path": item.get("file_path") or "",
            "file_name": item.get("file_name") or "",
            "release_name": item.get("release_name") or "",
            "episode": list(item.get("episode") or []),
        })
    rows.sort(key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
    raw = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stored_inventory_signature():
    try:
        with open(INVENTORY_STATE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("signature")
    except Exception:
        return None


def _store_inventory_signature(signature):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = INVENTORY_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"signature": signature, "updated": time.time()}, handle)
    os.replace(tmp, INVENTORY_STATE_FILE)


def _scan_process_lock():
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(SCAN_PROCESS_LOCK_PATH, "a+")
    if fcntl is None:
        return handle
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _scan_process_unlock(handle):
    if not handle:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _auto_scan_loop(plugin):
    LOG.info("Decypharr VOD: background auto-scan worker started.")
    while not AUTO_SCAN_STOP.is_set():
        close_old_connections()
        settings = _fresh_plugin_settings()
        if settings:
            plugin._settings = settings
        try:
            interval = float((settings or {}).get("scan_interval", 60) or 0)
        except (TypeError, ValueError):
            interval = 60

        if interval > 0:
            try:
                result = _scan(plugin, force=False, background=True)
                if result.get("status") not in ("ok", "unchanged", "busy"):
                    LOG.warning("Decypharr VOD: background scan returned %s", result)
            except Exception:
                LOG.exception("Decypharr VOD: background scan failed")
            finally:
                close_old_connections()
            wait_for = max(5, interval)
        else:
            wait_for = 30

        AUTO_SCAN_STOP.wait(wait_for)

    LOG.info("Decypharr VOD: background auto-scan worker stopped.")


def _start_auto_scan(plugin):
    global AUTO_SCAN_THREAD
    with AUTO_SCAN_START_LOCK:
        if AUTO_SCAN_THREAD is not None and AUTO_SCAN_THREAD.is_alive():
            return
        AUTO_SCAN_STOP.clear()
        AUTO_SCAN_THREAD = threading.Thread(
            target=_auto_scan_loop,
            args=(plugin,),
            name="decypharr-vod-auto-scan",
            daemon=True,
        )
        AUTO_SCAN_THREAD.start()


def _link_next_episode_metadata(account):
    """
    Store deterministic next-episode information on every Decypharr episode.

    Dispatcharr's current VOD frontend does not consume this field yet, but
    keeping it on the relation makes the ordering/playback contract available
    without changing stream IDs. Final episodes are explicitly marked so a
    future player can stop at the end of the season.
    """
    rels = list(
        M3UEpisodeRelation.objects.filter(
            m3u_account=account,
            series_relation__isnull=False,
        ).select_related("episode", "series_relation__series").order_by(
            "series_relation__series_id",
            "episode__season_number",
            "episode__episode_number",
            "id",
        )
    )
    grouped = {}
    for rel in rels:
        key = rel.series_relation.series_id if rel.series_relation else None
        if key is not None:
            grouped.setdefault(key, []).append(rel)

    changed = 0
    for items in grouped.values():
        for index, rel in enumerate(items):
            ep = rel.episode
            props = dict(rel.custom_properties or {})
            if index + 1 < len(items):
                nxt = items[index + 1]
                # Only auto-advance inside the same season. The end of a
                # season is a deliberate stopping point for this feature.
                if nxt.episode.season_number == ep.season_number:
                    props.update({
                        "decypharr_next_episode_id": nxt.id,
                        "decypharr_next_episode_uuid": str(nxt.episode.uuid),
                        "decypharr_next_season": int(nxt.episode.season_number),
                        "decypharr_next_episode": int(nxt.episode.episode_number),
                        "decypharr_season_final": False,
                    })
                else:
                    props.pop("decypharr_next_episode_id", None)
                    props.pop("decypharr_next_episode_uuid", None)
                    props.pop("decypharr_next_season", None)
                    props.pop("decypharr_next_episode", None)
                    props["decypharr_season_final"] = True
            else:
                props.pop("decypharr_next_episode_id", None)
                props.pop("decypharr_next_episode_uuid", None)
                props.pop("decypharr_next_season", None)
                props.pop("decypharr_next_episode", None)
                props["decypharr_season_final"] = True

            if props != (rel.custom_properties or {}):
                rel.custom_properties = props
                rel.save(update_fields=["custom_properties"])
                changed += 1
    return changed


def _scan(plugin, force=False, background=False, fast=False, enrich_only=False):
    if not SCAN_LOCK.acquire(blocking=False):
        return {"status": "busy"}

    process_lock = _scan_process_lock()
    if process_lock is None:
        SCAN_LOCK.release()
        return {"status": "busy"}

    try:
        settings = getattr(plugin, "_settings", {}) or {}
        global API_TOKEN, API_BASE_URL
        API_BASE_URL = (settings.get("api_url") or DEFAULT_API_URL).rstrip("/")
        API_TOKEN = settings.get("api_token") or ""
        cfg = {
            "root": SOURCE_ROOT,
            "library": settings.get("library_root") or LIBRARY_ROOT,
            "tmdb_key": settings.get("tmdb_api_key") or "",
            "metadata": settings.get("metadata_enabled", True),
            "ffprobe": settings.get("ffprobe_path") or "/usr/local/bin/ffprobe",
            "fast_initial": settings.get("fast_initial_scan", True),
            "metadata_batch_size": settings.get("metadata_batch_size", 10),
            "api_workers": settings.get("api_workers", API_WORKERS),
        }
        root = cfg["root"]
        lib = cfg["library"]

        if not API_TOKEN:
            return {"status": "error", "message": "Decypharr API token is required."}

        os.makedirs(root, exist_ok=True)
        os.makedirs(lib, exist_ok=True)

        account = _account()
        movie_cat = None
        tv_cat = None

        seen_movies = set()
        seen_eps = set()
        seen_series = set()
        seen_series_relations = set()
        seen_files = set()

        counts = {
            "movies": 0,
            "episodes": 0,
            "skipped": 0,
        }

        progressive_fast = bool(
            fast
            or (
                _stored_inventory_signature() is None
                and settings.get("fast_initial_scan", True)
            )
        )
        progressive_seen = {
            "movies": set(),
            "episodes": set(),
            "series": set(),
            "series_relations": set(),
            "files": set(),
        }

        # Progressive discovery is intentionally streaming. Do not retain
        # the entire library or a digest per item in RAM.
        progressive_accumulator = bytearray(32)
        if progressive_fast and not enrich_only:
            _metadata_queue_reset()

        def progressive_digest(digest):
            for index, value in enumerate(digest):
                progressive_accumulator[index] ^= value

        def progressive_batch(batch):
            if not progressive_fast or enrich_only:
                return
            batch = _dedupe_discovered_media(batch)
            for item in batch:
                if item.get("kind") == "season_file":
                    LOG.warning(
                        "Skipping unresolved season-pack item during progressive import: %s",
                        item.get("path"),
                    )
                    continue
                try:
                    ok = _fast_import_item(
                        account,
                        item,
                        lib,
                        progressive_seen["files"],
                        progressive_seen["movies"],
                        progressive_seen["episodes"],
                        progressive_seen["series"],
                        progressive_seen["series_relations"],
                    )
                    if ok:
                        _metadata_queue_append(item)
                    else:
                        LOG.warning(
                            "Progressive fast import skipped %s",
                            item.get("path"),
                        )
                except Exception:
                    LOG.exception(
                        "Progressive fast import failed for %s",
                        item.get("path"),
                    )

        media = _discover_media(
            root,
            API_BASE_URL,
            API_TOKEN,
            batch_callback=progressive_batch if progressive_fast else None,
            batch_size=max(
                25,
                min(
                    1000,
                    int(settings.get("progressive_batch_size", 200) or 200),
                ),
            ),
            collect=not progressive_fast,
            inventory_digest_callback=(
                progressive_digest if progressive_fast else None
            ),
        )

        if progressive_fast and not enrich_only:
            inventory_signature = bytes(progressive_accumulator).hex()

            _cleanup_library(lib, progressive_seen["files"])
            M3UMovieRelation.objects.filter(
                m3u_account=account
            ).exclude(id__in=progressive_seen["movies"]).delete()
            M3UEpisodeRelation.objects.filter(
                m3u_account=account
            ).exclude(id__in=progressive_seen["episodes"]).delete()
            M3USeriesRelation.objects.filter(
                m3u_account=account
            ).exclude(id__in=progressive_seen["series_relations"]).delete()

            _cleanup_legacy_categories(account)

            metadata_state = {
                "inventory_signature": inventory_signature,
                "pending": [],
                "pending_queue": "metadata_queue.jsonl",
                "queue_offset": 0,
                "updated": time.time(),
            }
            _metadata_state_save(metadata_state)
            _link_next_episode_metadata(account)
            _store_inventory_signature(inventory_signature)

            return {
                "status": "ok",
                "mode": "fast_initial_progressive",
                "message": (
                    "Fast initial catalog is ready; metadata enrichment "
                    "is queued in bounded batches."
                ),
                "movies": len(progressive_seen["movies"]),
                "episodes": len(progressive_seen["episodes"]),
                "discovered": (
                    len(progressive_seen["movies"])
                    + len(progressive_seen["episodes"])
                ),
            }

        media = _dedupe_discovered_media(media)
        inventory_signature = _inventory_signature(media)
        metadata_state = _metadata_state_load()
        first_inventory = _stored_inventory_signature() is None

        if background and not force and inventory_signature == _stored_inventory_signature():
            queue_offset = int(metadata_state.get("queue_offset") or 0)
            if not (metadata_state.get("pending") or []) and not _metadata_queue_has_items(queue_offset):
                return {
                    "status": "unchanged",
                    "message": "No Decypharr media changes detected; scan skipped.",
                    "discovered": len(media),
                }
            enrich_only = True

        fast = bool(fast or (first_inventory and cfg.get("fast_initial", True)))
        queue_items = []
        queue_next_offset = int(metadata_state.get("queue_offset") or 0)
        if enrich_only:
            batch_size = max(1, int(cfg.get("metadata_batch_size", 25) or 25))
            if _metadata_queue_has_items(queue_next_offset):
                queue_items, queue_next_offset = _metadata_queue_batch(queue_next_offset, batch_size)
                media = queue_items
            else:
                pending = set(metadata_state.get("pending") or [])
                if not pending:
                    return {"status": "unchanged", "message": "Metadata enrichment is complete."}
                selected = []
                for item in media:
                    if _media_state_key(item) in pending and len(selected) < batch_size:
                        selected.append(item)
                media = selected
            LOG.info("Decypharr VOD: metadata enrichment batch %d", len(media))

        # Season packs often contain obfuscated child filenames. Assign
        # deterministic provisional episode numbers; embedded FFprobe
        # titles remain authoritative when available.
        media = _normalize_season_pack_items(media)

        LOG.info(
            "Decypharr discovery found %d logical media objects after dedupe",
            len(media),
        )

        for item in media:
            p = item["path"]
            api_media_url = item.get("api_url")

            if not api_media_url:
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

                    release_files = []

                    # Always derive the initial candidate from the
                    # source path.  _series_title_from_path() handles
                    # normal SxxEyy files AND Blu-ray release directories.
                    series_name = _series_title_from_path(p)

                    if not series_name:
                        series_name = _release_title(release.name)

                    # Obfuscated season-pack members may carry the real
                    # SxxExx identity in the container title.
                    probe = None
                    if item.get("season_pack"):
                        probe = _ffprobe(api_media_url, cfg["ffprobe"])
                        embedded = _parse_episode_structure(
                            str((probe or {}).get("title") or "")
                        )
                        if embedded:
                            _, embedded_season, embedded_number = embedded
                            season = embedded_season
                            number = embedded_number

                    year = _year(str(release)) or _year(p)

                    data = (
                        _tmdb_tv(cfg["tmdb_key"], series_name, year)
                        if cfg["metadata"]
                        else None
                    )

                    series_genre_categories = _genre_categories(account, data, "series")

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
                            "category": (
                                series_genre_categories[0][1]
                                if series_genre_categories
                                else None
                            ),
                            "custom_properties": {MARKER: True},
                        },
                    )

                    rel.category = (
                        series_genre_categories[0][1]
                        if series_genre_categories
                        else None
                    )
                    rel.custom_properties = dict(rel.custom_properties or {})
                    rel.custom_properties.update({
                        MARKER: True,
                        "decypharr_path": p,
                        "decypharr_api_url": api_media_url,
                        "decypharr_info_hash": item.get("info_hash"),
                        "decypharr_file_path": item.get("file_path"),
                        "episodes_fetched": True, "detailed_fetched": True,
                    })
                    rel.last_episode_refresh = timezone.now()
                    rel.save()

                    series_relations = _sync_series_genre_relations(
                        account, series, rel, series_genre_categories
                    )
                    seen_series.add(series.id)
                    seen_series_relations.update(srel.id for _, srel in series_relations)

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

                    if probe is None:
                        probe = _ffprobe(api_media_url, cfg["ffprobe"])

                    ec.update({
                        "decypharr_path": p,
                        "decypharr_api_url": api_media_url,
                        "decypharr_info_hash": item.get("info_hash"),
                        "decypharr_file_path": item.get("file_path"),
                        "episodes_fetched": True, "detailed_fetched": True,
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
                        "decypharr_path": p,
                        "decypharr_api_url": api_media_url,
                        "decypharr_info_hash": item.get("info_hash"),
                        "decypharr_file_path": item.get("file_path"),
                        "episodes_fetched": True, "detailed_fetched": True,
                        "ffprobe": probe,
                    })

                    er.save()

                    _sync_episode_genre_relations(
                        account,
                        ep,
                        er,
                        series_relations,
                        p,
                        api_media_url,
                        item,
                        probe,
                        season,
                        number,
                        seen_eps,
                    )

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
                            _release_source_hint(item.get("release_name")) or source_hint,
                            item.get("release_name"),
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
                    if enrich_only:
                        pending = set(metadata_state.get("pending") or [])
                        pending.discard(_media_state_key(item))
                        metadata_state["pending"] = list(pending)

                # ========================================================
                # MOVIE
                # ========================================================
                else:
                    # A TV-looking release whose season/episode identity is
                    # still unresolved must never be silently converted into
                    # a movie.  Leave it pending for a later enrichment pass.
                    if item.get("kind") == "season_file":
                        counts["skipped"] += 1
                        LOG.warning(
                            "Deferring unresolved TV/season-pack item instead of importing as movie: %s",
                            p,
                        )
                        continue

                    release = _release_root(p, root)

                    release_files = []

                    is_blu = (
                        Path(p).suffix.lower() == ".m2ts"
                        or _is_blu_ray_release(
                            str(release),
                            release_files,
                        )
                    )

                    # Release name is authoritative; child filenames may be obfuscated.
                    title_source = release.name
                    name = _release_title(title_source)

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

                    movie_genre_categories = _genre_categories(account, data, "movie")

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
                    probe = _ffprobe(api_media_url, cfg["ffprobe"])

                    # ----------------------------------------------------
                    # ONE relation per canonical movie
                    # ----------------------------------------------------
                    stream_id = _canonical_movie_stream_id(movie)

                    mr, _ = M3UMovieRelation.objects.get_or_create(
                        m3u_account=account,
                        stream_id=stream_id,
                        defaults={
                            "movie": movie,
                            "category": (
                                movie_genre_categories[0][1]
                                if movie_genre_categories
                                else None
                            ),
                            "container_extension": (
                                Path(p).suffix.lstrip(".") or "mp4"
                            ),
                            "custom_properties": {MARKER: True},
                        },
                    )

                    mr.movie = movie
                    mr.category = (
                        movie_genre_categories[0][1]
                        if movie_genre_categories
                        else None
                    )
                    mr.container_extension = (
                        Path(p).suffix.lstrip(".") or "mp4"
                    )

                    mr.custom_properties = dict(
                        mr.custom_properties or {}
                    )

                    mr.custom_properties.update({
                        MARKER: True,
                        "decypharr_path": p,
                        "decypharr_api_url": api_media_url,
                        "decypharr_info_hash": item.get("info_hash"),
                        "decypharr_file_path": item.get("file_path"),
                        "episodes_fetched": True, "detailed_fetched": True,
                        "ffprobe": probe,
                    })

                    mr.save()

                    movie_relation_ids = _sync_movie_genre_relations(
                        account, movie, mr, movie_genre_categories
                    )

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
                        _release_source_hint(item.get("release_name"))
                        or ("bluray" if is_blu else None)
                    )
                    movie._decypharr_release_name = item.get("release_name") or ""

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
                    seen_movies.update(movie_relation_ids)

                    counts["movies"] += 1
                    if enrich_only:
                        pending = set(metadata_state.get("pending") or [])
                        pending.discard(_media_state_key(item))
                        metadata_state["pending"] = list(pending)

            except Exception:
                counts["skipped"] += 1
                LOG.exception(
                    "Failed processing %s",
                    p,
                )

        if enrich_only:
            if queue_items:
                metadata_state["queue_offset"] = queue_next_offset
                metadata_state["queue_updated"] = time.time()
            metadata_state["inventory_signature"] = inventory_signature
            metadata_state["updated"] = time.time()
            _metadata_state_save(metadata_state)
            remaining_queue = 0
            try:
                remaining_queue = max(0, os.path.getsize(METADATA_QUEUE_FILE) - int(metadata_state.get("queue_offset") or 0))
            except OSError:
                pass
            return {
                "status": "ok",
                "mode": "metadata_enrichment",
                "movies": counts["movies"],
                "episodes": counts["episodes"],
                "skipped": counts["skipped"],
                "remaining": len(metadata_state.get("pending") or []),
                "queue_bytes_remaining": remaining_queue,
            }

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

        M3USeriesRelation.objects.filter(
            m3u_account=account,
        ).exclude(
            id__in=seen_series_relations,
        ).delete()

        _cleanup_legacy_categories(account)

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

        _store_inventory_signature(inventory_signature)

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
        _scan_process_unlock(process_lock)
        SCAN_LOCK.release()


# ================================================================
# Browser transcoding (optional, OFF by default)
#
# Dispatcharr's web player is a browser <video> element, which cannot
# decode HEVC / HDR10 remuxes. When "Browser Transcoding" is enabled,
# requests made by that player for Decypharr-owned content are answered
# with a live H.264/AAC fragmented-MP4 transcode. Every other client
# (VLC, Emby, Jellyfin, TiviMate, Kodi, ...) is passed through untouched.
#
# The transcoder reads its source through Dispatcharr's own
# /proxy/vod/ endpoint, so the Decypharr API token never appears in a
# process command line. Any failure falls back to normal passthrough.
# ================================================================
TX_MARK = "DecypharrVODTx"
TX_LOCAL_BASE = "http://127.0.0.1:9191"
TX_ORDER = ("nvenc", "qsv", "vaapi", "cpu")
TX_CACHE_TTL = 3600
TX_CFG_TTL = 5
TX_START_TIMEOUT = 60
TX_UA = "DecypharrVOD-Transcode"
TX_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
TX_SAFE_VIDEO = {"h264", "vp8", "vp9", "av1"}
TX_SAFE_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
TX_PROBE_TTL = 600
TX_TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)
TX_NON_BROWSER = re.compile(
    r"(vlc|lavf|ffmpeg|kodi|tivimate|emby|jellyfin|plex|infuse|exoplayer|okhttp|dalvik|curl|wget|python|decypharrvod)",
    re.I,
)
_TX_LOCK = threading.Lock()
_TX_CACHE = {}
_TX_CFG = {"ts": 0.0, "cfg": None}
_TX_PATCHED = False
_TX_PROBE_CACHE = {}
_TX_PROBE_LOCK = threading.Lock()


def _tx_bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _tx_settings(force=False):
    """Current transcode settings, read live from the plugin config (5s cache)."""
    now = time.time()
    if not force and _TX_CFG["cfg"] is not None and now - _TX_CFG["ts"] < TX_CFG_TTL:
        return _TX_CFG["cfg"]
    raw = {}
    try:
        from apps.plugins.models import PluginConfig

        config = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        if config:
            raw = getattr(config, "settings", {}) or {}
    except Exception:
        LOG.exception("Decypharr VOD: could not read transcode settings")
    try:
        max_streams = int(raw.get("transcode_max_streams") or 2)
    except (TypeError, ValueError):
        max_streams = 2
    encoder = str(raw.get("transcode_encoder") or "auto").strip().lower()
    if encoder not in ("auto",) + TX_ORDER:
        encoder = "auto"
    cfg = {
        "enabled": _tx_bool(raw.get("browser_transcode"), False),
        "encoder": encoder,
        "vaapi_device": str(raw.get("vaapi_device") or "/dev/dri/renderD128").strip(),
        "ffmpeg_path": str(raw.get("ffmpeg_path") or "").strip(),
        "ffprobe_path": str(raw.get("ffprobe_path") or "").strip(),
        "max_streams": max(1, max_streams),
        "preferred_audio_language": _preferred_audio_code(raw),
    }
    _TX_CFG["ts"] = now
    _TX_CFG["cfg"] = cfg
    return cfg


def _tx_find_binary(name, configured, sibling_of=None):
    candidates = []
    if configured:
        candidates.append(configured)
    if sibling_of:
        candidates.append(os.path.join(os.path.dirname(sibling_of), name))
    found = shutil.which(name)
    if found:
        candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _tx_find_ffmpeg(cfg):
    return _tx_find_binary("ffmpeg", cfg.get("ffmpeg_path"), cfg.get("ffprobe_path"))


def _tx_find_ffprobe(cfg, ffmpeg):
    return _tx_find_binary("ffprobe", cfg.get("ffprobe_path"), ffmpeg)


def _tx_encoder_parts(encoder, vaapi_device):
    """Return (global_args, filter_suffix, codec_args) for an encoder."""
    rate = ["-b:v", "8M", "-maxrate", "12M", "-bufsize", "24M", "-g", "48"]
    if encoder == "nvenc":
        return [], "", ["-c:v", "h264_nvenc"] + rate
    if encoder == "qsv":
        return (
            [
                "-init_hw_device", "vaapi=va:%s" % vaapi_device,
                "-init_hw_device", "qsv=hw@va",
                "-filter_hw_device", "hw",
            ],
            ",format=nv12,hwupload=extra_hw_frames=64",
            ["-c:v", "h264_qsv"] + rate,
        )
    if encoder == "vaapi":
        return (
            ["-vaapi_device", vaapi_device],
            ",format=nv12,hwupload",
            ["-c:v", "h264_vaapi"] + rate,
        )
    threads = max(2, (os.cpu_count() or 4) // 2)
    return (
        [],
        "",
        [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-maxrate", "12M", "-bufsize", "24M", "-g", "48", "-keyint_min", "48",
            "-sc_threshold", "0", "-threads", str(threads),
        ],
    )


TX_GENERIC_ERR = (
    "nothing was written", "conversion failed", "terminating thread", "task finished",
    "error sending frames", "could not open encoder before eof", "error while opening encoder",
    "error while filtering", "error initializing output",
)
TX_HINT_ERR = ("cuda", "nvenc", "device", "permission", "cannot", "can't", "not found", "no such", "unknown encoder", "driver", "vaapi", "qsv", "libva")


def _tx_reason(stderr, returncode=None):
    """Pick the line of ffmpeg output that explains why an encoder failed."""
    lines = []
    for raw in (stderr or "").splitlines():
        line = re.sub(r"^\[[^\]]*\]\s*", "", raw.strip())
        if line and not any(g in line.lower() for g in TX_GENERIC_ERR):
            lines.append(line)
    hinted = [ln for ln in lines if any(h in ln.lower() for h in TX_HINT_ERR)]
    picked = (hinted or lines or ["exit code %s" % returncode])[0]
    return picked[:220]


def _tx_smoke(ffmpeg, encoder, vaapi_device):
    """Run a 1-second synthetic encode. Returns (ok, reason)."""
    pre, suffix, codec = _tx_encoder_parts(encoder, vaapi_device)
    cmd = (
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        + pre
        + ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "1", "-vf", "format=yuv420p" + suffix]
        + codec
        + ["-f", "null", "-"]
    )
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    return False, _tx_reason(proc.stderr, proc.returncode)


def _tx_detect(cfg, ffmpeg, force=False):
    """Pick the encoder to use. Returns {"encoder", "results", "ts"} (cached)."""
    key = (ffmpeg, cfg["encoder"], cfg["vaapi_device"])
    with _TX_LOCK:
        hit = _TX_CACHE.get(key)
        if hit and not force and time.time() - hit["ts"] < hit.get("ttl", TX_CACHE_TTL):
            return hit
        wanted = cfg["encoder"]
        order = list(TX_ORDER) if wanted == "auto" else [wanted]
        if "cpu" not in order:
            order.append("cpu")
        results = {}
        chosen = None
        for encoder in order:
            ok, why = _tx_smoke(ffmpeg, encoder, cfg["vaapi_device"])
            results[encoder] = "ok" if ok else why
            if ok:
                chosen = encoder
                break
        if wanted not in ("auto", chosen) and chosen:
            LOG.warning(
                "Decypharr VOD: selected encoder '%s' is not usable (%s); using '%s'",
                wanted, results.get(wanted), chosen,
            )
        LOG.info("Decypharr VOD: transcode encoder = %s (tests: %s)", chosen, results)
        # A CPU fallback is re-checked every minute so a recovered GPU is picked up quickly.
        ttl = TX_CACHE_TTL if chosen and chosen != "cpu" else 60
        entry = {"encoder": chosen, "results": results, "ts": time.time(), "ttl": ttl}
        _TX_CACHE[key] = entry
        return entry


def _tx_active_count():
    """Number of running plugin transcodes in this container (all workers)."""
    marker = TX_MARK.encode()
    count = 0
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as handle:
                    cmdline = handle.read()
            except Exception:
                continue
            if marker in cmdline and b"ffmpeg" in cmdline:
                count += 1
    except Exception:
        return 0
    return count


def _tx_probe(ffprobe, url):
    """Probe the source and retain every audio track for preferred-language selection."""
    cmd = [
        ffprobe, "-hide_banner", "-v", "error", "-user_agent", TX_UA,
        "-show_entries",
        "stream=index,codec_type,codec_name,profile,pix_fmt,color_transfer:stream_tags=language,title:stream_disposition=default,forced,original,dub:format=format_name",
        "-of", "json", url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout or "{}")
    except Exception:
        LOG.exception("Decypharr VOD: transcode probe failed")
        return None
    streams = data.get("streams") or []
    video = next(
        (s for s in streams
         if s.get("codec_type") == "video" and s.get("codec_name") not in ("mjpeg", "png", "bmp")),
        None,
    )
    if not video:
        return None
    audio_tracks = [s for s in streams if s.get("codec_type") == "audio"]
    for track in audio_tracks:
        tags = track.get("tags") or {}
        track["language"] = tags.get("language")
        track["title"] = tags.get("title")
        track["default"] = bool((track.get("disposition") or {}).get("default"))
        track["forced"] = bool((track.get("disposition") or {}).get("forced"))
    preferred = _preferred_audio_stream(audio_tracks, _preferred_audio_code(_fresh_plugin_settings()))
    return {
        "video": video,
        "audio": preferred,
        "audio_tracks": audio_tracks,
        "preferred_audio": preferred,
        "format": (data.get("format") or {}).get("format_name", ""),
    }


def _tx_probe_cached(ffprobe, url, cache_key):
    """Probe once per title (10 minute cache); serialised so a request burst probes once."""
    with _TX_PROBE_LOCK:
        hit = _TX_PROBE_CACHE.get(cache_key)
        if hit and time.time() - hit[0] < TX_PROBE_TTL:
            return hit[1], True
        info = _tx_probe(ffprobe, url)
        if info:
            if len(_TX_PROBE_CACHE) > 500:
                _TX_PROBE_CACHE.clear()
            _TX_PROBE_CACHE[cache_key] = (time.time(), info)
        return info, False


def _tx_needs_transcode(info, agent):
    """Decide whether a browser cannot play this file as-is. Returns (bool, reason)."""
    video = info["video"]
    audio = info.get("audio")
    vcodec = (video.get("codec_name") or "").lower()
    acodec = ((audio or {}).get("codec_name") or "").lower()
    if vcodec not in TX_SAFE_VIDEO:
        return True, "video codec %s" % (vcodec or "unknown")
    if str(video.get("color_transfer") or "").lower() in TX_HDR_TRANSFERS:
        return True, "HDR video"
    if vcodec == "h264" and (video.get("pix_fmt") or "yuv420p") not in ("yuv420p", "yuvj420p"):
        return True, "h264 %s" % video.get("pix_fmt")
    if audio and acodec not in TX_SAFE_AUDIO:
        return True, "audio codec %s" % (acodec or "unknown")
    tokens = set((info.get("format") or "").lower().split(","))
    if tokens & {"mov", "mp4"} and "matroska" not in tokens:
        return False, "browser-compatible"
    if tokens & {"matroska", "webm"}:
        if any(t in (agent or "") for t in ("Chrome/", "Chromium/", "Edg/")):
            return False, "browser-compatible"
        if vcodec in ("vp8", "vp9", "av1") and acodec in ("opus", "vorbis", ""):
            return False, "browser-compatible"
        return True, "matroska container in this browser"
    return True, "container %s" % ((info.get("format") or "unknown").split(",")[0])


def _tx_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    raw = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    return re.sub(r"[^0-9a-fA-F:.]", "", raw) or "unknown"


def _tx_read_cmdline(pid):
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as handle:
            return handle.read()
    except Exception:
        return b""


def _tx_kill_matching(tag):
    """Kill running transcodes carrying this tag, in any worker. Returns how many."""
    import signal

    needle = tag.encode()
    killed = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == os.getpid():
                continue
            cmdline = _tx_read_cmdline(pid)
            if needle in cmdline and b"ffmpeg" in cmdline:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    killed.append(pid)
                except Exception:
                    pass
    except Exception:
        return 0
    total = len(killed)
    deadline = time.time() + 3.0
    while killed and time.time() < deadline:
        killed = [p for p in killed if needle in _tx_read_cmdline(p)]
        if killed:
            time.sleep(0.05)
    return total


def _tx_audio_reorder_needed(info, preferred):
    """Return True when browser playback would expose the wrong default/first audio track."""
    tracks = list(info.get("audio_tracks") or [])
    if len(tracks) < 2 or not preferred:
        return False
    preferred_index = preferred.get("index")
    first = tracks[0]
    if preferred_index != first.get("index"):
        return True
    return not bool(first.get("default"))


def _tx_build_remux_cmd(ffmpeg, src_url, audio_tracks, preferred_index, tag=TX_MARK):
    """Remux without re-encoding, putting the preferred audio track first/default."""
    ordered = []
    for track in audio_tracks:
        if track.get("index") == preferred_index:
            ordered.insert(0, track)
        else:
            ordered.append(track)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-user_agent", TX_UA, "-i", src_url,
        "-map", "0:v:0",
    ]
    for track in ordered:
        cmd += ["-map", "0:%s?" % int(track["index"])]
    cmd += [
        "-c:v", "copy", "-c:a", "copy",
        "-disposition:a:0", "default",
    ]
    for pos in range(1, len(ordered)):
        cmd += ["-disposition:a:%d" % pos, "0"]
    cmd += [
        "-metadata", "comment=" + tag,
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    return cmd


def _tx_build_cmd(ffmpeg, encoder, cfg, src_url, hdr, audio_index=None, tag=TX_MARK):
    pre, suffix, codec = _tx_encoder_parts(encoder, cfg["vaapi_device"])
    vf = ",".join(["scale=-2:'min(1080,ih)'", TX_TONEMAP if hdr else "format=yuv420p"]) + suffix
    audio_map = "0:a:0?" if audio_index is None else "0:%s?" % int(audio_index)
    return (
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        + pre
        + ["-user_agent", TX_UA, "-i", src_url, "-map", "0:v:0", "-map", audio_map, "-sn", "-dn", "-vf", vf]
        + codec
        + [
            "-c:a", "aac", "-ac", "2", "-b:a", "192k",
            "-disposition:a:0", "default",
            "-max_muxing_queue_size", "1024",
            "-metadata", "comment=" + tag,
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
    )


def _tx_kill(proc):
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def _tx_spawn(cmd):
    """Start ffmpeg and wait for its first output. Returns (proc, first, error_tail)."""
    import collections

    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
    )
    tail = collections.deque(maxlen=25)

    def drain():
        try:
            for line in iter(proc.stderr.readline, b""):
                tail.append(line.decode("utf-8", "replace").rstrip())
        except Exception:
            pass

    threading.Thread(target=drain, daemon=True).start()
    timer = threading.Timer(TX_START_TIMEOUT, lambda: _tx_kill(proc))
    timer.daemon = True
    timer.start()
    try:
        first = proc.stdout.read(65536)
    finally:
        timer.cancel()
    if not first:
        _tx_kill(proc)
        return None, None, " | ".join(list(tail)[-6:])
    return proc, first, ""


def _tx_iter(proc, first):
    try:
        yield first
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            yield chunk
    finally:
        _tx_kill(proc)
        try:
            proc.stdout.close()
        except Exception:
            pass


def _tx_wants(request):
    """True for requests made by a browser video element (Dispatcharr web player)."""
    flag = request.GET.get("transcode")
    if flag == "0":
        return False
    if flag == "1":
        return True
    agent = request.META.get("HTTP_USER_AGENT", "") or ""
    if "Mozilla/5.0" not in agent or TX_NON_BROWSER.search(agent):
        return False
    referer = request.META.get("HTTP_REFERER", "") or ""
    fetch_dest = request.META.get("HTTP_SEC_FETCH_DEST", "") or ""
    return "/vods" in referer or fetch_dest == "video"


def _tx_owned(content_type, content_id, stream_id):
    """True when the requested content belongs to this plugin's synthetic account."""
    try:
        if content_type == "movie":
            qs = M3UMovieRelation.objects.filter(m3u_account__name=ACCOUNT_NAME)
            if stream_id and str(stream_id).startswith(PREFIX):
                return qs.filter(stream_id=stream_id).exists()
            return qs.filter(movie__uuid=content_id).exists()
        if content_type == "episode":
            qs = M3UEpisodeRelation.objects.filter(m3u_account__name=ACCOUNT_NAME)
            if stream_id and str(stream_id).startswith(PREFIX):
                return qs.filter(stream_id=stream_id).exists()
            return qs.filter(episode__uuid=content_id).exists()
    except Exception:
        LOG.exception("Decypharr VOD: transcode ownership check failed")
    return False


def _tx_maybe(request, kwargs):
    """Return a transcoded response, or None to use normal passthrough."""
    if request.method != "GET":
        return None
    cfg = _tx_settings()
    if not cfg["enabled"] or not _tx_wants(request):
        return None
    content_type = kwargs.get("content_type")
    content_id = kwargs.get("content_id")
    if content_type not in ("movie", "episode") or not content_id:
        return None
    stream_id = request.GET.get("stream_id") or ""
    if not _tx_owned(content_type, content_id, stream_id):
        return None

    ffmpeg = _tx_find_ffmpeg(cfg)
    if not ffmpeg:
        LOG.warning("Decypharr VOD: browser transcoding is enabled but ffmpeg was not found; using passthrough")
        return None

    # Same access rules as Dispatcharr's own stream_vod view.
    from django.http import JsonResponse
    from apps.proxy.vod_proxy import views as vod_views

    allowed = getattr(vod_views, "network_access_allowed", None)
    if allowed and not allowed(request, "STREAMS"):
        return JsonResponse({"error": "Forbidden"}, status=403)
    playback = getattr(vod_views, "_vod_playback_allowed", None)
    user_of = getattr(vod_views, "_user_from_vod_request", None)
    if playback and not playback(content_type, user_of(request) if user_of else None):
        return JsonResponse({"error": "Forbidden"}, status=403)

    import uuid

    params = {"transcode": "0"}
    for name in ("stream_id", "m3u_account_id"):
        if request.GET.get(name):
            params[name] = request.GET.get(name)

    def source_url(prefix):
        session = "%s_%d_%s" % (prefix, int(time.time() * 1000), uuid.uuid4().hex[:6])
        return "%s/proxy/vod/%s/%s/%s?%s" % (
            TX_LOCAL_BASE, content_type, content_id, session, urllib.parse.urlencode(params)
        )

    # 1. Transcode only what a browser cannot play as-is.
    ffprobe = _tx_find_ffprobe(cfg, ffmpeg)
    if not ffprobe:
        LOG.warning("Decypharr VOD: ffprobe not found; browser transcoding skipped")
        return None
    agent = request.META.get("HTTP_USER_AGENT", "") or ""
    info, cached = _tx_probe_cached(ffprobe, source_url("vodtxp"), (content_type, str(content_id), stream_id))
    if not info:
        LOG.warning("Decypharr VOD: could not probe source for transcoding; using passthrough")
        return None
    needs, reason = _tx_needs_transcode(info, agent)
    preferred_audio = _preferred_audio_stream(
        info.get("audio_tracks") or ([info.get("audio")] if info.get("audio") else []),
        cfg.get("preferred_audio_language") or "eng",
    )
    # Browser-compatible files still need a lightweight remux when the selected
    # language is not already the first/default audio stream. This changes no
    # codecs and keeps every source audio track available.
    if not needs and _tx_audio_reorder_needed(info, preferred_audio):
        audio_tracks = list(info.get("audio_tracks") or [])
        if audio_tracks and preferred_audio and preferred_audio.get("index") is not None:
            tag = "%s|%s|%s|" % (TX_MARK, _tx_client_ip(request), content_id)
            src_url = source_url("vodremux")
            remux_cmd = _tx_build_remux_cmd(
                ffmpeg, src_url, audio_tracks, preferred_audio.get("index"), tag
            )
            proc, first, err = _tx_spawn(remux_cmd)
            if proc:
                LOG.info(
                    "Decypharr VOD: browser audio remux started (preferred=%s, source_tracks=%d)",
                    preferred_audio.get("language"), len(audio_tracks),
                )
                response = StreamingHttpResponse(_tx_iter(proc, first), status=200, content_type="video/mp4")
                response["Accept-Ranges"] = "none"
                response["Cache-Control"] = "no-store"
                response["X-Accel-Buffering"] = "no"
                return response
            LOG.warning("Decypharr VOD: preferred-audio remux failed; using native passthrough: %s", err)
        if not cached:
            LOG.warning("Decypharr VOD: preferred audio could not be remuxed; using native passthrough")
    if not needs:
        if not cached:
            LOG.info(
                "Decypharr VOD: %s plays natively in the browser (%s/%s); passthrough",
                content_type, info["video"].get("codec_name"), (info.get("audio") or {}).get("codec_name"),
            )
        return None
    hdr = str(info["video"].get("color_transfer") or "").lower() in TX_HDR_TRANSFERS

    detected = _tx_detect(cfg, ffmpeg)
    if not detected.get("encoder"):
        LOG.error("Decypharr VOD: no working encoder found (%s); using passthrough", detected.get("results"))
        return None

    # 2. Newest request wins. A browser opens overlapping connections for one
    #    file (open burst, seek); replace this client's older transcode of the
    #    same title instead of stacking them.
    tag = "%s|%s|%s|" % (TX_MARK, _tx_client_ip(request), content_id)
    if _tx_kill_matching(tag):
        LOG.info("Decypharr VOD: replaced an earlier transcode of the same title for this client")

    if _tx_active_count() >= cfg["max_streams"]:
        LOG.warning("Decypharr VOD: transcode limit (%s) reached", cfg["max_streams"])
        return HttpResponse("Transcode capacity reached. Try again shortly.", status=503)

    src_url = source_url("vodtx")
    LOG.info("Decypharr VOD: transcoding needed (%s)", reason)

    encoders = [detected["encoder"]] + (["cpu"] if detected["encoder"] != "cpu" else [])
    attempts = [(encoder, hdr) for encoder in encoders]
    if hdr:
        # Last resort for ffmpeg builds without the tone-mapping filters:
        # washed-out colours are better than no picture.
        attempts.append((encoders[-1], False))
    preferred_audio_index = preferred_audio.get("index") if preferred_audio else None
    for encoder, use_tonemap in attempts:
        cmd = _tx_build_cmd(ffmpeg, encoder, cfg, src_url, use_tonemap, preferred_audio_index, tag)
        proc, first, err = _tx_spawn(cmd)
        if proc:
            LOG.info(
                "Decypharr VOD: browser transcode started (%s, %s, %s)",
                encoder, "HDR->SDR tonemap" if use_tonemap else ("HDR without tonemap" if hdr else "SDR"), content_type,
            )
            response = StreamingHttpResponse(_tx_iter(proc, first), status=200, content_type="video/mp4")
            response["Accept-Ranges"] = "none"
            response["Cache-Control"] = "no-store"
            response["X-Accel-Buffering"] = "no"
            return response
        LOG.error("Decypharr VOD: %s transcode produced no output: %s", encoder, err)
        if encoder != "cpu":
            _TX_CACHE.clear()  # re-detect on the next request instead of trusting a dead GPU
    return None


def _tx_wrap(original):
    import functools
    from django.views.decorators.csrf import csrf_exempt

    @csrf_exempt
    @functools.wraps(original)
    def decypharr_stream_vod(request, *args, **kwargs):
        try:
            response = _tx_maybe(request, kwargs)
            if response is not None:
                return response
        except Exception:
            LOG.exception("Decypharr VOD: transcode path failed; using passthrough")
        return original(request, *args, **kwargs)

    decypharr_stream_vod._decypharr_tx_wrapper = True
    decypharr_stream_vod._decypharr_original = original
    return decypharr_stream_vod


def _patch_transcode():
    """Wrap Dispatcharr's stream_vod URL callbacks. Never raises."""
    global _TX_PATCHED
    try:
        from apps.proxy.vod_proxy import views as vod_views
        from apps.proxy.vod_proxy import urls as vod_urls

        original = getattr(vod_views, "stream_vod", None)
        wrapped = 0
        for pattern in getattr(vod_urls, "urlpatterns", []):
            callback = getattr(pattern, "callback", None)
            if callback is None:
                continue
            if getattr(callback, "_decypharr_tx_wrapper", False):
                wrapped += 1
            elif original is not None and callback is original:
                pattern.callback = _tx_wrap(original)
                wrapped += 1
        _TX_PATCHED = wrapped > 0
        if _TX_PATCHED:
            LOG.info("Decypharr VOD: browser transcode hook installed on %d route(s)", wrapped)
        else:
            LOG.warning("Decypharr VOD: stream_vod routes not found; browser transcoding unavailable")
    except Exception:
        _TX_PATCHED = False
        LOG.exception("Decypharr VOD: could not install browser transcode hook")
    return _TX_PATCHED


def _cleanup_vod_database():
    """
    Destructive VOD reset for the plugin Actions tab.

    Use Django's ORM instead of hard-coded SQL table names.  Dispatcharr's
    VOD app has changed table naming across releases, while the model
    classes remain the stable interface.  Relations are deleted first so
    foreign-key rows cannot survive the reset.  M3UAccount rows are
    intentionally preserved.
    """
    deleted = {}
    try:
        with transaction.atomic():
            # Delete relation rows first.
            relation_models = (
                ("Episode relations", M3UEpisodeRelation),
                ("Movie relations", M3UMovieRelation),
                ("Series relations", M3USeriesRelation),
                ("Category relations", M3UVODCategoryRelation),
            )
            object_models = (
                ("Episodes", Episode),
                ("Movies", Movie),
                ("Series", Series),
                ("Categories", VODCategory),
                ("Logos", VODLogo),
            )

            for label, model in relation_models + object_models:
                count, _ = model.objects.all().delete()
                deleted[label] = count
                LOG.info(
                    "Decypharr VOD cleanup: %s deleted %d rows",
                    label,
                    count,
                )

            # Make sure the transaction is actually visible before reporting
            # success.  Use the same ORM models that Dispatcharr uses.
            verification = {
                "Movies": Movie.objects.count(),
                "Series": Series.objects.count(),
                "Episodes": Episode.objects.count(),
                "Movie relations": M3UMovieRelation.objects.count(),
                "Series relations": M3USeriesRelation.objects.count(),
                "Episode relations": M3UEpisodeRelation.objects.count(),
                "Category relations": M3UVODCategoryRelation.objects.count(),
                "Categories": VODCategory.objects.count(),
                "Logos": VODLogo.objects.count(),
                "M3U accounts": M3UAccount.objects.count(),
            }

            empty = all(
                verification[name] == 0
                for name in (
                    "Movies",
                    "Series",
                    "Episodes",
                    "Movie relations",
                    "Series relations",
                    "Episode relations",
                    "Category relations",
                    "Categories",
                    "Logos",
                )
            )

            if not empty:
                raise RuntimeError(
                    "VOD cleanup verification failed: %s"
                    % verification
                )

        return {
            "status": "ok",
            "message": "VOD DATABASE CLEANUP COMPLETE",
            "deleted": deleted,
            "verification": verification,
        }
    except Exception as exc:
        LOG.exception("Decypharr VOD: VOD database cleanup failed")
        return {
            "status": "error",
            "message": "VOD database cleanup failed: %s" % exc,
            "deleted": deleted,
        }

def _tx_test(cfg):
    """Human-readable encoder test report for the 'Test Transcoding' action."""
    ffmpeg = _tx_find_ffmpeg(cfg)
    if not ffmpeg:
        return {"status": "error", "message": "ffmpeg not found. Set the FFmpeg Path setting."}
    detected = _tx_detect(cfg, ffmpeg, force=True)
    lines = ["ffmpeg: %s" % ffmpeg]
    for encoder, result in detected["results"].items():
        lines.append("%s: %s" % (encoder, "OK" if result == "ok" else "failed (%s)" % result))
    chosen = detected.get("encoder")
    lines.append("Selected: %s" % (chosen or "none"))
    lines.append("Browser transcoding is %s." % ("ENABLED" if cfg["enabled"] else "disabled"))
    lines.append("Hook installed: %s" % ("yes" if _TX_PATCHED else "no"))
    return {
        "status": "ok" if chosen else "error",
        "message": " | ".join(lines),
        "encoder": chosen,
        "results": detected["results"],
    }


class Plugin:
    name = "Decypharr VOD"
    version = "1.0.1"
    description = "Imports Decypharr media as native Dispatcharr VOD with .strm presentation, FFprobe technical metadata, and optional TMDB metadata."
    author = "Tw1zT3d2four7"
    fields = [
        {"id": "api_url", "label": "Decypharr API URL", "type": "string", "default": DEFAULT_API_URL},
        {"id": "api_token", "label": "Decypharr API Token", "type": "string", "default": ""},
        {"id": "library_root", "label": "Normalized Library", "type": "string", "default": LIBRARY_ROOT},
        {"id": "tmdb_api_key", "label": "TMDB API Key", "type": "string", "default": ""},
        {"id": "metadata_enabled", "label": "TMDB Metadata", "type": "boolean", "default": True},
        {"id": "ffprobe_path", "label": "FFprobe Path", "type": "string", "default": "/usr/local/bin/ffprobe"},
        {"id": "preferred_audio_language", "label": "Preferred Audio Language", "type": "select", "default": "eng",
         "options": [
             {"value": "eng", "label": "English"},
             {"value": "spa", "label": "Spanish"},
             {"value": "fra", "label": "French"},
             {"value": "deu", "label": "German"},
             {"value": "ita", "label": "Italian"},
             {"value": "por", "label": "Portuguese"},
             {"value": "jpn", "label": "Japanese"},
             {"value": "kor", "label": "Korean"},
             {"value": "zho", "label": "Chinese"},
             {"value": "hin", "label": "Hindi"},
             {"value": "ara", "label": "Arabic"},
             {"value": "original", "label": "Source Default / First Available"}
         ],
         "help_text": "Preferred audio language when multiple tracks exist. If unavailable, the source default track is used, then the first available track. Direct-play sources are not remuxed just to change the default."},
        {"id": "scan_interval", "label": "Auto Scan Interval (seconds)", "type": "number", "default": 60},
        {"id": "fast_initial_scan", "label": "Fast Initial Scan", "type": "boolean", "default": True,
         "help_text": "First import builds the playable catalog without remote FFprobe/TMDB probing. Metadata, genres, artwork and technical details are enriched afterward in small background batches."},
        {"id": "metadata_batch_size", "label": "Metadata Enrichment Batch Size", "type": "number", "default": 10,
         "help_text": "Titles enriched per background pass. Lower values minimize impact on active VOD playback."},
        {"id": "progressive_batch_size", "label": "Progressive Import Batch Size", "type": "number", "default": 200,
         "help_text": "Number of discovered media items imported at a time during the initial scan. Smaller values make the catalog appear sooner while reducing database bursts."},
        {"id": "api_workers", "label": "Decypharr API Workers", "type": "number", "default": 4,
         "help_text": "Parallel Decypharr inventory requests. Lower values reduce network pressure on active playback; 4 is the default for large libraries."},
        {"id": "transcode_info", "label": "About Browser Transcoding", "type": "info", "help_text": 'Browsers cannot play many common files: HEVC/x265, HDR10, and Dolby or DTS audio. In the Dispatcharr web player these start, stall, or freeze even though the stream is healthy. When enabled, the plugin checks each file the web player opens and converts only files the browser cannot play into H.264/AAC on the fly. Files a browser already plays, and every other app (VLC, Emby, Jellyfin, TiviMate, Kodi), are never transcoded. Seeking is limited while transcoding.'},
        {"id": "browser_transcode", "label": "Browser Transcoding", "type": "boolean", "default": False, "help_text": "Only used by the Dispatcharr web player, and only for files the browser cannot play. Off by default."},
        {"id": "transcode_encoder", "label": "Transcode Encoder", "type": "select", "default": "auto", "options": [
            {"value": "auto", "label": "Auto-detect (recommended)"},
            {"value": "nvenc", "label": "NVIDIA (NVENC)"},
            {"value": "qsv", "label": "Intel (Quick Sync)"},
            {"value": "vaapi", "label": "AMD / Intel (VAAPI)"},
            {"value": "cpu", "label": "CPU only (libx264)"},
        ], "help_text": "Auto tests NVIDIA, Intel, then AMD/VAAPI and falls back to CPU."},
        {"id": "vaapi_device", "label": "VAAPI / QSV Render Device", "type": "string", "default": "/dev/dri/renderD128"},
        {"id": "ffmpeg_path", "label": "FFmpeg Path", "type": "string", "default": "/usr/local/bin/ffmpeg"},
        {"id": "transcode_max_streams", "label": "Max Simultaneous Transcodes", "type": "number", "default": 2},
    ]
    actions = [
        {"id": "scan", "label": "Scan Decypharr", "description": "Scan Decypharr and rebuild the normalized presentation library.", "button_label": "Scan Now", "button_variant": "filled", "button_color": "blue"},
        {"id": "repair", "label": "Repair Integration", "description": "Reinstall native VOD hooks and ensure the synthetic account exists.", "button_label": "Repair", "button_variant": "outlined", "button_color": "gray"},
        {"id": "transcode_test", "label": "Test Transcoding", "description": "Test which hardware encoders (NVIDIA, Intel, AMD/VAAPI) or the CPU can transcode on this system.", "button_label": "Test", "button_variant": "outlined", "button_color": "gray"},
        {"id": "vod_database_cleanup", "label": "Clean VOD Database", "description": "Delete the VOD objects and relations, then immediately verify that the VOD tables are empty. The M3U accounts are preserved.", "button_label": "Clean + Verify", "button_variant": "outlined", "button_color": "red"},
    ]

    def __init__(self):
        self._settings = _fresh_plugin_settings()
        _install_route()
        _patch_refresh_guards()
        _patch_relations()
        _account()
        _patch_transcode()
        _start_auto_scan(self)

    def stop(self, context=None):
        AUTO_SCAN_STOP.set()
        thread = AUTO_SCAN_THREAD
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        LOG.info("Decypharr VOD: background auto-scan worker stop requested.")

    def run(self, action, params, context):
        self._settings = (context or {}).get("settings") or getattr(self, "_settings", {}) or {}
        if action == "repair":
            _install_route()
            _patch_refresh_guards()
            _patch_relations()
            _account()
            _patch_transcode()
            _start_auto_scan(self)
            return {"status": "ok", "message": "Decypharr VOD integration repaired.", "route_installed": ROUTE_INSTALLED, "patched": PATCHED, "transcode_hook": _TX_PATCHED}
        if action == "transcode_test":
            return _tx_test(_tx_settings(force=True))
        if action == "vod_database_cleanup":
            return _cleanup_vod_database()
        if action == "scan":
            return _scan(self, force=True, background=False)
        return {"status": "error", "message": "Unknown action: %s" % action}