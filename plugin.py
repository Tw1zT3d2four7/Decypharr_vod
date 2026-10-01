"""Decypharr VOD plugin entry point.

v1.0.3 keeps exactly one VOD relation per logical movie, series, and episode.
TMDB genres remain metadata/categories and never become additional streams.
This entry point is part of the v1.0.3 release and exposes the matching core version.
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
    _core.M3UMovieRelation.objects.filter(
        m3u_account=account, movie=movie
    ).exclude(id=base_rel.id).delete()
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
    _core.M3USeriesRelation.objects.filter(
        m3u_account=account, series=series
    ).exclude(id=base_rel.id).delete()
    return [(names[0], base_rel)] if names else [(None, base_rel)]


def _sync_episode_genre_relations(
    account, episode, base_rel, series_relations, path, api_url, item, probe, season, number, seen_eps
):
    """Enforce one episode relation; keep series genres in metadata."""
    names = [name for name, _ in series_relations if name]
    props = dict(base_rel.custom_properties or {})
    props[_core.MARKER] = True
    props["decypharr_genres"] = names
    base_rel.custom_properties = props
    base_rel.save(update_fields=["custom_properties"])
    _core.M3UEpisodeRelation.objects.filter(
        m3u_account=account, episode=episode
    ).exclude(id=base_rel.id).delete()
    seen_eps.add(base_rel.id)


_core._sync_movie_genre_relations = _sync_movie_genre_relations
_core._sync_series_genre_relations = _sync_series_genre_relations
_core._sync_episode_genre_relations = _sync_episode_genre_relations
_core.Plugin.version = "1.0.3"
Plugin = _core.Plugin

__all__ = ["Plugin"]
