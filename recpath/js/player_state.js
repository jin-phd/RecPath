() => {
  // State of the watch-page player and the companion slot under it, sampled while the runner
  // stays on a video. Read-only: never clicks anything.
  const text = (selector, root = document) => {
    const el = root.querySelector(selector);
    return el ? (el.innerText || el.textContent).trim().replace(/\s+/g, " ") || null : null;
  };
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

  const player = document.querySelector("#movie_player, .html5-video-player");
  const adShowing = !!player && (player.classList.contains("ad-showing") || player.classList.contains("ad-interrupting"));
  const video = document.querySelector("video");
  const visitLink = document.querySelector(".ytp-ad-button[href], .ytp-ad-visit-advertiser-button[href], .ytp-ad-player-overlay a[href]");

  const companionCard = document.querySelector("ytm-companion-slot ytm-companion-ad-renderer");
  let companion = null;
  if (companionCard) {
    const cta = companionCard.querySelector('a[href*="adurl="], a[href]');
    const headline = text(".YtmCompanionAdRendererHeadline", companionCard);
    const description = text(".YtmCompanionAdRendererDescription", companionCard);
    // One of the two lines is usually the advertiser's display domain (e.g. "screwfix.ie").
    const domainLike = (value) => !!value && /^(www\.)?[\w-]+(\.[\w-]+)+$/.test(value);
    companion = {
      headline,
      description,
      cta_host: cta ? hostOf(cta.getAttribute("href")) : null,
      advertiser: domainLike(description) ? description : domainLike(headline) ? headline : null,
    };
  }

  return {
    player_found: !!player,
    ad_showing: adShowing,
    ad_title: adShowing ? text(".ytp-ad-text.ytp-video-ad-top-bar-title, .ytp-ad-player-overlay-title, .ytp-ad-preview-text") : null,
    pod_text: adShowing ? text(".ytp-ad-player-overlay-instream-info, .ytp-ad-simple-ad-badge, .ytp-ad-badge__text--clean-player") : null,
    advertiser_host: adShowing && visitLink ? hostOf(visitLink.getAttribute("href")) : null,
    visit_text: adShowing ? text(".ytp-ad-button, .ytp-ad-visit-advertiser-button") : null,
    skippable: adShowing ? !!document.querySelector(".ytp-ad-skip-button-container, .ytp-skip-ad-button, .ytp-ad-skip-button-modern, .ytp-ad-skip-button") : null,
    companion,
    paid_promotion: !!document.querySelector(".ytp-paid-content-overlay, .ytm-paid-content-overlay-renderer, ytm-paid-content-overlay-renderer"),
    video_time: video ? Math.round(video.currentTime * 10) / 10 : null,
    video_paused: video ? video.paused : null,
  };
}
