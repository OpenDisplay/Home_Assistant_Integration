/**
 * Navigate-away warning (tier-1 round 2, finding 6) -- INTERIM until the
 * designer's own export-aware dirty flag (designer#167) ships. Every
 * committed edit updates `getStatus().lastEditAt` (`null` before any edit
 * this mount); a non-null value means there is unsaved work a tab close or
 * reload would silently discard (the maintainer lost work this way).
 *
 * `beforeunload` is the only hook that actually covers this: it fires for a
 * tab close, a reload, and a browser-chrome navigation (typed URL,
 * back/forward, bookmark) alike.
 *
 * ****************************************************************
 * * IT DOES NOT FIRE FOR HA'S OWN IN-APP SIDEBAR NAVIGATION.      *
 * * Clicking Overview/Settings/another sidebar item -- the        *
 * * single most obvious way to "leave the page" while testing --  *
 * * is a SPA route swap, not a real page unload, and shows NO     *
 * * warning even with unsaved work. This is not a bug in the code *
 * * below; it is a hard platform limitation (see next paragraph). *
 * * Verify THIS fix with an actual reload, tab close, or a typed  *
 * * URL/bookmark -- NOT a sidebar click.                          *
 * ****************************************************************
 *
 * Investigated and confirmed there is no equivalent hook for in-app
 * navigation: HA's router just disconnects the panel custom element like
 * any other DOM removal, and the Custom Elements spec has no cancelable
 * "about to be removed" callback (`disconnectedCallback` runs AFTER
 * removal, with no way to veto it) -- nor does the designer's own host
 * contract (`odl-drawcustom-designer.d.ts`) expose one.
 *
 * Follow-up investigation (a maintainer report of "couldn't trigger the
 * browser-level warning on leaving the page"): live-verified end to end in
 * the real harness with a temporary diagnostic log (never committed) that
 * `beforeunload` DOES fire while this panel is mounted, `getHandle()`
 * DOES return the live, non-destroyed handle (not stale, not null) at
 * fire time, `getStatus().lastEditAt` DOES read as the real non-null
 * timestamp after an edit, and this code DOES reach the
 * `preventDefault()`/`returnValue` branch -- every step of this module's
 * own logic executes exactly as designed. The actual native "leave site?"
 * dialog itself is what's fundamentally UNTESTABLE from here (browser
 * automation harnesses -- this repo's own included -- auto-dismiss
 * `beforeunload` prompts so tests don't hang forever waiting for a human;
 * a real, unattended Chrome shows the dialog under the exact same
 * preventDefault()+returnValue contract this code already satisfies). Most
 * likely explanation for the report, given the above: the maintainer's
 * first instinct for "leave the page" during manual testing was almost
 * certainly clicking another sidebar item -- the in-app-navigation gap
 * this doc comment already described, just not loudly enough. Reworded
 * for visibility rather than left to be found by reading past the first
 * paragraph.
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

/**
 * Registration, bundled with its own disposer -- same shape as
 * `containKeyEvents` (key-containment.js), and for the same reason:
 * `opendisplay-designer-panel.js`'s `connectedCallback`/
 * `disconnectedCallback` call exactly this, one line each, so the actual
 * registration timing (does connectedCallback wire this up unconditionally,
 * synchronously, regardless of whether mount succeeded?) is unit-testable
 * against a fake `window`-shaped object instead of only being verifiable by
 * reading the call site and trusting it. `_mount()` in the panel wrapper is
 * itself synchronous (no `await` before this runs) and any mount failure is
 * caught internally there -- this call always runs.
 *
 * @param {{ addEventListener: Function, removeEventListener: Function }} win
 *   real `window` in production; a fake with the same two methods in tests.
 * @param {() => ({ getStatus(): { lastEditAt: number | null } } | null | undefined)} getHandle
 * @returns {() => void} disposer -- call from disconnectedCallback.
 */
export function installUnsavedWorkWarning(win, getHandle) {
  const handler = makeBeforeUnloadHandler(getHandle);
  win.addEventListener('beforeunload', handler);
  return () => {
    win.removeEventListener('beforeunload', handler);
  };
}
