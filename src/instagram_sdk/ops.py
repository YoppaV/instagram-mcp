"""The registry of Instagram operations the SDK calls with in-page ``fetch``.

This is the single place to patch when Instagram rotates a ``doc_id`` or a
response root — see "Maintenance" in ``README.md``. Values were captured from
instagram.com on ``TABLE_DATE`` (web revision 1056444526).

Variable templates: a string ``"$name"`` is a hole that ``graphql(**params)``
fills with the value as is (``None``, number, dict...). ``"$name?"`` is an
optional hole: when the parameter is ``None`` the key is dropped, so a first
page goes out exactly like the web sends it. ``$ig_did`` is filled by the
client from the cookie, not by the caller.

``status`` is documentary: V verified with fetch, C captured from the web
app, I inferred.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Union

TABLE_DATE = "2026-11-27"

GRAPHQL = "/api/graphql"
GRAPHQL_QUERY = "/graphql/query"
ENDPOINTS = (GRAPHQL, GRAPHQL_QUERY)

# Parameters the client fills itself.
CLIENT_PARAMS = frozenset({"ig_did"})


@dataclass(frozen=True)
class HealRecipe:
    """Page whose JS sends this op: how ``opsheal.Healer`` rediscovers it."""

    path: str
    action: str = "none"  # "none" | "scroll"


@dataclass(frozen=True)
class GraphQLOp:
    name: str
    friendly_name: str
    doc_id: str
    endpoint: str
    variables: Mapping[str, Any]
    root: tuple[str, ...]
    alt_roots: tuple[tuple[str, ...], ...] = ()
    nullable_root: bool = False
    cursor_var: str | None = None
    budget_class: str = "general"
    heal: HealRecipe | None = None
    status: str = "I"


@dataclass(frozen=True)
class RestOp:
    name: str
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    items_key: str = "items"
    cursor_param: str | None = "max_id"
    next_key: str = "next_max_id"
    more_key: str = "more_available"
    budget_class: str = "general"
    status: str = "I"


Op = Union[GraphQLOp, RestOp]


def PV(name: str) -> str:  # noqa: N802 - mirrors the design's notation
    return f"__relay_internal__pv__Polaris{name}relayprovider"


def _freeze(obj: Any) -> Any:
    if isinstance(obj, dict):
        return MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return tuple(_freeze(v) for v in obj)
    return obj


def _thaw(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: _thaw(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [_thaw(v) for v in obj]
    return obj


_MISSING = object()


def _hole(value: Any) -> tuple[str, bool] | None:
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        name = value[1:]
        optional = name.endswith("?")
        return (name[:-1] if optional else name), optional
    return None


def template_holes(template: Any) -> set[str]:
    """Every hole name in a template (optional ones included)."""
    found: set[str] = set()
    if isinstance(template, Mapping):
        for value in template.values():
            found |= template_holes(value)
    elif isinstance(template, (list, tuple)):
        for value in template:
            found |= template_holes(value)
    else:
        hole = _hole(template)
        if hole:
            found.add(hole[0])
    return found


def fill_template(template: Any, params: Mapping[str, Any]) -> Any:
    """Return a plain JSON-able copy of ``template`` with holes filled.

    A missing required parameter or an unknown one raises ``ValueError``:
    both are programming errors, better caught before a request goes out.
    """
    unknown = set(params) - template_holes(template)
    if unknown:
        raise ValueError(f"unknown template parameters: {sorted(unknown)}")

    def fill(value: Any) -> Any:
        if isinstance(value, Mapping):
            out = {}
            for key, sub in value.items():
                filled = fill(sub)
                if filled is not _MISSING:
                    out[key] = filled
            return out
        if isinstance(value, (list, tuple)):
            return [fill(v) for v in value]
        hole = _hole(value)
        if hole is None:
            return value
        name, optional = hole
        if name not in params:
            if optional:
                return _MISSING
            raise ValueError(f"missing template parameter {name!r}")
        if optional and params[name] is None:
            return _MISSING
        return params[name]

    return fill(template)


# --------------------------------------------------------------------------
# variable building blocks (design appendix A)
# --------------------------------------------------------------------------
PV_FEED = {
    PV("ShortDramaEnabled"): True,
    PV("ProductTagIndicatorEnabled"): False,
    PV("MultiCaptionCarouselEnabled"): False,
    PV("ThreadsStyleCommentsEnabled"): False,
    PV("ReelsRecoDebugOverlayEnabled"): False,
    PV("DSA16EntrypointEnabled"): False,
    PV("AdDebugToolEnabled"): False,
    PV("SuggestedUserFeedUnitCardsEnabled"): True,
}
FEED_DATA = {
    "device_id": "$ig_did",
    "is_async_ads_double_request": "0",
    "is_async_ads_in_headload_enabled": "0",
    "is_async_ads_rti": "0",
    "rti_delivery_backend": "0",
}
POSTS_DATA = {
    "count": 12,
    "include_reel_media_seen_timestamp": True,
    "include_relationship_info": True,
    "latest_besties_reel_media": True,
    "latest_reel_media": True,
}
PV_POSTS = {
    PV("MultiCaptionCarouselEnabled"): False,
    PV("ShortDramaEnabled"): True,
    PV("SEOIsCrawler"): False,
    PV("ProductTagIndicatorEnabled"): False,
    PV("ThreadsStyleCommentsEnabled"): False,
    PV("ReelsRecoDebugOverlayEnabled"): False,
}
REELS_DATA = {"include_feed_video": True, "page_size": 12, "target_user_id": "$id"}

TIMELINE_ROOT = ("data", "xdt_api__v1__feed__timeline__connection")
POSTS_ROOT = ("data", "xdt_api__v1__feed__user_timeline_graphql_connection")
REELS_ROOT = ("data", "fetch__XDTUserDict", "clips_connection")
REELS_ALT_ROOT = ("data", "xdt_api__v1__clips__user__connection_v2")

_GRAPHQL_OPS: tuple[GraphQLOp, ...] = (
    GraphQLOp(
        name="home_page",
        friendly_name="PolarisFeedRootPaginationCachedQuery_subscribe",
        doc_id="40856843000596438",
        endpoint=GRAPHQL_QUERY,
        # The web also sends data.feed_view_info ("seen" telemetry); omitted (D1).
        variables={
            "after": "$after",
            "before": None,
            "data": FEED_DATA,
            "first": "$first",
            "last": None,
            "variant": "home",
            **PV_FEED,
        },
        root=TIMELINE_ROOT,
        cursor_var="after",
        heal=HealRecipe("/", "scroll"),
        status="C",
    ),
    GraphQLOp(
        name="home_first",
        friendly_name="PolarisFeedTimelineRootV2Query",
        doc_id="29827794186910521",
        endpoint=GRAPHQL_QUERY,
        variables={
            "after": None,
            "before": None,
            "data": FEED_DATA,
            "first": 3,
            "last": None,
            "variant": "home",
            **PV_FEED,
        },
        root=TIMELINE_ROOT,
        heal=HealRecipe("/"),
        status="C",
    ),
    GraphQLOp(
        name="saved_collections",
        friendly_name="PolarisProfileSavedTabContentQuery",
        doc_id="27584326974521636",
        endpoint=GRAPHQL,
        variables={
            "after": "$after?",
            "collection_types": [
                "ALL_MEDIA_AUTO_COLLECTION",
                "PLAYLIST_AUTO_COLLECTION",
                "MEDIA",
                "AUDIO_AUTO_COLLECTION",
            ],
            "first": 12,
        },
        root=("data", "viewer", "collections_unified_with_auto_collections"),
        alt_roots=(("data", "xdt_api__v1__collections__list_graphql_connection"),),
        cursor_var="after",
        heal=HealRecipe("/{viewer}/saved/"),
        status="C",
    ),
    GraphQLOp(
        name="saved_collection",
        friendly_name="PolarisSavedCollectionPageWWWQuery",
        doc_id="39910535158594768",
        endpoint=GRAPHQL,
        variables={
            "after": "$after",
            "collection_id": "$collection_id",
            "first": 12,
            PV("ShortDramaEnabled"): True,
            PV("SEOIsCrawler"): False,
        },
        root=("data", "fetch__MediaCollection"),
        nullable_root=True,
        cursor_var="after",
        heal=HealRecipe("/{viewer}/saved/{collection_id}/{collection_id}/"),
        status="C",
    ),
    GraphQLOp(
        name="search_box",
        friendly_name="PolarisSearchBoxRefetchableQuery",
        doc_id="27706427925724183",
        endpoint=GRAPHQL,
        variables={
            "data": {
                "context": "blended",
                "include_reel": "true",
                "query": "$query",
                "rank_token": "",
                "search_session_id": "",
                "search_surface": "web_top_search",
            },
            "hasQuery": True,
        },
        root=("data", "xdt_api__v1__fbsearch__topsearch_connection"),
        budget_class="search",
        status="V",
    ),
    GraphQLOp(
        name="profile_info",
        friendly_name="PolarisProfilePageContentQuery",
        doc_id="29609640381972919",
        endpoint=GRAPHQL,
        variables={
            "enable_integrity_filters": True,
            "id": "$id",
            PV("CannesGuardianExperienceEnabled"): True,
            PV("CASB976ProfileEnabled"): False,
            PV("ProfileInformTreatmentBannerEnabled"): False,
            PV("WebSchoolsEnabled"): False,
            PV("ProfileRingsGradientEnabled"): True,
            PV("DSA16EntrypointEnabled"): False,
            PV("RepostsConsumptionEnabled"): True,
            PV("ShortDramaEnabled"): True,
        },
        root=("data", "user"),
        nullable_root=True,
        heal=HealRecipe("/{username}/"),
        status="V",
    ),
    GraphQLOp(
        name="profile_posts_first",
        friendly_name="PolarisProfilePostsQuery",
        doc_id="29911767465181002",
        endpoint=GRAPHQL,
        variables={"data": POSTS_DATA, "username": "$username", **PV_POSTS},
        root=POSTS_ROOT,
        nullable_root=True,
        heal=HealRecipe("/{username}/"),
        status="V",
    ),
    GraphQLOp(
        name="profile_posts_page",
        friendly_name="PolarisProfilePostsTabContentQuery_connection",
        doc_id="39818828461098125",
        endpoint=GRAPHQL,
        variables={
            "after": "$after",
            "before": None,
            "data": POSTS_DATA,
            "first": 12,
            "include_multi_captions": False,
            "last": None,
            "username": "$username",
            **PV_POSTS,
        },
        root=POSTS_ROOT,
        nullable_root=True,
        cursor_var="after",
        heal=HealRecipe("/{username}/", "scroll"),
        status="V",
    ),
    GraphQLOp(
        name="profile_reels_first",
        friendly_name="PolarisProfileReelsTabContentQuery",
        doc_id="30033462306341888",
        endpoint=GRAPHQL,
        variables={"data": REELS_DATA, "user_id": "$id", PV("ShortDramaEnabled"): True},
        root=REELS_ROOT,
        alt_roots=(REELS_ALT_ROOT,),
        nullable_root=True,
        heal=HealRecipe("/{username}/reels/"),
        status="C",
    ),
    GraphQLOp(
        name="profile_reels_page",
        friendly_name="PolarisProfileReelsTabContentQuery_connection",
        doc_id="29174105612283268",
        endpoint=GRAPHQL,
        variables={
            "after": "$after",
            "data": REELS_DATA,
            "first": "$first",
            "id": "$id",
            PV("ShortDramaEnabled"): True,
        },
        root=REELS_ROOT,
        alt_roots=(REELS_ALT_ROOT,),
        nullable_root=True,
        cursor_var="after",
        heal=HealRecipe("/{username}/reels/", "scroll"),
        status="C",
    ),
    # Registered, not used: the post page HTML is the primary path (D2).
    GraphQLOp(
        name="post_root",
        friendly_name="PolarisPostRootQuery",
        doc_id="28931583176534666",
        endpoint=GRAPHQL,
        variables={"shortcode": "$shortcode", "include_longform_landscape_cover": False},
        root=("data", "xdt_api__v1__media__shortcode__web_info"),
        nullable_root=True,
        status="I",
    ),
)

_REST_OPS: tuple[RestOp, ...] = (
    RestOp(name="saved_posts", path="/api/v1/feed/saved/posts/", status="V"),
    RestOp(name="user_info", path="/api/v1/users/{user_id}/info/", cursor_param=None),
    # The web now reads collections over GraphQL, but this still answers (probed
    # 2026-11-27) and carries video_versions/taken_at; used first (D3/R3).
    RestOp(name="collection_feed", path="/api/v1/feed/collection/{collection_id}/posts/",
           status="V"),
)


def _build_table() -> Mapping[str, Op]:
    table: dict[str, Op] = {}
    for op in (*_GRAPHQL_OPS, *_REST_OPS):
        if op.name in table:
            raise ValueError(f"duplicate op name {op.name!r}")
        if isinstance(op, GraphQLOp):
            op = GraphQLOp(**{**op.__dict__, "variables": _freeze(dict(op.variables))})
        table[op.name] = op
    return MappingProxyType(table)


TABLE: Mapping[str, Op] = _build_table()


def get(name: str) -> Op:
    try:
        return TABLE[name]
    except KeyError:
        raise KeyError(f"unknown Instagram operation {name!r}") from None


def build_variables(op: GraphQLOp, params: Mapping[str, Any]) -> dict[str, Any]:
    return fill_template(_thaw(op.variables), params)
