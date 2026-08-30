export declare type AssetKind = 'font' | 'image';

/**
 * Read-only snapshot of designer status (issue #133, ADR-018's observability
 * clause: "state flows out via a read handle plus optional change
 * notification; status is derived, never authoritative, and carries no
 * designer internals"). Answers "is the YAML good, what did the user just do,
 * how much has changed" without exposing elements, YAML text or any other
 * internal shape.
 *
 * Deliberately small — grow it by maintainer ruling, not speculation.
 * Frozen: a later status is always a new object, never a mutation of one a
 * host already holds.
 */
export declare interface DesignerStatus {
    /** Whether the current YAML document parses and validates. */
    readonly yamlValid: boolean;
    /**
     * A one-line description of the first validation problem, truncated to its
     * first `\n`-delimited line if the underlying parser error is not (a raw
     * YAML syntax error's message can carry a multi-line caret diagram
     * pointing at the offending column). Present only while
     * {@link DesignerStatus.yamlValid} is `false` — a valid document carries no
     * summary rather than an empty or stale one.
     */
    readonly yamlErrorSummary?: string;
    /**
     * Epoch ms, in the **host's** clock domain (`Date.now()` at the moment of
     * the edit) — never a designer-internal or build-time value — of the last
     * user-originated change: typing committed to the canvas, a canvas drag, a
     * property-panel edit, undo/redo. `null` before the user has made any edit
     * during this mount.
     *
     * Never bumped by a host push (`setPayload()`, `setStates()`, …) — a push is
     * the host acting on the designer, not the user acting on it.
     */
    readonly lastEditAt: number | null;
    /**
     * Monotonic counter, incremented once per committed payload change —
     * whether it came from the user (typing, a drag, undo/redo) or from a host
     * `setPayload()` push. A host that only needs "has anything changed since I
     * last looked" can diff this number instead of diffing YAML strings.
     *
     * Granularity, precisely:
     *
     * - **One drag or property-panel drag gesture is one revision**, not one
     *   per pointermove — coalesced the same way the gesture's undo-history
     *   entry is (`beginEditCoalesce`/`endEditCoalesce`), applied once when the
     *   gesture ends. A gesture that starts and ends without net change bumps
     *   nothing.
     * - **A `setPayload()` push with a structurally equal payload (element-wise)
     *   is a no-op for the revision** — dedupe before commit, the same
     *   full-bail pattern the `states`/`actions` channels use for an unchanged
     *   re-push (issue #110): no revision bump, no reset undo history, no
     *   cleared selection. Formatting/comment-only YAML differences dedupe too
     *   (the comparison is over parsed elements, not the YAML text). **Except**
     *   while a pending, not-yet-committed YAML edit the user was typing before
     *   this push exists: `setPayload()` is authoritative over an in-flight
     *   draft regardless of whether the pushed payload turns out to match
     *   what's already committed, so the draft is **always** discarded, deduped
     *   or not — and when there was a real draft to discard, the dedupe is
     *   skipped entirely and the push takes the full apply path instead,
     *   which **does** bump the revision even though the committed elements
     *   end up structurally the same as before. This is deliberate, not an
     *   inconsistency: the push observably changed something (the draft the
     *   editor was showing is gone, replaced by a fresh sync of the pushed
     *   payload) — a plain content comparison alone would miss that and leave
     *   the editor's on-screen text uncorrected.
     * - Every other committed change (a single keystroke's debounced commit, a
     *   click-driven property edit, undo, redo, a genuinely different
     *   `setPayload()` push) bumps exactly once.
     */
    readonly payloadRevision: number;
    /** How many elements are currently selected on the canvas. */
    readonly selectedElementCount: number;
}

/** Theme applied to the mount container (never `document.documentElement`). */
export declare type EmbedTheme = 'light' | 'dark';

/**
 * One host-registered toolbar button (issue #108, ADR-018 actions seam).
 *
 * The host owns what the button means; the designer owns how it looks and
 * reports which one fired, with the current payload (`onAction`). This is a
 * typed, closed button list — deliberately not a plugin API: no host markup,
 * styles or components ever enter the designer's shadow root (ADR-017).
 */
export declare interface HostAction {
    /**
     * Opaque, host-defined identity, echoed back by `onAction`. Also the list's
     * diff key: re-pushing the same id updates that button in place rather than
     * replacing it. Must be unique within a push.
     */
    id: string;
    /** Button text, shown as-is (surrounding whitespace trimmed). Also its accessible name. */
    label: string;
    /**
     * Optional [Material Design Icon](https://pictogrammer.com/library/mdi/)
     * name — the same vocabulary a payload `icon` element accepts, resolved
     * the same way (`mdi:` prefix optional, e.g. `send`, `mdi:home-assistant`).
     * The designer bundles the full MDI set for the payload's icon element
     * anyway, so every one of those names is available to a host at no added
     * bundle size and with no icon dependency of its own. An unknown name is
     * rejected, not ignored.
     */
    icon?: string;
    /** Button chrome; defaults to `'normal'`. */
    severity?: HostActionSeverity;
    /**
     * Whether this action reads the designer's payload; defaults to `true`.
     *
     * A payload-carrying action is disabled while the YAML editor is blocked by
     * a parse/schema error. An action that does *not* need the payload
     * (host-side settings, a reconnect, a help link) sets `false` and stays
     * clickable throughout; it still receives the last valid payload, exactly
     * as {@link MountHandle.getPayload} documents for a blocked document.
     */
    needsPayload?: boolean;
    /**
     * When set, the button renders visibly disabled and this text is what the
     * user gets on hover ("Display offline", "No target selected"). Clearing it
     * in a later push re-enables the button — this is the field hosts re-push
     * as their own state changes.
     */
    disabledReason?: string;
}

/**
 * Third argument of `onAction` — the opaque ids that accompany the payload.
 */
export declare interface HostActionContext {
    /**
     * The selected target's opaque host id (issue #106), or `undefined` when
     * the design is not pinned to one — no targets pushed, none picked yet, or
     * the user switched to the virtual display. Always the same value the last
     * {@link MountOptions.onTargetSelected} call reported.
     */
    targetId?: string;
}

/**
 * Fired when the user clicks a host-registered action. Save and send are host
 * actions — the designer has no save channel of its own (ADR-018).
 *
 * `payload` is the current drawcustom YAML — the exact string
 * {@link MountHandle.getPayload} returns at that instant (same serializer,
 * same pending-edit flush), so a host never has to reconcile two readings of
 * the same design.
 */
export declare type HostActionHandler = (id: string, payload: string, context: HostActionContext) => void;

/**
 * How prominently an action's button warns before it is clicked (issue #108,
 * ADR-018):
 *
 * - `'normal'` (default) — regular button chrome.
 * - `'caution'` — orange: the action reaches beyond the designer, e.g. the
 *   OpenDisplay integration's Send-to-display drives physical hardware.
 * - `'danger'` — red: the action destroys or overwrites something.
 *
 * Severity is *presentation only*. The designer never infers meaning from it
 * — confirmation, auth and the actual call stay host-side.
 */
export declare type HostActionSeverity = 'normal' | 'caution' | 'danger';

/**
 * Host asset resolver — the LAST tier of asset resolution (issue #138,
 * ADR-002 amendment).
 *
 * A payload may reference fonts and images by bare name (`Ubuntu-R.ttf`,
 * `logo.png`): that is how hand-written drawcustom payloads address the
 * integration's own font/media directories. The designer cannot know those
 * directories, so an embedding host supplies a resolver and the designer asks
 * it for any reference it could not resolve locally:
 *
 * 1. local content map (uploaded via Content Manager, ADR-002)
 * 2. bundled assets (`ppb.ttf`, `rbm.ttf`, the showcase image)
 * 3. **this tier** — the host, by name
 *
 * The contract is deliberately `name -> asset`: search paths, media
 * directories and integration layout are the HOST's business, so the designer
 * learns no domain vocabulary (ADR-018). A `null` answer, a rejection, a
 * silence past {@link HOST_ASSET_TIMEOUT_MS} or an out-of-contract value all
 * settle as "not supplied" and reach the user as the existing explicit
 * render-error state for the element referencing it — never a silent skip and
 * never a plausible-looking wrong render (issue #10).
 */
export declare type HostAssetResolver = (kind: AssetKind, name: string) => Promise<Blob | string | null>;

/**
 * What a display *is* — the description driving canvas setup, carried by every
 * {@link HostTarget}. Mirrors the payload shape of the OpenDisplay HA
 * integration's `capabilities.py` so a host-side adapter can pass it through
 * unchanged. All fields optional; anything a display does not declare comes
 * from the designer's canonical defaults, never from the display previously in
 * effect.
 */
export declare interface HostCapabilities {
    /** Physical panel width in pixels (before rotation). */
    pixel_width?: number;
    /** Physical panel height in pixels (before rotation). */
    pixel_height?: number;
    /** Mounting rotation in degrees; only quarter turns are representable. */
    rotation_degrees?: number;
    /** Drawing-surface width after rotation; preferred over pixel_width. */
    render_width?: number;
    /** Drawing-surface height after rotation; preferred over pixel_height. */
    render_height?: number;
    /** OpenDisplay Basic Standard colour scheme (0x00 BW … 0x04 six-color). */
    color_scheme?: number;
    /** Accent color name, e.g. 'red' or 'yellow'. */
    accent_color?: string;
    /** Palette color names, e.g. ['black', 'white', 'red']. */
    available_colors?: string[];
    /** Palette name -> hex map, e.g. { black: '#000000', … }. */
    color_map?: Record<string, string>;
    /** Whether the palette hexes were measured on real hardware. */
    palette_measured?: boolean;
}

/**
 * Second argument of {@link HostPreviewRenderer} — the same
 * "payload plus opaque ids" shape {@link HostActionContext} carries, extended
 * with the geometry and the service options the render depends on.
 */
export declare interface HostPreviewContext {
    /**
     * The selected target's opaque host id (issue #106), or `undefined` when the
     * design is not pinned to one — exactly the value {@link HostActionContext}
     * reports at the same instant.
     */
    targetId?: string;
    /**
     * The canvas the payload is authored against. Always present: a payload's
     * coordinates are meaningless without the surface they refer to, and the
     * designer always knows it — a display-config change (resolution pick,
     * re-orientation) re-requests the render, so a provider that renders at its
     * own idea of the size answers a changed request with an image of the wrong
     * shape, which the designer letterboxes visibly rather than stretching.
     */
    display: HostPreviewDisplayGeometry;
    /**
     * The service options this render must honour. Always present — the designer
     * always knows its own dither mode, so a host never has to guard for it.
     */
    service: HostPreviewServiceOptions;
}

/**
 * The **logical drawing surface** the payload is authored against — the
 * coordinate space its `x`/`y` values live in, and therefore what a host has to
 * render at for the image to mean anything beside the design.
 *
 * Already oriented: {@link HostPreviewDisplayGeometry.width}/`height` are
 * swapped for a quarter turn (issue #139), exactly as upstream `imagegen`
 * creates its canvas before drawing, and `rotation` says which way round the
 * panel holds that surface. Never the raw physical panel size, and never a
 * transform to apply to the returned image.
 */
export declare interface HostPreviewDisplayGeometry {
    width: number;
    height: number;
    /** The orientation `width`/`height` are already expressed in. */
    rotation: 0 | 90 | 180 | 270;
}

/**
 * Renders the current payload host-side and hands the finished image back
 * (issue #109, ADR-018 preview seam).
 *
 * When a host supplies one, the designer offers a **Display preview** toggle
 * next to its canvas heading; turning it on replaces the designer's own
 * client-side preview with this image — a real server-side render, not another
 * client approximation, which is what makes it usable as the
 * [ADR-007](../../docs/adr/ADR-007-hybrid-rendering.md) pixel-parity
 * reference. Every edit affordance is inert while it shows; Copy/Download PNG,
 * zoom and the dither control keep working, and dither re-requests.
 *
 * - Resolve with a `Blob` (`image/png`, `image/*`) or with a URL string
 *   (`data:`, `blob:`, `http(s):`) the designer can point an `<img>` at. A
 *   URL must be readable by the host page for Copy/Download PNG to reach the
 *   bytes.
 * - **Reject to report failure.** The designer shows an explicit error in the
 *   preview area — the rejection's `message` when it has one — and shows no
 *   image at all: a stated error beats a stale or wrong render.
 * - Called again (debounced) whenever anything it was given changes while the
 *   preview shows: a `setPayload()` push, the display config (resolution,
 *   orientation), the selected target, the dither option. Responses are matched
 *   to their request, so a slow answer that a newer request has already
 *   superseded is discarded rather than painted.
 * - Must be a function when supplied — anything else throws out of `mount()`,
 *   like a malformed action or target list does.
 *
 * A stable closure fixed at mount: ADR-018 pushes data, never functions.
 */
export declare type HostPreviewRenderer = (payload: string, context: HostPreviewContext) => Promise<Blob | string>;

/**
 * The service options a host-side render must honour, carried by every
 * {@link HostPreviewContext} (issue #109, ADR-018 preview seam).
 *
 * Deliberately minimal: the designer sends the options it actually owns a
 * control for. The full drawcustom service-options seam is formalized in
 * [issue #105](https://github.com/schlomo/odl-drawcustom-designer/issues/105)
 * — this object is where it lands, and it grows additively (a host reads the
 * fields it knows).
 */
export declare interface HostPreviewServiceOptions {
    /**
     * The dither mode the designer's own dither control currently holds, in the
     * drawcustom `dither` service option's own domain (`src/core/schema/service.ts`):
     * `0` flat, `1`, `2` ordered halftone. The designer's preview control
     * produces `0` or `2` today.
     *
     * A provider **must** honour it: changing the control re-requests the
     * preview, so a provider that ignores the value answers a changed request
     * with an unchanged image and the designer shows a preview that contradicts
     * its own dither setting.
     */
    dither: 0 | 1 | 2;
}

/** A pushed state value with optional attributes and an optional display name. */
export declare interface HostState {
    state: string | number | boolean;
    attributes?: Record<string, unknown>;
    /**
     * Human-readable label for this state key (issue #107, ADR-018 state
     * catalog) — what the referenced-states panel shows instead of the raw key
     * ("Living-room temperature", not `sensor.demo_temperature`).
     *
     * Presentation only, and re-pushable like every other field: templates never
     * see it (a payload reads `states()`/`state_attr()`, which are unaffected by
     * whether the host named the key), and the designer never parses meaning out
     * of it. Surrounding whitespace is trimmed; a blank name counts as none, and
     * an unnamed key shows as its key.
     */
    name?: string;
}

/**
 * Host-pushed states: state key -> state value or {state, attributes, name}.
 * The keys are the host's own identifiers, opaque to the designer — they are
 * what a payload's templates name (`states('…')`, `state_attr('…', '…')`).
 *
 * When provided, this **replaces the State Simulator entirely** (issue #107,
 * ADR-018 Simulator policy): the designer shows a read-only referenced-states
 * panel instead, and the full catalog stays reachable through YAML/template
 * autocomplete.
 *
 * Ownership contract (issue #110): treated as an **immutable snapshot** at
 * the moment `setStates()` is called. Repeated pushes are diffed
 * structurally against the previously applied object to keep a 4x/s
 * full-registry push cheap (no re-render, no template re-evaluation when
 * nothing changed) — that diff compares by value against the retained
 * reference, not by cloning, so **mutate-and-repush is unsupported**:
 * mutating this same object in place and calling `setStates()` again with
 * that reference is invisible to the diff and silently treated as
 * "unchanged". Construct a fresh object per push instead (see
 * docs/embedding.md's `states` section).
 */
export declare type HostStates = Record<string, string | number | boolean | HostState>;

/**
 * Fired on a status transition (issue #133) — a YAML validity flip or a
 * {@link DesignerStatus.payloadRevision} change — debounced so a burst of
 * keystrokes or drag updates yields one call, not one per commit. Not fired
 * for a selection change alone, and not fired for the initial status observed
 * at mount (read {@link MountHandle.getStatus} for that). Delivery is capped
 * at 1 second after the first pending transition, however many times the
 * debounce gets rescheduled in between — something that keeps re-triggering
 * it without ever settling cannot postpone delivery indefinitely.
 *
 * The delivered status is always **live**: read fresh (and flushed, per
 * {@link MountHandle.getStatus}) at the moment the debounce settles, never the
 * value captured when the debounce was scheduled — a flip that reverts to the
 * last-notified truth before the debounce settles delivers no call at all
 * (there is nothing new to report), and a flip that settles on a different
 * truth delivers exactly that, never an intermediate one from partway through
 * the window. A host that reacts to this callback and calls
 * {@link MountHandle.getStatus} inside it always sees the identical value.
 *
 * A stable closure fixed at mount, like `onAction`: ADR-018 pushes data,
 * never functions, so there is no update channel for it.
 */
export declare type HostStatusChangeHandler = (status: DesignerStatus) => void;

/**
 * One display the host knows about (issue #106, ADR-018 targets seam) — the
 * designer's single display channel.
 *
 * The host pushes the list, the designer renders a picker inside its own
 * display-config area, and selecting an entry adopts that display's
 * capabilities behind the existing lock (issue #70). A **one-element list is
 * adopted and locked without a pick** (issue #121): that is how a
 * single-display host says "this is the display". The id is **opaque**: it
 * round-trips through `onTargetSelected` and `onAction`'s context untouched,
 * and the designer never learns what it names (ADR-018: domain-neutral
 * vocabulary — "target", never "entity").
 */
export declare interface HostTarget {
    /**
     * Opaque, host-defined identity, echoed back by `onTargetSelected` and
     * `onAction`. Also the list's diff key: re-pushing the same id keeps a
     * selection on it. Must be unique within a push.
     */
    id: string;
    /** Picker entry text, shown as-is (surrounding whitespace trimmed). */
    label: string;
    /**
     * The display this target *is* ({@link HostCapabilities}) — what the canvas,
     * palette and orientation are set from when this target is adopted. Only the
     * documented fields are retained; the copy the designer keeps is frozen, so
     * mutating the pushed object afterwards cannot change what the picker
     * applies, and a re-push carrying different values is recognised as the host
     * re-defining this display.
     */
    capabilities: HostCapabilities;
}

/**
 * Called when the effective display target changes (issue #106).
 *
 * `null` means "no target": the user picked the virtual display, unlocked the
 * display config, or has not picked anything yet. Fires only on a *change* —
 * including the one a single-element `targets` push makes by adopting that
 * display (issue #121) — and never as a side effect of a `setTargets` push
 * that leaves the selection alone: a push that removes the selected display
 * keeps it (marked stale) rather than switching.
 *
 * A stable closure fixed at mount: ADR-018 pushes data, never functions.
 */
export declare type HostTargetSelectedHandler = (targetId: string | null) => void;

/**
 * Mount the designer into an arbitrary host container (issue #20, ADR-010).
 * Renders into an open shadow root on the container — created here, or
 * reused when the host attached one already (issue #21) — so styles are
 * isolated in both directions.
 *
 * The host pushes data through the returned handle; the designer never
 * persists the payload itself — the host reads the current drawcustom YAML
 * through `getPayload()`, or receives it with the action the user clicked
 * (ADR-018). Invalid `payload` YAML throws synchronously (here and in
 * `setPayload`).
 *
 * This is the embedded host adapter over {@link mountDesigner} (ADR-017);
 * the standalone SPA is a sibling adapter over the same lifecycle.
 */
export declare function mount(container: HTMLElement, options?: MountOptions): MountHandle;

export declare interface MountHandle {
    /**
     * The designer build's version (issue #23, reworked 2026-07-29: git tags
     * are the sole version source, `package.json` stays pinned at `0.0.0`),
     * e.g. `'1.0.0'`. A release build bakes in the tag-derived version via the
     * `APP_VERSION` env var (`tools/autoRelease.ts` sets it, `tools/version.ts`
     * resolves it); any other build (local dev, CI `checks`) falls back to
     * `'0.0.0-dev'`, and Vitest gets the fixed string `'test'`. Same value as
     * the library's `version` export (`src/embed/index.ts`); handy when a host
     * only has the handle.
     */
    readonly version: string;
    /** Unmount the designer and remove everything from the container. */
    destroy(): void;
    /**
     * Push a full replacement state map for template preview. Treat the passed
     * object as an immutable snapshot — see `HostStates`'s ownership contract
     * above; mutating it and calling `setStates()` again with the same reference
     * is unsupported and gets treated as a no-op push.
     *
     * Throws on a malformed map (a non-primitive or missing `state`, a
     * non-object `attributes`, a non-string `name`, or a `states` argument that
     * is not an object) with a message naming the offending key, and **changes
     * nothing** when it does: no values, no names, no Simulator-off latch, and
     * the rejected map never becomes the last-applied push the diff compares
     * against — so the same bad map fails again rather than being deduped, and a
     * corrected one applies normally.
     */
    setStates(states: HostStates): void;
    /** Replace the current payload with new drawcustom YAML (throws on invalid YAML). */
    setPayload(payload: string): void;
    /**
     * Replace the host action buttons (issue #108). Everything pushed at mount
     * is re-pushable (ADR-018), and this is the channel hosts use to keep
     * labels and `disabledReason`s live: push the full list again whenever host
     * state changes (connection lost, target deselected). The designer diffs —
     * an unchanged list costs no re-render — and an empty list removes all
     * action chrome.
     *
     * Throws on a malformed list (unknown `icon` or `severity`, missing or
     * duplicate `id`, missing `label`) without changing what is on screen, and
     * on any non-empty list when `mount()` was given no `onAction` — the
     * handler is fixed at mount, so those buttons could never fire.
     */
    setActions(actions: readonly HostAction[]): void;
    /**
     * Replace the display targets the picker offers (issue #106). Everything
     * pushed at mount is re-pushable (ADR-018), and targets are the channel a
     * host uses as its own display inventory changes: a display that appears
     * shows up in the picker without a reload, and the designer diffs the list
     * so an unchanged re-push costs no re-render.
     *
     * A **one-element** push is adopted and locked straight away (issue #121) —
     * a single display is not a choice — but only while the user has made no
     * display choice of their own; after that, nothing but a pick moves the
     * canvas.
     *
     * Otherwise a push never moves the canvas on its own, and never overrides
     * the user: if it **removes the currently selected target**, the designer keeps that
     * display's last-known capabilities and lock state and marks the selection
     * stale ("display no longer available") instead of silently switching or
     * unlocking. Pushing the target back clears the stale marker.
     *
     * Throws on a malformed list (missing or duplicate `id`, missing `label`,
     * missing `capabilities`) without changing what is on screen.
     */
    setTargets(targets: readonly HostTarget[]): void;
    /** Switch the container-scoped theme. */
    setTheme(theme: EmbedTheme): void;
    /**
     * The designer's current drawcustom YAML payload (issue #104) — exactly the
     * string an `onAction` callback receives at this instant. Same serializer,
     * same underlying elements state; there is no second source of truth.
     *
     * - **Never returns `undefined`, and throws only after `destroy()`** — like
     *   every other method on this handle, it rejects a destroyed mount
     *   (`MountHandle used after destroy()`). On a live mount it always answers
     *   with a string, including in the brief window right after
     *   `mount()`/`mountStandaloneApp()` return but before React has committed
     *   and run its effects, when it reports the bootstrap payload the designer
     *   is about to render.
     * - **Never lags a pending edit.** The YAML editor commits typed text to
     *   the canvas model on an 80ms debounce (or on blur); `getPayload()`
     *   forces that flush first, so a call made mid-keystroke reflects the
     *   text already typed — the same content an action click sends (a click
     *   blurs the editor, which flushes the debounce, before the payload is
     *   read).
     * - **Never resurrects a pre-push draft.** A `setPayload()` push is
     *   authoritative: it discards any debounced edit typed before it, so the
     *   flush above can only ever commit text typed *after* the last push.
     * - **While the YAML editor is blocked** by a parse/schema error (every
     *   payload-carrying action is disabled), returns the last valid payload —
     *   the canvas model is frozen there too, so this is exactly what the last
     *   action would have sent, and the only way to read anything at all.
     *
     * See [`docs/embedding.md`](../../docs/embedding.md#getpayload-issue-104)
     * for the full semantics and rationale.
     */
    getPayload(): string;
    /**
     * The designer's own rasterization of the current payload, right now — the
     * exact bytes its own Copy PNG / Download PNG would produce outside
     * Display preview, full font/renderer fidelity included. Exists so a host
     * with no rendering backend of its own (a demo, a thin adapter) can answer
     * `renderPreview` by reading this instead of writing a second renderer —
     * the same "read access" fix {@link getPayload} is for reading the payload
     * instead of driving the Save button.
     *
     * - **Independent of Display preview.** Always the client-side render, even
     *   while the toggle is on and a host image is showing — a `renderPreview`
     *   provider built on this can therefore never call itself.
     * - **Rejects, never throws synchronously**, while the designer has not
     *   yet committed its first render (the brief window right after
     *   `mount()`/`mountStandaloneApp()` returns) — there is no bootstrap
     *   fallback for a raster the way {@link getPayload} falls back to
     *   serialized YAML, since fonts/assets have not loaded yet either.
     * - Throws (does not reject) `MountHandle used after destroy()`, like every
     *   other method on this handle.
     */
    getPngBlob(): Promise<Blob>;
    /**
     * The designer's current status (issue #133, ADR-018's observability
     * clause) — a small, frozen, derived snapshot; never authoritative, and it
     * carries no designer internals (no elements, no YAML text).
     *
     * **Flushes a pending debounced YAML edit first**, exactly like
     * {@link getPayload} does — the two must never disagree about whether there
     * is unsaved, uncommitted text: a call made moments after typing already
     * reflects the typed edit in `payloadRevision`/`lastEditAt`, not the state
     * as of 80ms ago. Calling `getStatus()` before `getPayload()` or vice versa
     * flushes the same way either order.
     *
     * Always answers synchronously, including in the brief pre-registration
     * window right after `mount()`/`mountStandaloneApp()` returns — before that
     * registration has run, reports a default status (`yamlValid: true`, no
     * edits yet, revision `0`, nothing selected), the same "safe default before
     * the shell exists" shape {@link getPayload}'s bootstrap fallback uses.
     * Throws `MountHandle used after destroy()` like every other method here.
     */
    getStatus(): DesignerStatus;
}

export declare interface MountOptions {
    /** Initial drawcustom YAML payload (list of draw elements). */
    payload?: string;
    /**
     * Initial states for template preview. A mount option *is* an initial push
     * (ADR-018 seam grammar): identical to calling {@link MountHandle.setStates}
     * before the first painted frame, validated the same way — a malformed map
     * throws out of `mount()`, like an invalid `payload`.
     */
    states?: HostStates;
    /** Initial theme; defaults to 'light'. */
    theme?: EmbedTheme;
    /**
     * Initial host action buttons (issue #108). A mount option *is* an initial
     * push (ADR-018 seam grammar): identical to calling
     * {@link MountHandle.setActions} before the first painted frame, and
     * re-pushable from then on. A malformed list throws out of `mount()`, like
     * an invalid `payload`.
     *
     * Requires {@link MountOptions.onAction}: registering actions no one can
     * hear about is rejected rather than rendered.
     */
    actions?: readonly HostAction[];
    /**
     * Called when the user clicks one of the {@link MountOptions.actions}.
     * A stable closure — there is no update channel for it (ADR-018: data is
     * pushed, functions are not) — which is why a mount without it can never
     * take actions, at mount time or through a later `setActions()`.
     */
    onAction?: HostActionHandler;
    /**
     * Initial display targets (issue #106) — the designer's only display
     * channel. A mount option *is* an initial push (ADR-018 seam grammar):
     * identical to calling {@link MountHandle.setTargets} before the first
     * painted frame, and re-pushable from then on. A malformed list throws out
     * of `mount()`, like an invalid `payload`.
     *
     * A list the user can choose between only says what they *can* pick — it
     * never moves the canvas by itself. A **one-element** list says "this is the
     * display": it is adopted and locked straight away, so a single-display host
     * needs no pick and no seeding option (issue #121).
     */
    targets?: readonly HostTarget[];
    /**
     * Called when the selected display target changes, including to `null` for
     * the virtual display. Optional: a host that only needs the id when
     * something happens gets it from `onAction`'s context instead. Hosts that
     * *react* to the selection — re-pushing `actions` with a
     * `disabledReason: 'No display selected'`, say — want this.
     */
    onTargetSelected?: HostTargetSelectedHandler;
    /**
     * Host-side render of the current payload (issue #109). Supplying one is
     * what makes the designer offer its **Display preview** toggle at all — no
     * provider, no toggle and no other visual trace, exactly like `actions` and
     * `targets` (conditional chrome; standalone output is unchanged).
     *
     * A stable closure — there is no update channel for it (ADR-018: data is
     * pushed, functions are not).
     */
    renderPreview?: HostPreviewRenderer;
    /**
     * Resolve a font or image the designer could not resolve locally
     * ([issue #138](https://github.com/schlomo/odl-drawcustom-designer/issues/138)).
     *
     * Payloads reference assets by the name the *host* understands
     * (`Ubuntu-R.ttf`, `logo.png`) — for the OpenDisplay integration, a file in
     * its font/media directories. The designer asks this closure for any
     * reference left over after its own tiers (Content Manager uploads, bundled
     * assets), and caches what comes back for the life of the mount:
     *
     * - a `Blob` — the asset's bytes (the safest answer: no CORS, no tainting);
     * - a `string` — a URL the designer can load (data:, blob:, or same-origin);
     * - `null` — "I don't have that", which surfaces as the designer's explicit
     *   render-error state on every element referencing the asset, naming it and
     *   saying the host could not supply it. A rejection reads the same way,
     *   with its reason. Never a silent skip, never a wrong render.
     *
     * Search paths, directory layout and permissions stay host-side: the
     * contract is `name -> asset` and nothing else (ADR-018). A stable closure
     * fixed at mount — like `onAction`, there is no update channel for it; an
     * unresolvable name is retried after a short interval, so a host whose store
     * comes back can answer differently without a remount.
     */
    resolveAsset?: HostAssetResolver;
    /**
     * Called on a designer status transition (issue #133) — see
     * {@link HostStatusChangeHandler}. Optional: a host that only wants status
     * on demand reads {@link MountHandle.getStatus} instead and never supplies
     * this.
     *
     * A stable closure fixed at mount — there is no update channel for it
     * (ADR-018: data is pushed, functions are not).
     */
    onStatusChange?: HostStatusChangeHandler;
}

/**
 * Runtime version (issue #23, reworked 2026-07-29: git tags are the sole
 * version source, not package.json — see `tools/version.ts`). Baked in at
 * build time (`tools/buildDefines.ts`) from the release script's
 * `APP_VERSION` env var; a non-release build (local dev, CI `checks`) has
 * none set and falls back to `0.0.0-dev`. Re-exported from
 * `src/embed/index.ts` (`version`) and surfaced on `MountHandle.version`.
 */
export declare const version: string;

export { }
