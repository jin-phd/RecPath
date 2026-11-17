"""Offline m.youtube.com pages built from real card markup (tests/fixtures/mweb_cards.json).

The cards were captured from live pages; the page shells around them are minimal and add the
traps the extractor must avoid: the top-bar "Open App" link to the playing video, a hidden
chapter link, a companion ad, an in-feed ad card, a LIVE card, an ordinary video linked as a
radio start (&list=RD<id>&start_radio=1), and recommendations split over several
"related-items" sections. Optional extras: a player showing an ad, YouTube's embedded page data
(an ad schedule and a related video that is really a Short), a small Shorts feed, and the
"lockup" layout, where the same videos come as yt-lockup-view-model cards (the newer card some
visitors get), with a playlist, a Mix and a card without a channel avatar added to the search page.
"""
from __future__ import annotations

import json
import re
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlparse


CARDS = json.loads((Path(__file__).parent / "fixtures" / "mweb_cards.json").read_text(encoding="utf-8"))
VIDEO_ID_RE = re.compile(r"watch\?v=([\w-]{11})")

STYLE = """<style>
ytm-app, ytm-watch, ytm-search, ytm-section-list-renderer, ytm-item-section-renderer, lazy-list,
ytm-single-column-watch-next-results-renderer, ytm-video-with-context-renderer, ytm-ad-slot-renderer,
grid-shelf-view-model, ytm-mobile-topbar-renderer, yt-lockup-view-model, .ytLockupViewModelContentImage,
yt-thumbnail-view-model { display: block; }
ytm-video-with-context-renderer, ytm-ad-slot-renderer, yt-lockup-view-model { min-height: 220px; }
yt-thumbnail-view-model { width: 160px; height: 90px; }
</style>"""

def consent_dialog(mode: str = "normal") -> str:
    """The EU cookie dialog. mode "reload" reloads the page after answering, like YouTube does on
    watch pages; mode "accept_only" offers no Reject button; mode "late" ignores clicks for the
    first 1.5 s, like a dialog that renders before its buttons are wired up."""
    after = "location.reload()" if mode == "reload" else "this.parentElement.remove()"
    guard = "if (!window.consentReady) return; " if mode == "late" else ""
    accept = (
        '<button aria-label="Accept the use of cookies and other data for the purposes described" '
        f"onclick=\"{guard}document.cookie='SOCS=CAI; path=/; max-age=3600'; {after}\">Accept all</button>"
    )
    reject = (
        '<button aria-label="Reject the use of cookies and other data for the purposes described" '
        f"onclick=\"{guard}document.cookie='SOCS=CAE; path=/; max-age=3600'; {after}\">Reject all</button>"
    )
    buttons = accept if mode == "accept_only" else accept + reject
    wiring = "<script>setTimeout(() => { window.consentReady = true; }, 1500);</script>" if mode == "late" else ""
    return (
        '<ytm-consent-bump-v2-renderer style="display:block"><p>Before you continue to YouTube</p>'
        f"{buttons}</ytm-consent-bump-v2-renderer>{wiring}"
    )


def client_config(cookie_header: str, ip_location: str = "IE") -> str:
    """YouTube's client config (ytcfg) with the content location it applies: the gl of the PREF
    cookie if there is one, otherwise the IP's location. URL parameters do not count, as they
    do not survive YouTube's cookie dialog."""
    pref = next((part.split("=", 1)[1] for part in cookie_header.split("; ") if part.startswith("PREF=")), "")
    gl = (parse_qs(pref).get("gl") or [ip_location])[0]
    context = {"client": {"clientName": "MWEB", "gl": gl, "hl": "en"}}
    return (
        f"<script>window.ytcfg = {{get: (key) => key === 'INNERTUBE_CONTEXT' ? {json.dumps(context)} : undefined}};"
        "</script>"
    )

AD_CARD = """<ytm-ad-slot-renderer><a href="https://www.googleadservices.com/pagead/aclk?sa=L&adurl=https://example.com">
<h3>Fixture advertiser offer</h3></a><badge-shape class="ytBadgeShapeAd" aria-label="Sponsored">Sponsored</badge-shape>
</ytm-ad-slot-renderer>"""

COMPANION_AD = """<ytm-companion-slot><ytm-companion-ad-renderer>
<span class="YtmCompanionAdRendererHeadline">Fixture companion headline</span>
<span class="YtmCompanionAdRendererDescription">shop.example</span>
<a href="https://www.googleadservices.com/pagead/aclk?sa=L&amp;adurl=https%3A%2F%2Fshop.example%2Foffer">Visit advertiser</a>
<a href="/watch?v=AAAAAAAAAAA">ad video</a></ytm-companion-ad-renderer></ytm-companion-slot>"""

PLAYER = '<div id="movie_player" class="html5-video-player playing-mode" style="height:220px;background:#000">player</div>'
PLAYER_WITH_AD = """<div id="movie_player" class="html5-video-player ad-showing ad-interrupting" style="height:220px;background:#000">
<div class="ytp-ad-text ytp-video-ad-top-bar-title">Fixture ad title</div>
<div class="ytp-ad-player-overlay-instream-info">Sponsored 1 of 2 · 0:15</div>
<button class="ytp-ad-skip-button-modern">Skip</button></div>"""
SCHEDULED_AD_VIDEO_ID = "AdVideo0001"


def page_data_script(current_video_id: str, reel_video_id: str) -> str:
    """window.ytInitialPlayerResponse / ytInitialData as YouTube embeds them on full loads."""
    player = {
        "videoDetails": {"videoId": current_video_id},
        "adSlots": [{"adSlotRenderer": {
            "adSlotMetadata": {"slotType": "SLOT_TYPE_PLAYER_BYTES", "triggerEvent": "SLOT_TRIGGER_EVENT_BEFORE_CONTENT"},
            "fulfillmentContent": {"instreamVideoAdRenderer": {
                "externalVideoId": SCHEDULED_AD_VIDEO_ID,
                "headline": {"simpleText": "Fixture headline"},
                "clickthroughUrl": {"simpleText": "https://ad.example/track?id=1"},
            }},
        }}],
        "adPlacements": [{"adPlacementRenderer": {"config": {"adPlacementConfig": {
            "kind": "AD_PLACEMENT_KIND_MILLISECONDS", "adTimeOffset": {"offsetStartMilliseconds": "90000"}}}}}],
    }
    initial = {"contents": [{"videoWithContextRenderer": {
        "videoId": reel_video_id,
        "navigationEndpoint": {"reelWatchEndpoint": {"videoId": reel_video_id}},
        "shortBylineText": {"runs": [{"text": "Reel channel", "navigationEndpoint": {"browseEndpoint": {"browseId": "UCreel"}}}]},
    }}]}
    return (
        f"<script>window.ytInitialPlayerResponse = {json.dumps(player)};"
        f"window.ytInitialData = {json.dumps(initial)};</script>"
    )


def card_video_id(card_html: str) -> str:
    return VIDEO_ID_RE.search(card_html).group(1)


SEARCH_VIDEO_IDS = [card_video_id(card) for card in CARDS["search_cards"]]
WATCH_VIDEO_IDS = [card_video_id(card) for card in CARDS["watch_cards"]]


def topbar(current_video_id: str | None) -> str:
    bar = CARDS["topbar"]
    if not current_video_id:
        return bar
    open_app = (
        f'<a href="intent://m.youtube.com/watch?v={current_video_id}&feature=mweb_c3_open_app#Intent;end" '
        'style="display:inline-block;width:100px;height:30px">Open App</a>'
        f'<a href="https://m.youtube.com/watch?v={current_video_id}&feature=open_app" '
        'style="display:inline-block;width:30px;height:30px">App</a>'
    )
    return bar.replace("</ytm-mobile-topbar-renderer>", open_app + "</ytm-mobile-topbar-renderer>")


def page(body: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{STYLE}</head><body><ytm-app>{body}</ytm-app></body></html>"
    )


def lockup_card(
    video_id: str,
    *,
    href: str | None = None,
    badge: str | None = "12:34",
    channel: str | None = None,
    avatar: bool = True,
) -> str:
    """A yt-lockup-view-model card, from the template captured on a live mobile page."""
    channel = channel or f"Lockup channel {video_id[:4]}"
    card = CARDS["lockup_card"]
    if not avatar:
        card = re.sub(r'<div class="ytLockupMetadataViewModelAvatar">.*?</yt-decorated-avatar-view-model></div>', "", card)
    badge_html = (
        CARDS["lockup_badge"].replace("__BADGE_TEXT__", badge).replace("__BADGE_LABEL__", badge) if badge else ""
    )
    return (
        card.replace("__BADGE__", badge_html)
        .replace("__HREF__", href or f"/watch?v={video_id}&amp;pp=0gcJCdgAo7VqN5tD")
        .replace("__VIDEO_ID__", video_id)
        .replace("__TITLE__", f"Lockup video {video_id}")
        .replace("__CHANNEL__", channel)
        .replace("__VIEWS__", "12K views")
    )


def as_lockup(card_html: str) -> str:
    """The same video as a lockup card: same link (list parameters included), LIVE kept."""
    href = re.search(r'href="(/watch\?v=[^"]+)"', card_html).group(1)
    badge = "LIVE" if 'data-style="LIVE"' in card_html else "12:34"
    return lockup_card(card_video_id(card_html), href=href, badge=badge)


LOCKUP_PLAYLIST_ID = "PLfixturePlaylist01"
LOCKUP_MIX_VIDEO_ID = "MixLockup01"
LOCKUP_NO_AVATAR_VIDEO_ID = "NoAvatar001"
LOCKUP_EXTRAS = (
    lockup_card("PlaylistLk1", href=f"/playlist?list={LOCKUP_PLAYLIST_ID}", badge=None)
    + lockup_card(
        LOCKUP_MIX_VIDEO_ID,
        href=f"/watch?v={LOCKUP_MIX_VIDEO_ID}&amp;list=RD{LOCKUP_MIX_VIDEO_ID}&amp;start_radio=1",
        badge="Mix",
    )
    + lockup_card(LOCKUP_NO_AVATAR_VIDEO_ID, channel="Rows only channel", avatar=False)
)


def search_page(*, consent: str | None = "normal", layout: str = "legacy") -> str:
    cards = CARDS["search_cards"]
    if layout == "lockup":
        cards = [as_lockup(card) for card in cards]
    feed = cards[0] + CARDS["shorts_shelf"] + "".join(cards[1:3]) + AD_CARD + "".join(cards[3:])
    if layout == "lockup":
        feed += LOCKUP_EXTRAS
    return page(
        topbar(None)
        + (consent_dialog(consent) if consent else "")
        + "<ytm-search><ytm-section-list-renderer><lazy-list><ytm-item-section-renderer><lazy-list>"
        + feed
        + "</lazy-list></ytm-item-section-renderer></lazy-list></ytm-section-list-renderer></ytm-search>"
    )


def live_variant(card_html: str) -> str:
    return card_html.replace('data-style="DEFAULT"', 'data-style="LIVE"', 1)


def radio_start_variant(card_html: str) -> str:
    video_id = card_video_id(card_html)
    return card_html.replace(f"watch?v={video_id}", f"watch?v={video_id}&amp;list=RD{video_id}&amp;start_radio=1")


RADIO_START_VIDEO_ID = WATCH_VIDEO_IDS[-1]
REEL_VIDEO_ID = WATCH_VIDEO_IDS[2]  # page data says this related video opens in the Shorts player
SHORTS_SEQUENCE = list(dict.fromkeys(re.findall(r"/shorts/([\w-]{11})", CARDS["shorts_shelf"])))[:3] + [
    "FixtureShrt",
    "FixtureEnd1",
]
SHORTS_AD_ID = SHORTS_SEQUENCE[1]


def shorts_page(video_id: str) -> str:
    """One Short in the Shorts player; ArrowDown moves to the next one in SHORTS_SEQUENCE."""
    index = SHORTS_SEQUENCE.index(video_id) if video_id in SHORTS_SEQUENCE else len(SHORTS_SEQUENCE) - 1
    following = SHORTS_SEQUENCE[min(index + 1, len(SHORTS_SEQUENCE) - 1)]
    if video_id == SHORTS_AD_ID:
        overlay = (
            "<ad-slot-renderer><reels-ad-metadata-view-model style='display:block'>"
            "<ad-badge-view-model>Sponsored</ad-badge-view-model>"
            "<yt-ad-metadata-shape style='display:block'>Fixture shorts ad headline</yt-ad-metadata-shape>"
            "<ad-button-view-model><a href='https://www.googleadservices.com/pagead/aclk?adurl=https%3A%2F%2Fshop.example%2F'>"
            "Shop now</a></ad-button-view-model></reels-ad-metadata-view-model></ad-slot-renderer>"
        )
    else:
        overlay = (
            f"<yt-shorts-video-title-view-model style='display:block'>Short title {video_id}</yt-shorts-video-title-view-model>"
            f"<yt-reel-channel-bar-view-model style='display:block'>@channel_{index} Subscribe</yt-reel-channel-bar-view-model>"
        )
    return page(
        "<div style='height:280px'></div>"
        f"<shorts-video style='display:block;height:300px'>{overlay}</shorts-video>"
        "<script>document.addEventListener('keydown', (e) => {"
        f" if (e.key === 'ArrowDown') location.href = '/shorts/{following}'; }});</script>"
    )


def watch_page(
    current_video_id: str,
    *,
    consent: str | None = None,
    related: bool = True,
    player_ad: bool = False,
    page_data: bool = False,
    layout: str = "legacy",
) -> str:
    cards = list(CARDS["watch_cards"])
    # Rotate per video so consecutive watch pages show different related lists.
    shift = sum(map(ord, current_video_id)) % len(cards)
    cards = cards[shift:] + cards[:shift]
    playing_card = cards[0].replace(card_video_id(cards[0]), current_video_id)
    live_card = live_variant(CARDS["search_cards"][1])
    cards = [radio_start_variant(card) if card_video_id(card) == RADIO_START_VIDEO_ID else card for card in cards]
    if layout == "lockup":
        playing_card, live_card = as_lockup(playing_card), as_lockup(live_card)
        cards = [as_lockup(card) for card in cards]
    sections = [
        "".join(cards[:2]),
        "",
        "".join(cards[2:4]) + playing_card + live_card + "".join(cards[4:]),
    ] if related else []
    related = "".join(
        f'<ytm-item-section-renderer section-identifier="related-items"><lazy-list>{section}</lazy-list>'
        "</ytm-item-section-renderer>"
        for section in sections
    )
    return page(
        topbar(current_video_id)
        + (consent_dialog(consent) if consent else "")
        + (page_data_script(current_video_id, REEL_VIDEO_ID) if page_data else "")
        + "<ytm-watch>"
        + (PLAYER_WITH_AD if player_ad else PLAYER)
        + COMPANION_AD
        + f'<ytm-crawler-description style="display:none"><a href="/watch?v={current_video_id}&t=16s">0:16</a>'
        + "</ytm-crawler-description>"
        + "<ytm-single-column-watch-next-results-renderer>"
        + related
        + "</ytm-single-column-watch-next-results-renderer></ytm-watch>"
    )


def route_handler(
    route,
    request=None,
    *,
    consent: str = "normal",
    abort_video_ids=(),
    related: bool = True,
    player_ad: bool = False,
    page_data: bool = False,
    ip_location: str | None = "IE",
    layout: str = "legacy",
) -> None:
    """Serve fixture pages for m.youtube.com documents and block everything else.

    The cookie dialog is shown until the SOCS cookie is set, on search and watch pages alike.
    Every page carries YouTube's client config with its content location (see client_config),
    unless ip_location is None. layout "lockup" serves the cards as yt-lockup-view-model.
    """
    request = route.request
    parsed = urlparse(request.url)
    if parsed.netloc != "m.youtube.com" or request.resource_type != "document":
        route.abort()
        return
    dialog = None if "SOCS=" in (request.headers.get("cookie") or "") else consent
    if parsed.path == "/results":
        body = search_page(consent=dialog, layout=layout)
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query)["v"][0]
        if video_id in abort_video_ids:
            route.abort()
            return
        body = watch_page(
            video_id, consent=dialog, related=related, player_ad=player_ad, page_data=page_data, layout=layout
        )
    elif parsed.path.startswith("/shorts/"):
        body = shorts_page(parsed.path.split("/")[2])
    else:
        route.abort()
        return
    if ip_location:
        body = body.replace("</head>", client_config(request.headers.get("cookie") or "", ip_location) + "</head>", 1)
    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)


def make_route_handler(**options):
    return partial(route_handler, **options)


def make_flaky_handler(failures: int, error: str = "connectionreset", **options):
    """Drop the first `failures` search-page loads like a broken connection, then serve normally."""
    serve = make_route_handler(**options)
    remaining = [failures]

    def handler(route, request=None):
        request = route.request
        if remaining[0] > 0 and request.resource_type == "document" and urlparse(request.url).path == "/results":
            remaining[0] -= 1
            route.abort(error)
            return
        serve(route)

    return handler
