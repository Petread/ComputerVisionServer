export function shouldHideStatusHeader(pathname: string) {
  return /^\/pipelines\/[^/]+/.test(pathname) || /^\/channels(?:\/|$)/.test(pathname);
}
