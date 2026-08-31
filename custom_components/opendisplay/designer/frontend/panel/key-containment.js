/**
 * Keyboard containment (tier-1 round 2, CRITICAL): without this, typing in
 * the designer's YAML editor is largely unusable inside the real HA panel --
 * most keystrokes did nothing, and single letters like e/d/c popped HA's own
 * global quick-bar (entity/device/command search) OVER the editor.
 *
 * Root cause, verified directly against the installed homeassistant-frontend
 * package (home-assistant-frontend==20260826.1, matching this venv's pinned
 * hass_frontend build -- confirmed via that bundle's own source map, which
 * names the exact upstream files below at that exact tag; re-verified
 * against 20260729.7, the version installed earlier in the same review
 * round, after `uv run`'s own floating resolution drifted the venv forward
 * mid-session -- `can-override-input.ts` and `shortcuts.ts` are BYTE-
 * IDENTICAL between the two releases; `quick-bar-mixin.ts` only gained
 * unrelated TypeScript event-cast typing, not a behavior change, so this
 * mechanism is stable across at least those two recent releases, not a
 * one-version fluke):
 *
 *   - HA registers its e/c/d/m/a/? shortcuts globally on `window`, in the
 *     BUBBLE phase, via tinykeys (`src/common/keyboard/shortcuts.ts`
 *     `registerShortcuts()`, `tinykeys(window, wrappedShortcuts)`, line 46;
 *     wired up from `src/state/quick-bar-mixin.ts`'s `_registerShortcut()`,
 *     lines 104-139). A composed, bubbling `keydown` (which is what real
 *     typing dispatches) reaches this listener regardless of how deep in the
 *     DOM -- or how many shadow roots -- it started in; shadow-root
 *     retargeting does NOT protect us here, because...
 *   - ...the "is the user typing in an editable field" gate,
 *     `canOverrideAlphanumericInput` (`src/common/dom/can-override-input.ts`,
 *     the whole file, 36 lines), does not check `Element.isContentEditable`
 *     at all. It only recognizes a fixed tag-name allowlist:
 *     `TEXTAREA`/`INPUT` (non-button-like) directly, plus `HA-MENU`/
 *     `HA-CODE-EDITOR` anywhere in `composedPath()` (HA special-cased its
 *     OWN CodeMirror wrapper by tag name rather than checking
 *     `isContentEditable` generically). The vendored designer's own
 *     CodeMirror 6 instance renders a plain `contenteditable` div with none
 *     of those tag names -- so `canOverrideAlphanumericInput` returns `true`
 *     (shortcuts allowed) even while the cursor is inside our editor, e/c/d
 *     fire `_showQuickBar()`/`preventDefault()` and steal focus into HA's
 *     own dialog, and every subsequent keystroke goes into THAT dialog
 *     instead of back into the designer (matching "most keystrokes don't
 *     type at all" -- not a separate bug, a consequence of focus having
 *     already been stolen by the first e/c/d/m/a keystroke).
 *
 * Fix, host-side (this integration cannot patch home-assistant-frontend):
 * stop keyboard events from ever reaching `window` once they originate
 * inside the designer's own mount, at the one DOM boundary we own -- the
 * panel custom element itself, in the bubble phase, after the shadow root's
 * own listeners (CodeMirror's included) have already run and before the
 * event would otherwise continue bubbling out into the rest of the page.
 * NEVER preventDefault: that would break the browser's own native text
 * editing. HA's global shortcuts going quiet while focus is inside the
 * designer is intentional and acceptable (maintainer ruling) -- the
 * designer owns its own surface once the user is working inside it.
 *
 * Kept in its own module (imported by the panel wrapper) so it can be unit
 * tested with plain `node --test`, without executing the vendored designer
 * bundle (which assumes a real browser) just to import one function.
 *
 * @param {EventTarget} host - the panel custom element (`this` from
 *   connectedCallback), i.e. the shadow root's host.
 * @returns {() => void} disposer -- call from disconnectedCallback.
 */
export function containKeyEvents(host) {
  const stop = (event) => {
    event.stopPropagation();
  };
  const types = ['keydown', 'keyup', 'keypress'];
  for (const type of types) {
    host.addEventListener(type, stop);
  }
  return () => {
    for (const type of types) {
      host.removeEventListener(type, stop);
    }
  };
}
