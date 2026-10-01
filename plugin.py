"""Decypharr VOD plugin entry point.

v1.0.1 keeps the existing implementation in _decypharr_vod_core.py and
patches the legacy TMDB genre relation behavior before exposing Plugin.
Genres remain metadata/categories; they never create additional streams.
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
    """Keep exactly one movie relation; genres are metadata/categories."""
    names = [name for name, category in genre_categories if category]
    categories = [category for name, category in genre_categories if category]
    props = dict(base_rel.custom_properties or {})
    props["decypharr_genres"] = names
    props[_core.MARKER] = True
    base_rel.category = categories[0] if categories else None
    base_rel.custom_properties = props
    base_rel.save(update_fields=["category", "custom_properties"])

    marked = _core.M3UMovieRelation.objects.filter(
        m3u_account=account, movie=movie,
        custom_properties__decypharr_genre_relation=True,
    ).exclude(id=base_rel.id)
    legacy = _core.M3UMovieRelation.objects.filter(
        m3u_account=account, movie=movie,
        stream_id__contains="--genre-",
    ).exclude(id=base_rel.id)
    (marked | legacy).distinct().delete()
    return [base_rel.id]


def _sync_series_genre_relations(account, series, base_rel, genre_categories):
    """Keep exactly one series relation; genres are metadata/categories."""
    names = [name for name, category in genre_categories if category]
    categories = [category for name, category in genre_categories if category]
    props = dict(base_rel.custom_properties or {})
    props["decypharr_genres"] = names
    props[_core.MARKER] = True
    base_rel.category = categories[0] if categories else None
    base_rel.custom_properties = props
    base_rel.save(update_fields=["category", "custom_properties"])

    marked = _core.M3USeriesRelation.objects.filter(
        m3u_account=account, series=series,
        custom_properties__decypharr_genre_relation=True,
    ).exclude(id=base_rel.id)
    legacy = _core.M3USeriesRelation.objects.filter(
        m3u_account=account, series=series,
        external_series_id__contains="-genre-",
    ).exclude(id=base_rel.id)
    (marked | legacy).distinct().delete()
    return [(names[0], base_rel)] if names else [(None, base_rel)]


def _sync_episode_genre_relations(
    account, episode, base_rel, series_relations, path, api_url, item, probe, season, number, seen_eps
):
    """Keep exactly one episode relation; remove legacy genre copies."""
    names = [name for name, _ in series_relations if name]
    props = dict(base_rel.custom_properties or {})
    props[_core.MARKER] = True
    props["decypharr_genres"] = names
    base_rel.custom_properties = props
    base_rel.save(update_fields=["custom_properties"])

    marked = _core.M3UEpisodeRelation.objects.filter(
        m3u_account=account, episode=episode,
        custom_properties__decypharr_genre_relation=True,
    ).exclude(id=base_rel.id)
    legacy = _core.M3UEpisodeRelation.objects.filter(
        m3u_account=account, episode=episode,
        stream_id__contains="--genre-",
    ).exclude(id=base_rel.id)
    (marked | legacy).distinct().delete()
    seen_eps.add(base_rel.id)




# Patch the functions in the module where Plugin.run/scan resolves globals.
_core._sync_movie_genre_relations = _sync_movie_genre_relations
_core._sync_series_genre_relations = _sync_series_genre_relations
_core._sync_episode_genre_relations = _sync_episode_genre_relations
_core.Plugin.version = "1.0.1"
Plugin = _core.Plugin

__all__ = ["Plugin"]
