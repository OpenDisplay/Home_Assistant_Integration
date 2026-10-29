/**
 * Keyboard containment (tier-1 round 2, CRITICAL; narrowed from a blanket
 * version in a follow-up fix round -- see "Residual tradeoff" below).
 * Without this, typing in the designer's YAML editor is largely unusable
 * inside the real HA panel -- most keystrokes did nothing, and single
 * letters like e/d/c popped HA's own global quick-bar (entity/device/
 * command search) OVER the editor.
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
 * SELECTIVE, not blanket (fixed in a follow-up round -- a first version
 * stopped propagation for every key event unconditionally, which also
 * silently killed the vendored designer's OWN window-level keyboard
 * shortcuts: undo/redo and Escape/Delete/Backspace/Arrow-nudge are
 * registered by the designer itself on `window`, not inside the shadow
 * root (`src/ui/lib/canvas-keyboard.ts`'s window keydown listener, and
 * `src/ui/lib/undo-keyboard.ts`'s `Z$t`/`Q$t` undo/redo predicates --
 * verified directly against the vendored bundle: search it for
 * `canvas-keyboard.ts`/`undo-keyboard.ts` to find the exact minified
 * call sites). A blanket stop broke every one of them whenever the event
 * happened to pass through this panel's own host element first -- which,
 * being the panel's own DOM ancestor, is always). Two conditions,
 * mirroring the designer's OWN editable-target guard (`X$t` in
 * `canvas-keyboard.ts`, reused here as `isEditableTarget` below -- same
 * `.cm-editor`/`INPUT`/`TEXTAREA`/`SELECT`/`isContentEditable` checks, so
 * "should this reach the designer's own canvas shortcuts" and "should
 * this reach HA's quick-bar" agree on what counts as "the user is
 * editing text"):
 *
 *   (a) the event's target is editable (mirrors the designer's own guard) --
 *       contained regardless of which key, so ordinary typing (including
 *       Backspace/Delete/Escape/Arrows used for text editing, and ctrl+z
 *       used for CodeMirror's own text-undo) never also reaches the
 *       designer's window-level canvas handler OR HA's window-level
 *       shortcuts. This matches the designer's own intent: `X$t` returns
 *       `false` for an editable target specifically so the canvas handler
 *       bails out before ever checking undo/redo/delete/arrows -- the
 *       designer itself does not want its OWN shortcuts firing during a
 *       text edit either.
 *   (b) the event is an UNMODIFIED alphanumeric key (no ctrl/meta/alt --
 *       Shift is not treated as a modifier here, so Shift+letter is still
 *       contained too; see "Residual tradeoff" below), regardless of
 *       target -- the designer binds no bare letters/digits anywhere
 *       (`canvas-keyboard.ts`'s switch only handles Escape/Delete/
 *       Backspace/Arrow* by key name; `Z$t`/`Q$t` both require ctrl/meta),
 *       so nothing of the designer's own breaks, and this is what keeps
 *       a bare 'e'/'c'/'d' from opening HA's quick-bar even when the
 *       CANVAS (not the YAML editor) has focus -- (a) alone would not
 *       catch that case, since the canvas is not an editable target.
 *
 * Everything else -- Delete, Backspace, Escape, Arrow keys, and every
 * ctrl/meta combo (including undo/redo) -- propagates all the way to
 * `window` from a non-editable (canvas) target, exactly as before this
 * fix existed: the designer's own window listener sees them and its
 * undo/redo/delete-element/nudge/deselect all work.
 *
 * Residual tradeoff, disclosed rather than left implicit: bare-letter HA
 * shortcuts (e/c/d/m/a) are suppressed PANEL-WIDE by design (condition (b)
 * has no target check) -- not just while text-editing, also while the
 * canvas or any other part of the panel has focus. This is deliberate
 * (the designer has no bare-letter shortcuts of its own to protect, and
 * suppressing HA's quick-bar only while literally inside the CodeMirror
 * editor would leave it popping up over the canvas mid-design). If HA
 * ever adds a global CTRL/META-modified shortcut the designer also needs
 * (today it doesn't: only ctrl/meta+z and ctrl+y/ctrl+shift+z), this file
 * would need a third, narrower condition -- revisit then, don't
 * pre-emptively guess at one now. Known, same-shape gap in the other
 * direction: `?` and other non-alphanumeric printable keys (condition (b)
 * only matches `[a-zA-Z0-9]`) still reach `window` from a non-editable
 * (canvas) target, so HA's own `?` shortcuts-dialog CAN pop over the
 * designer while the canvas -- not the editor -- has focus. Deliberate,
 * not an oversight: the designer binds no `?` of its own either, so
 * nothing of its own is at risk; add a third condition here if this ever
 * actually annoys someone, rather than widening (b) pre-emptively for a
 * case nobody's hit yet.
 *
 * NEVER preventDefault: that would break the browser's own native text
 * editing (and the designer's own default-prevented handling for the keys
 * it does bind). HA's global shortcuts going quiet while focus is inside
 * the designer, or while a bare letter is pressed anywhere in the panel,
 * is intentional and acceptable (maintainer ruling) -- the designer owns
 * its own surface.
 *
 * Kept in its own module (imported by the panel wrapper) so it can be unit
 * tested with plain `node --test`, without executing the vendored designer
 * bundle (which assumes a real browser) just to import one function.
 */

/**
 * Mirrors the designer's own `X$t` (`src/ui/lib/canvas-keyboard.ts`,
 * compiled into the vendored bundle): is the event's real origin (the
 * innermost node in `composedPath()`, not the possibly-retargeted
 * `event.target`) an element a user could reasonably be typing text into.
 *
 * Duck-typed (`typeof target.closest === 'function'`) rather than `target
 * instanceof HTMLElement`, which is what `X$t` itself checks -- deliberate:
 * this file has no DOM to `instanceof` against under plain `node --test`
 * (no jsdom, no npm package manager in this repo at all), and every real
 * target this check ever sees (CodeMirror's own contenteditable div, a
 * real `INPUT`/`TEXTAREA`/`SELECT`) is a genuine `HTMLElement` with a real
 * `.closest` either way -- the two checks agree for everything this
 * function is actually asked about.
 *
 * @param {Event} event
 * @returns {boolean}
 */
function isEditableTarget(event) {
  const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
  const target = path[0] ?? event.target;
  if (!target || typeof target.closest !== 'function') return false;
  return !!(
    target.closest('.cm-editor') ||
    target.tagName === 'INPUT' ||
    target.tagName === 'TEXTAREA' ||
    target.tagName === 'SELECT' ||
    target.isContentEditable
  );
}

/**
 * A bare letter/digit key with no ctrl/meta/alt modifier -- the shape of
 * every HA quick-bar single-key shortcut (e/c/d/m/a), and of nothing the
 * designer itself binds. Shift is deliberately NOT treated as a modifier
 * here (see this module's own "Residual tradeoff" doc comment) -- errs
 * toward containing more, not less, since the designer has no Shift+letter
 * bindings to protect either.
 *
 * @param {KeyboardEvent} event
 * @returns {boolean}
 */
function isUnmodifiedAlphanumeric(event) {
  if (event.ctrlKey || event.metaKey || event.altKey) return false;
  return typeof event.key === 'string' && event.key.length === 1 && /[a-zA-Z0-9]/.test(event.key);
}

/**
 * @param {EventTarget} host - the panel custom element (`this` from
 *   connectedCallback), i.e. the shadow root's host.
 * @returns {() => void} disposer -- call from disconnectedCallback.
 */
export function containKeyEvents(host) {
  const stop = (event) => {
    if (isEditableTarget(event) || isUnmodifiedAlphanumeric(event)) {
      event.stopPropagation();
    }
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
