// Unit tests for assetRequestUrl (tier-2 real-hardware finding: a payload
// referencing a host-side image by absolute path -- the maintainer's
// `/media/pohl89-480h.png` -- rendered fine on the server but showed the
// designer's own "missing asset" state, because the panel short-circuited
// every `kind !== 'font'` to `null` without ever calling the asset
// endpoint).
//
// Plain `node --test`, zero dependencies -- see key-containment.test.mjs's
// own header comment for why this repo tests panel JS this way. The
// decision the panel used to make inline lives in its own module for
// exactly this reason: the panel class itself imports the ~5.6MB vendored
// designer bundle and cannot be imported under `node --test`.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  ASSET_URL,
  RESOLVABLE_ASSET_KINDS,
  assetRequestUrl,
} from '../../custom_components/opendisplay/designer/frontend/panel/asset-request.js';

test('an image reference reaches the endpoint instead of short-circuiting to null', () => {
  // THE REGRESSION: this returned null before kind=image existed server-side.
  const url = assetRequestUrl('image', '/media/pohl89-480h.png');
  assert.notEqual(url, null);
  assert.ok(url.startsWith(`${ASSET_URL}?`), url);
  assert.ok(url.includes('kind=image'), url);
});

test('a font reference still reaches the endpoint, unchanged', () => {
  const url = assetRequestUrl('font', 'Tinos-Bold');
  assert.equal(url, `${ASSET_URL}?kind=font&name=Tinos-Bold`);
});

test('both kinds the designer can ask for are resolvable', () => {
  // AssetKind (vendored odl-drawcustom-designer.d.ts) is exactly these two.
  assert.deepEqual([...RESOLVABLE_ASSET_KINDS].sort(), ['font', 'image']);
});

test('an absolute path is percent-encoded, not pasted into the query raw', () => {
  const url = assetRequestUrl('image', '/media/holiday photos/a&b.png');
  assert.ok(url.includes(`name=${encodeURIComponent('/media/holiday photos/a&b.png')}`), url);
  // A raw `&` would split the query and silently drop most of the name.
  assert.equal(url.split('&').length, 2, url);
});

test('a kind the endpoint does not serve is not requested at all', () => {
  assert.equal(assetRequestUrl('video', 'clip.mp4'), null);
  assert.equal(assetRequestUrl('', 'x'), null);
  assert.equal(assetRequestUrl(undefined, 'x'), null);
});

test('an empty name is not requested at all', () => {
  assert.equal(assetRequestUrl('image', ''), null);
  assert.equal(assetRequestUrl('font', undefined), null);
});
