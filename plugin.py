"""Decypharr VOD plugin entry point - v1.0.5.

Preserves Decypharr source identity across title/path renames and keeps one
canonical VOD relation per logical movie, series, and episode.
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

_IDENTITY_CONTEXT = {"item": None}
_ORIG_FAST_IMPORT_ITEM = _core._fast_import_item
_ORIG_FIND_MOVIE = _core._find_movie
_ORIG_FIND_SERIES = _core._find_series


def _source_relation(model, account, item):
    info_hash = item.get("info_hash")
    file_path = item.get("file_path")
    if not info_hash and not file_path:
        return None
    try:
        for rel in model.objects.filter(m3u_account=account):
            props = dict(rel.custom_properties or {})
            if not props.get(_core.MARKER):
                continue
            if info_hash and props.get("decypharr_info_hash") == info_hash:
                return rel
            if file_path and props.get("decypharr_file_path") == file_path:
                return rel
    except Exception:
        _core.LOG.exception("Decypharr VOD: source identity lookup failed")
    return None


def _find_movie_by_source(name, year, tmdb_id=None):
    item = _IDENTITY_CONTEXT.get("item") or {}
    rel = _source_relation(_core.M3UMovieRelation, _core._account(), item)
    if rel is not None and rel.movie is not None:
        return rel.movie
    return _ORIG_FIND_MOVIE(name, year, tmdb_id)


def _find_series_by_source(name, year, tmdb_id=None):
    item = _IDENTITY_CONTEXT.get("item") or {}
    rel = _source_relation(_core.M3USeriesRelation, _core._account(), item)
    if rel is not None and rel.series is not None:
        return rel.series
    return _ORIG_FIND_SERIES(name, year, tmdb_id)


def _fast_import_with_identity(account, item, lib, seen_files, seen_movies, seen_eps, seen_series, seen_series_relations):
    _IDENTITY_CONTEXT["item"] = item
    try:
        return _ORIG_FAST_IMPORT_ITEM(account, item, lib, seen_files, seen_movies, seen_eps, seen_series, seen_series_relations)
    finally:
        _IDENTITY_CONTEXT["item"] = None

_core._find_movie = _find_movie_by_source
_core._find_series = _find_series_by_source
_core._fast_import_item = _fast_import_with_identity
_core.Plugin.version = "1.0.5"
Plugin = _core.Plugin

__all__ = ["Plugin"]
