/**
 * The `resolveAsset` request the panel makes on the designer's behalf.
 *
 * Extracted from the panel wrapper for the same reason `rotation.js` and
 * `drawcustom-request.js` were: the decision is pure, it is worth unit
 * testing (`tests/js/asset-request.test.mjs`), and the panel class itself
 * imports the vendored designer bundle and cannot be loaded under
 * `node --test`.
 *
 * WHAT THIS FIXES (tier-2 round 3, real hardware): the panel used to
 * short-circuit every `kind !== 'font'` to `null` without calling the
 * endpoint at all, because the endpoint served fonts only. A display's
 * payload referencing `/media/pohl89-480h.png` therefore rendered fine on
 * the server and showed the designer's own missing-asset state in the
 * preview. The endpoint now resolves images too (`designer/asset.py`), so
 * the short-circuit is gone and both `AssetKind` values are requested.
 *
 * Note the asymmetry, which is deliberate and lives server-side: fonts are
 * resolved by BARE NAME against this integration's font directories, images
 * by ABSOLUTE PATH within Home Assistant's own permitted roots. This module
 * does not know or care -- the host contract is `name -> asset`, and the
 * name is passed through exactly as the designer supplied it.
 */

export const ASSET_URL = '/api/opendisplay/designer/asset';

/** `AssetKind` (vendored `odl-drawcustom-designer.d.ts`), in full. */
export const RESOLVABLE_ASSET_KINDS = Object.freeze(['font', 'image']);

/**
 * Return the asset-endpoint URL for one `(kind, name)` reference, or `null`
 * when there is nothing worth asking for.
 *
 * @param {string|undefined} kind An `AssetKind`.
 * @param {string|undefined} name The designer's own reference, verbatim.
 * @returns {string|null} The URL to fetch, or null to answer "not supplied"
 *   without a round trip.
 */
export function assetRequestUrl(kind, name) {
  if (!RESOLVABLE_ASSET_KINDS.includes(kind) || !name) return null;
  return `${ASSET_URL}?kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(name)}`;
}
