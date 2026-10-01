"""Decypharr VOD plugin entry point.

v1.0.5 keeps exactly one VOD relation per logical movie, series, and episode.
TMDB genres remain metadata/categories and never become additional streams.
v1.0.5 also preserves media identity across source renames so an updated
Decypharr title/path reuses the existing Dispatcharr object and generated
representation instead of creating a second movie or series.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_CORE_PATH = Path(__file__).with_name("_decypharr_vod_core.py")
_spec = spec_from_file_location("_decypharr_vod_core", _CORE_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Unable to load Decypharr VOD core")
_core = module_from_spec(_spec)
_spec.loader.exec_module(_core)


def _sync_movie_genre_relations(account, movie, base_rel, genre_categories):
    """Enforce one movie relation; keep all genres in metadata."""
    names = [name for name, category in genre_categories if category]
    categories = [category for name, category in genre_categories if category]
    props = dict(base_rel.custom_properties or {})
    props["decypharr_genres"] = names
    props[_core.MARKER] = True
    base_rel.category = categories[0] if categories else None
    base_rel.custom_properties = props
    base_rel.save(update_fields=["category", "custom_properties"])
    _core.M3UMovieRelation.objects.filter(m3u_account=account, movie=movie).exclude(id=base_rel.id).delete()
    return [base_rel.id]


def _sync_series_genre_relations(account, series, base_rel, genre_categories):
    """Enforce one series relation; keep all genres in metadata."""
    names = [name for name, category in genre_categories if category]
    categories = [category for name, category in genre_categories if category]
    props = dict(base_rel.custom_properties or {})
    props["decypharr_genres"] = names
    props[_core.MARKER] = True
    base_rel.category = categories[0] if categories else None
    base_rel.custom_properties = props
    base_rel.save(update_fields=["category", "custom_properties"])
    _core.M3USeriesRelation.objects.filter(m3u_account=account, series=series).exclude(id=base_rel.id).delete()
    return [(names[0], base_rel)] if names else [(None, base_rel)]


def _sync_episode_genre_relations(account, episode, base_rel, series_relations, path, api_url, item, probe, season, number, seen_eps):
    """Enforce one episode relation; keep series genres in metadata."""
    names = [name for name, _ in series_relations if name]
    props = dict(base_rel.custom_properties or {})
    props[_core.MARKER] = True
    props["decypharr_genres"] = names
    base_rel.custom_properties = props
    base_rel.save(update_fields=["custom_properties"])
    _core.M3UEpisodeRelation.objects.filter(m3u_account=account, episode=episode).exclude(id=base_rel.id).delete()
    seen_eps.add(base_rel.id)


_core._sync_movie_genre_relations = _sync_movie_genre_relations
_core._sync_series_genre_relations = _sync_series_genre_relations
_core._sync_episode_genre_relations = _sync_episode_genre_relations

# v1.0.5 identity continuity patch.
_IDENTITY_CONTEXT = {"item": None}
_ORIG_FAST_IMPORT_ITEM = _core._fast_import_item
_ORIG_FIND_MOVIE = _core._find_movie
_ORIG_FIND_SERIES = _core._find_series
_ORIG_SCAN = _core._scan


def _plugin_owned(cp):
    return bool(isinstance(cp, dict) and cp.get(_core.MARKER))


def _source_match_relation(model, account, info_hash, file_path):
    if not info_hash and not file_path:
        return None
    try:
        rows = model.objects.filter(m3u_account=account)
        for rel in rows:
            cp = dict(getattr(rel, "custom_properties", None) or {})
            if not _plugin_owned(cp):
                continue
            if info_hash and cp.get("decypharr_info_hash") == info_hash:
                return rel
            if file_path and cp.get("decypharr_file_path") == file_path:
                return rel
    except Exception:
        _core.LOG.exception("Decypharr VOD: source identity lookup failed")
    return None


def _identity_movie(name, year, tmdb_id=None):
    item = _IDENTITY_CONTEXT.get("item") or {}
    try:
        account = _core._account()
        rel = _source_match_relation(_core.M3UMovieRelation, account, item.get("info_hash"), item.get("file_path"))
        if rel is not None and rel.movie is not None:
            return rel.movie
    except Exception:
        _core.LOG.exception("Decypharr VOD: movie source identity lookup failed")
    return _ORIG_FIND_MOVIE(name, year, tmdb_id)


def _identity_series(name, year, tmdb_id=None):
    item = _IDENTITY_CONTEXT.get("item") or {}
    try:
        account = _core._account()
        rel = _source_match_relation(_core.M3USeriesRelation, account, item.get("info_hash"), item.get("file_path"))
        if rel is not None and rel.series is not None:
            return rel.series
    except Exception:
        _core.LOG.exception("Decypharr VOD: series source identity lookup failed")
    return _ORIG_FIND_SERIES(name, year, tmdb_id)


def _continuity_fast_import(account, item, lib, seen_files, seen_movies, seen_eps, seen_series, seen_series_relations):
    _IDENTITY_CONTEXT["item"] = item
    try:
        result = _ORIG_FAST_IMPORT_ITEM(account, item, lib, seen_files, seen_movies, seen_eps, seen_series, seen_series_relations)
        if result:
            epi = item.get("episode")
            if epi:
                release = _core._release_root(item["path"], _core.SOURCE_ROOT)
                name = _core._series_title_from_path(item["path"]) or _core._release_title(release.name)
                year = _core._year(str(release)) or _core._year(item["path"])
                obj = _identity_series(name, year, None)
            else:
                release = _core._release_root(item["path"], _core.SOURCE_ROOT)
                name = _core._release_title(release.name)
                year = _core._year(str(release)) or _core._year(item["path"])
                obj = _identity_movie(name, year, None)
            if obj is not None:
                changed = []
                if name and obj.name != name:
                    obj.name = name
                    changed.append("name")
                if year and obj.year != year:
                    obj.year = year
                    changed.append("year")
                cp = dict(obj.custom_properties or {})
                cp.update({
                    _core.MARKER: True,
                    "decypharr_info_hash": item.get("info_hash"),
                    "decypharr_file_path": item.get("file_path"),
                    "decypharr_path": item.get("path"),
                    "decypharr_api_url": item.get("api_url"),
                })
                obj.custom_properties = cp
                changed.append("custom_properties")
                if changed:
                    obj.save(update_fields=list(dict.fromkeys(changed)))
        return result
    finally:
        _IDENTITY_CONTEXT["item"] = None


def _dedupe_plugin_movies():
    """Remove duplicate plugin-owned Movie objects after identity reconciliation."""
    try:
        movies = [m for m in _core.Movie.objects.all() if _plugin_owned(m.custom_properties)]
        groups = {}
        for movie in movies:
            key = (_core._movie_match_key(movie.name), movie.year or None)
            groups.setdefault(key, []).append(movie)
        removed = 0
        account = _core._account()
        for _key, items in groups.items():
            if len(items) < 2:
                continue
            def score(movie):
                cp = dict(movie.custom_properties or {})
                return int(bool(cp.get("decypharr_info_hash"))) * 4 + int(bool(cp.get("decypharr_file_path"))) * 2 + movie.id / 10000000
            keep = max(items, key=score)
            for movie in items:
                if movie.id == keep.id:
                    continue
                _core.M3UMovieRelation.objects.filter(m3u_account=account, movie=movie).delete()
                movie.delete()
                removed += 1
        if removed:
            _core.LOG.info("Decypharr VOD: removed %d duplicate plugin-owned Movie objects after identity reconciliation.", removed)
        return removed
    except Exception:
        _core.LOG.exception("Decypharr VOD: duplicate movie reconciliation failed")
        return 0


def _identity_scan(plugin, force=False, background=False, fast=False, enrich_only=False):
    result = _ORIG_SCAN(plugin, force=force, background=background, fast=fast, enrich_only=enrich_only)
    if result.get("status") in ("ok", "unchanged") and not enrich_only:
        _dedupe_plugin_movies()
    return result


_core._find_movie = _identity_movie
_core._find_series = _identity_series
_core._fast_import_item = _continuity_fast_import
_core._scan = _identity_scan
_core.Plugin.version = "1.0.5"
Plugin = _core.Plugin

__all__ = ["Plugin"]
