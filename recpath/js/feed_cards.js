(args) => {
  // Card-scoped reader for the m.youtube.com search feed and the watch-page "related" list.
  // Called from browser_observation.py with page.evaluate(script, {mode, pageKind, ...}).
  //   mode "extract": one record per result card, in document order
  //   mode "count":   number of cards currently rendered in the feed (used while scrolling)
  //   mode "ids":     the first `limit` video ids in the feed (used to detect a refreshed list)
  //   mode "mark_stale" / "fresh_count": flag the current cards before navigating, then count
  //                   cards rendered since (the related list of the new video)
  //   mode "link":    a visible link element for videoId, so Python can tap it
  //   mode "mark":    scroll the card for videoId into view and outline it for the "selected" screenshot
  //   mode "unmark":  remove that outline again
  // yt-lockup-view-model is YouTube's newer video card; some visitors get it in place of
  // ytm-video-with-context-renderer on search and watch pages.
  const LOCKUP = "yt-lockup-view-model";
  const CARD = [
    "ytm-video-with-context-renderer", "ytm-compact-video-renderer", LOCKUP,
    "ytm-shorts-lockup-view-model", "ytm-shorts-lockup-view-model-v2", "ytm-reel-item-renderer",
    "ytm-compact-playlist-renderer", "ytm-compact-radio-renderer", "ytm-compact-channel-renderer",
    "ytm-compact-movie-renderer", "ytm-movie-renderer",
    "ytm-ad-slot-renderer", "ytm-promoted-sparkles-web-renderer", "ytm-promoted-video-renderer",
    "ytm-in-feed-ad-layout-renderer",
  ].join(",");
  const SCOPE = args.pageKind === "watch"
    ? 'ytm-item-section-renderer[section-identifier="related-items"]'
    : "ytm-search ytm-section-list-renderer";
  // The fixed top bar carries an "Open App" deep link to the playing video; the hidden crawler
  // description carries chapter links. Neither is a recommendation.
  const EXCLUDE = [
    "ytm-mobile-topbar-renderer", "ytm-crawler-description", "ytm-companion-slot",
    "ytm-structured-description-content-renderer", "ytm-engagement-panel-section-list-renderer",
    "ytm-comment-thread-renderer", "ytm-playlist-panel-renderer",
  ].join(",");
  const AD_TAG = /(^|-)(ad|ads|promoted|sponsor|sponsored|pyv)(-|$)/;
  const LINK = 'a[href*="watch?v="], a[href*="/shorts/"], a[href*="/playlist?list="], a[href^="/@"], a[href^="/channel/"]';
  const MARK_ATTR = "data-recpath-selected";
  const STALE_ATTR = "data-recpath-stale";

  const text = (el) => (el ? el.textContent.trim().replace(/\s+/g, " ") : null);
  // Watch pages can split recommendations over several "related-items" sections. Without a
  // feed section there is nothing to read: falling back to the whole page would record
  // whatever else is on it (e.g. the previous search results during a slow navigation).
  const scopes = Array.from(document.querySelectorAll(SCOPE))
    .filter((el, i, all) => !all.some((other) => other !== el && other.contains(el)));
  const roots = scopes;

  const linkFor = (card) => Array.from(card.querySelectorAll(LINK)).find((a) => {
    const href = a.getAttribute("href") || "";
    return href.startsWith("/") || href.startsWith("https://m.youtube.com/") || href.startsWith("https://www.youtube.com/");
  }) || null;

  const videoIdFor = (url) => {
    if (!url) return null;
    if (url.pathname.startsWith("/shorts/")) return url.pathname.split("/")[2] || null;
    return url.searchParams.get("v");
  };

  const cards = () => {
    const all = roots
      .flatMap((root) => Array.from(root.querySelectorAll(CARD)))
      .filter((card) => !card.closest(EXCLUDE));
    // Keep only the outermost card when renderers are nested.
    return all.filter((card) => !all.some((other) => other !== card && other.contains(card)));
  };

  const channelFromAria = (card) => {
    const el = card.querySelector('[aria-label^="Go to channel "]');
    return el ? el.getAttribute("aria-label").replace(/^Go to channel /, "").trim() : null;
  };

  const cardForVideo = (videoId) => cards().find((card) => {
    const a = linkFor(card);
    if (!a) return false;
    return videoIdFor(new URL(a.getAttribute("href"), location.href)) === videoId;
  }) || null;

  if (args.mode === "count") return cards().length;

  if (args.mode === "ids") {
    const ids = [];
    for (const card of cards()) {
      const a = linkFor(card);
      const id = a ? videoIdFor(new URL(a.getAttribute("href"), location.href)) : null;
      if (id && !ids.includes(id)) ids.push(id);
      if (ids.length >= (args.limit || 3)) break;
    }
    return ids;
  }

  if (args.mode === "mark_stale") {
    cards().forEach((card) => card.setAttribute(STALE_ATTR, "1"));
    return true;
  }

  if (args.mode === "fresh_count") return cards().filter((card) => !card.hasAttribute(STALE_ATTR)).length;

  if (args.mode === "link") {
    const card = cardForVideo(args.videoId);
    if (!card) return null;
    const links = Array.from(card.querySelectorAll("a[href]")).filter((a) => {
      const href = a.getAttribute("href") || "";
      if (!(href.includes(`v=${args.videoId}`) || href.includes(`/shorts/${args.videoId}`))) return false;
      const r = a.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    });
    return links[0] || linkFor(card);
  }

  if (args.mode === "mark") {
    const card = cardForVideo(args.videoId);
    if (!card) return false;
    if (!args.keepScroll) card.scrollIntoView({block: "center"});
    document.querySelectorAll(`[${MARK_ATTR}]`).forEach((box) => box.remove());
    // An overlay box, because thumbnails paint over an outline drawn on the card itself.
    const rect = card.getBoundingClientRect();
    const box = document.createElement("div");
    box.setAttribute(MARK_ATTR, "1");
    Object.assign(box.style, {
      position: "absolute",
      top: `${rect.top + window.scrollY}px`,
      left: `${rect.left + window.scrollX}px`,
      width: `${rect.width}px`,
      height: `${rect.height}px`,
      border: "4px solid #ff0033",
      boxSizing: "border-box",
      pointerEvents: "none",
      zIndex: "2147483647",
    });
    document.body.appendChild(box);
    return true;
  }

  if (args.mode === "unmark") {
    document.querySelectorAll(`[${MARK_ATTR}]`).forEach((box) => box.remove());
    return true;
  }

  const items = [];
  for (const card of cards()) {
    const tag = card.tagName.toLowerCase();
    const sectionIndex = roots.findIndex((root) => root.contains(card));
    const a = linkFor(card);
    const rect = card.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const href = a ? a.getAttribute("href") : null;
    const url = href ? new URL(href, location.href) : null;
    // A lockup shows the duration (or LIVE / UPCOMING) as a badge on the thumbnail instead.
    const lockupTime = tag === LOCKUP
      ? Array.from(card.querySelectorAll("yt-thumbnail-badge-view-model badge-shape"))
        .find((badge) => /^((\d+:)?\d{1,2}:\d{2}|live|upcoming)$/i.test(text(badge) || "")) || null
      : null;
    const timeEl = card.querySelector("ytm-thumbnail-overlay-time-status-renderer") || lockupTime;
    const timeStyle = lockupTime
      ? (/^(live|upcoming)$/i.test(text(lockupTime)) ? text(lockupTime).toUpperCase() : "DEFAULT")
      : timeEl ? timeEl.getAttribute("data-style") : null;
    const badges = Array.from(card.querySelectorAll("badge-shape"))
      .map((badge) => badge.getAttribute("aria-label") || text(badge))
      .filter(Boolean);
    const isAd = AD_TAG.test(tag)
      || !!card.querySelector('badge-shape.ytBadgeShapeAd, a[href*="googleadservices.com"], a[href*="doubleclick.net"]');

    let label = "normal_video";
    if (isAd) label = "ad";
    else if (href && url.pathname.startsWith("/shorts/")) label = "short";
    else if (tag.includes("shorts") || tag.includes("reel")) label = "short";
    else if (tag.includes("playlist")) label = "playlist";
    // Ordinary videos often link with &list=RD<id>&start_radio=1; only a card without a
    // duration overlay (or a radio renderer) is a Mix.
    else if (tag.includes("radio") || (!timeEl && url && (url.searchParams.get("list") || "").startsWith("RD"))) label = "mix";
    // A lockup for a playlist links to it (or to its first video with &list=) and has no duration.
    else if (tag === LOCKUP && !timeEl && url && (url.pathname === "/playlist" || url.searchParams.get("list"))) label = "playlist";
    else if (tag.includes("channel")) label = "channel";
    else if (tag.includes("movie") || badges.some((b) => /preview only|buy or rent|free with ads/i.test(b))) label = "movie";
    else if (timeStyle === "LIVE") label = "live";
    else if (timeStyle === "UPCOMING") label = "upcoming";

    const titleEl = card.querySelector("h3.media-item-headline, .shortsLockupViewModelHostMetadataTitle, h3");
    const titleAriaEl = titleEl ? (titleEl.matches("[aria-label]") ? titleEl : titleEl.querySelector("[aria-label]")) : null;
    const lockupChannel = card.querySelector(".shortsLockupViewModelHostOutsideMetadataTitle a[title]");
    const channelName = text(card.querySelector(".ytmBadgeAndBylineRendererItemByline"))
      || (lockupChannel ? lockupChannel.getAttribute("title") : null)
      || channelFromAria(card)
      // A lockup without a channel avatar names the channel first in its metadata rows.
      || (tag === LOCKUP ? text(card.querySelector('yt-content-metadata-view-model [class*="MetadataText"]')) : null);
    let title = text(titleEl);
    if (!title && label === "channel") {
      title = channelFromAria(card) || text(card.querySelector("h4, [role=heading]"))
        || (card.textContent || "").trim().replace(/\s+/g, " ").slice(0, 120) || null;
    }
    const channelLink = card.querySelector('ytw-channel-thumbnail-with-link-renderer a[href], a[href^="/@"], a[href^="/channel/"]');
    const shelf = card.closest("grid-shelf-view-model, ytm-reel-shelf-renderer");
    const thumb = card.querySelector("a.media-item-thumbnail-container, .shortsLockupViewModelHostThumbnailContainer, yt-thumbnail-view-model, img");
    const thumbRect = thumb ? thumb.getBoundingClientRect() : null;

    items.push({
      label,
      card_tag: tag,
      href,
      video_id: videoIdFor(url),
      playlist_id: url ? url.searchParams.get("list") : null,
      start_radio: url ? url.searchParams.get("start_radio") === "1" : false,
      pp: url ? url.searchParams.get("pp") : null,
      title,
      title_aria: titleAriaEl ? titleAriaEl.getAttribute("aria-label") : null,
      channel_name: channelName || (label === "channel" ? title : null),
      channel_url: channelLink ? channelLink.getAttribute("href") : null,
      duration_text: text(timeEl),
      time_style: timeStyle,
      badges,
      shelf: shelf ? {
        tag: shelf.tagName.toLowerCase(),
        title: text(shelf.querySelector("h2, [role=heading], .yt-shelf-header-layout__title")),
      } : null,
      thumb_aspect: thumbRect && thumbRect.width ? Math.round((thumbRect.height / thumbRect.width) * 100) / 100 : null,
      doc_top: Math.round(rect.top + window.scrollY),
      left: Math.round(rect.left),
      section_index: sectionIndex,
      stale: card.hasAttribute(STALE_ATTR),
    });
  }
  items.sort((x, y) => x.doc_top - y.doc_top || x.left - y.left);

  // Video links in the feed that no known card type covers: a sign that YouTube changed its
  // layout. Record their nearest custom-element ancestors so the change can be diagnosed.
  const known = cards();
  const unmatched = {count: 0, ancestors: {}};
  roots.flatMap((root) => Array.from(root.querySelectorAll('a[href*="watch?v="], a[href*="/shorts/"]'))).forEach((a) => {
    if (a.closest(EXCLUDE) || known.some((card) => card.contains(a))) return;
    unmatched.count += 1;
    let el = a.parentElement;
    while (el && !el.tagName.includes("-")) el = el.parentElement;
    const key = el ? el.tagName.toLowerCase() : "none";
    unmatched.ancestors[key] = (unmatched.ancestors[key] || 0) + 1;
  });

  return {
    scope_found: scopes.length > 0,
    section_count: scopes.length,
    unmatched_links: unmatched,
    current_video_id: new URL(location.href).searchParams.get("v"),
    viewport_height: window.innerHeight,
    document_height: document.documentElement.scrollHeight,
    items,
  };
}
