() => {
  // The Short currently on screen in the Shorts feed (/shorts/<id>). The feed is a carousel
  // of several items, so pick the elements nearest the middle of the viewport.
  const centre = window.innerHeight / 2;
  const nearest = (selector) => {
    let best = null;
    let bestDistance = Infinity;
    document.querySelectorAll(selector).forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0 || r.bottom < 0 || r.top > window.innerHeight) return;
      const distance = Math.abs((r.top + r.bottom) / 2 - centre);
      if (distance < bestDistance) {
        best = el;
        bestDistance = distance;
      }
    });
    return best;
  };
  const text = (el) => (el ? (el.innerText || el.textContent).trim().replace(/\s+/g, " ") || null : null);
  const hostOf = (href) => {
    if (!href) return null;
    try {
      const url = new URL(href, location.href);
      const target = url.searchParams.get("adurl");
      if (target) return new URL(target).host;
      // A click tracker without its destination says nothing about the advertiser.
      return /(^|\.)(googleadservices|doubleclick|googlesyndication)\./.test(url.host) ? null : url.host;
    } catch (e) {
      return null;
    }
  };

  const path = location.pathname;
  const videoId = path.startsWith("/shorts/") ? path.split("/")[2] || null : null;
  const adMeta = nearest("reels-ad-metadata-view-model, ad-slot-renderer reels-player-overlay-layout-view-model");
  const adBadge = nearest("ad-badge-view-model");
  let ad = null;
  if (adMeta || adBadge) {
    const root = adMeta || adBadge.closest("ad-slot-renderer") || adBadge.parentElement;
    const cta = root ? root.querySelector('a[href*="adurl="], a[href^="http"]') : null;
    ad = {
      advertiser: text(root && root.querySelector("ad-avatar-view-model ~ *, .ytAdMetadataShapeHeadline, yt-ad-metadata-shape [class*='Title']")),
      headline: text(root && root.querySelector("yt-ad-metadata-shape, [class*='Headline'], [class*='headline']")),
      cta_text: text(root && root.querySelector("ad-button-view-model")),
      cta_host: cta ? hostOf(cta.getAttribute("href")) : null,
    };
  }
  return {
    video_id: videoId,
    url: location.href,
    title: ad ? null : text(nearest("yt-shorts-video-title-view-model")),
    channel_name: ad ? null : (text(nearest("yt-reel-channel-bar-view-model")) || "").replace(/\s*Subscribed?$/, "") || null,
    is_ad: !!ad,
    ad,
  };
}
