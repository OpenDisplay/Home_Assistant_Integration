/**
 * Navigate-away warning (tier-1 round 2, finding 6) -- INTERIM until the
 * designer's own export-aware dirty flag (designer#167) ships. Every
 * committed edit updates `getStatus().lastEditAt` (`null` before any edit
 * this mount); a non-null value means there is unsaved work a tab close or
 * reload would silently discard (the maintainer lost work this way).
 *
 * `beforeunload` is the only hook that actually covers this: it fires for a
 * tab close, a reload, and a browser-chrome navigation (typed URL,
 * back/forward, bookmark) alike. It does NOT fire for HA's own in-app
 * sidebar navigation (a SPA route swap, no real page unload) -- investigated
 * and confirmed there is no equivalent hook for that: HA's router just
 * disconnects the panel custom element like any other DOM removal, and the
 * Custom Elements spec has no cancelable "about to be removed" callback
 * (`disconnectedCallback` runs AFTER removal, with no way to veto it) -- nor
 * does the designer's own host contract (`odl-drawcustom-designer.d.ts`)
 * expose one. Said honestly rather than silently only covering part of the
 * problem: sidebar-link navigation away from unsaved work is NOT caught by
 * this fix.
 *
 * Kept in its own module, like key-containment.js, so the pure predicate is
 * unit-testable with plain `node --test` without a real `beforeunload` event.
 */

/**
 * @param {{ getStatus(): { lastEditAt: number | null } } | null | undefined} handle
 * @returns {boolean} true if there is a committed edit this mount hasn't
 *   exported/sent -- the designer itself does not distinguish "sent" from
 *   "not sent" here (that distinction is exactly what designer#167 adds);
 *   for now, any edit at all counts.
 */
export function hasUnsavedWork(handle) {
  return handle?.getStatus().lastEditAt != null;
}

/**
 * beforeunload handler factory -- call the returned function with the real
 * `beforeunload` Event. Never calls anything on `event` unless there is
 * actually unsaved work, so a designer with nothing typed never nags.
 *
 * @param {() => ({ getStatus(): { lastEditAt: number | null } } | null | undefined)} getHandle
 *   thunk, not a snapshot -- `this._handle` can be reassigned across a
 *   remount, so the listener must read it fresh on every unload attempt,
 *   not close over whatever it was when connectedCallback ran.
 */
export function makeBeforeUnloadHandler(getHandle) {
  return (event) => {
    if (!hasUnsavedWork(getHandle())) return;
    event.preventDefault();
    event.returnValue = '';
  };
}
