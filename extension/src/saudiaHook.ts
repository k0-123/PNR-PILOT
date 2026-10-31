// Runs in the Saudia page itself (manifest: world MAIN, document_start), before the site's app.
// Saudia's manage-booking app loads the booking from its own API (dapi.saudia.com): first
// /purchase/order-previews (e-ticket -> booking reference), then /purchase/orders/<PNR> (the full
// booking). The page often shows only a blank error box (group bookings) although the booking was
// returned, so content.ts reads these answers instead of the page. This script only copies the
// responses the site already receives; it sends nothing and changes no request.

export {}; // a module: its names stay out of the global scope

const WATCH = /dapi\.saudia\.com\/purchase\/(order-previews|orders\/)/;

function forward(url: string, status: number, body: string): void {
  if (WATCH.test(url)) window.postMessage({ __gdsApi: true, url: url.split("?")[0], status, body }, location.origin);
}

const open = XMLHttpRequest.prototype.open;
const send = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open = function (this: XMLHttpRequest & { __gdsUrl?: string }, ...args: unknown[]) {
  this.__gdsUrl = String(args[1]);
  return (open as (...a: unknown[]) => void).apply(this, args);
} as typeof XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.send = function (this: XMLHttpRequest & { __gdsUrl?: string }, ...args: unknown[]) {
  this.addEventListener("load", () => {
    try {
      forward(this.__gdsUrl ?? "", this.status, typeof this.response === "string" ? this.response : JSON.stringify(this.response));
    } catch {
      // unreadable response type: ignore
    }
  });
  return (send as (...a: unknown[]) => void).apply(this, args);
};

const fetch0 = window.fetch;
window.fetch = async function (...args: Parameters<typeof fetch>) {
  const res = await fetch0.apply(this, args);
  const url = typeof args[0] === "string" ? args[0] : args[0] instanceof URL ? args[0].href : args[0].url;
  if (WATCH.test(url)) res.clone().text().then((b) => forward(url, res.status, b), () => undefined);
  return res;
};
