# Changelog

## [3.1.0](https://github.com/OpenDisplay/Home_Assistant_Integration/compare/3.0.2...3.1.0) (2026-10-09)


### Features

* add a connectivity binary sensor ([a7635e7](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/a7635e71f98d4ade62733da583b0c3cec57909f9))
* add a refine dithering option to image uploads ([3df2171](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/3df21710dfd5374e22984a73fbf0b86b8704231b))


### Bug Fixes

* only offer devices that advertise as OpenDisplay ([02e5e79](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/02e5e790053edf6a0077a0b87e62bfac591974a1))
* require py-opendisplay&gt;=7.18.1 instead of an exact pin ([735ceb8](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/735ceb8d201715997657dbb04476c8d347b8f3ee))
* take camera and image snapshots from the entity directly ([9dd46e4](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/9dd46e498746e93f5914111f1b29330757b8c15f))
* translate the setup errors ([9124594](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/9124594124b071b86fa5168988e27586a6b9e54b))
* update py-opendisplay to 7.18.1 ([1ad2af9](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/1ad2af9f2c42273cbf27b06acf1dfb9db1146471))

## [3.0.2](https://github.com/OpenDisplay/Home_Assistant_Integration/compare/3.0.1...3.0.2) (2026-08-26)


### Bug Fixes

* declare silabs-ble-ota explicitly in the manifest ([b27f123](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/b27f123697a114e220f904df15e291ca609bfde9))
* fetch camera and image sources from their still endpoint ([5589a94](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/5589a940529ea17e71806bba4427745bb699129d))
* register the mDNS service type in the manifest ([8096fc4](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/8096fc4b83252388f1296c9d4fa1ed8a22509053))
* update py-opendisplay to 7.16.0 ([516ff5c](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/516ff5c95f0d79c029b925b0f595e48158717c3e))

## [3.0.1](https://github.com/OpenDisplay/Home_Assistant_Integration/compare/3.0.0...3.0.1) (2026-08-22)


### Bug Fixes

* attach the HACS zip from the release-please job ([bc1cf86](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/bc1cf8694d52672ca28028d98cef660ad7e1c82c))
* **delivery:** report "auth" not a stale expiry when submitting while paused ([098704f](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/098704f8842dc08c263f251c9bd575286a733669)), closes [#91](https://github.com/OpenDisplay/Home_Assistant_Integration/issues/91)
* **delivery:** stop the auth retry loop on a pending config resync ([3caca90](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/3caca90461c8ddc9d2b9777accf2367750e54026)), closes [#91](https://github.com/OpenDisplay/Home_Assistant_Integration/issues/91)
* never set up the event platform for a base model ([4929c5c](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/4929c5c4b8f4ea18b502a7a17a253540894b8235))
* redact the AES key and device identity from diagnostics ([e831ba7](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/e831ba7a12a50a533d87c5b2be80b4b8c3e02fbb))
* **translations:** more natural German terminology ([#104](https://github.com/OpenDisplay/Home_Assistant_Integration/issues/104)) ([4198741](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/41987414a7072e86b4f3117ef6f3db5a968249e0))

## [3.0.0](https://github.com/OpenDisplay/Home_Assistant_Integration/compare/v3.0.0-beta.10...3.0.0) (2026-08-21)


### Features

* rewrite integration (3.0.0) ([6ddd641](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/6ddd6410a323009f72bce7a41971195dc34ec244))


### Miscellaneous Chores

* prepare 3.0.0 stable release ([882df96](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/882df961b82c841ea4732ee9012bc4309afe887a))

## [2.0.2](https://github.com/OpenDisplay/Home_Assistant_Integration/compare/2.0.1...2.0.2) (2026-05-12)


### Performance Improvements

* keep drawcustom images as PIL until upload ([e72b609](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/e72b609c2d0ffbe925033db5aeaf67863ce3608c))
* log drawcustom upload timings ([fc034d4](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/fc034d4083513fe7e1c5ff83b49f2902632ba5f1))
* optimize BLE image preparation ([c6fabd1](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/c6fabd1f619de2fcf0914eec33a87470ed1fb98b))
* select direct write compression by payload size ([1233130](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/1233130780da1c6439c98a6273b7155709814c1d))

## [2.0.1](https://github.com/OpenDisplay/Home_Assistant_Integration/compare/2.0.0...2.0.1) (2026-02-25)


### Bug Fixes

* fix repo URL in update.py ([4ca46c5](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/4ca46c523483f3fd21912e09aba77784e1d7b064))
* Fix tests and rename images ([#7](https://github.com/OpenDisplay/Home_Assistant_Integration/issues/7)) ([e2e64ee](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/e2e64ee54603118ef901c7e0b32382389e026023))
* loading tag types from GitHub, workflow for fallback_definitions ([#8](https://github.com/OpenDisplay/Home_Assistant_Integration/issues/8)) ([00a623c](https://github.com/OpenDisplay/Home_Assistant_Integration/commit/00a623cecd98d832dbb6dee81de358e1757f777c))

## [2.0.0](https://github.com/OpenDisplay-org/Home_Assistant_Integration/compare/1.0.0...2.0.0) (2026-01-12)


### ⚠ BREAKING CHANGES

* change domain to opendisplay

### Code Refactoring

* change domain to opendisplay ([74128a8](https://github.com/OpenDisplay-org/Home_Assistant_Integration/commit/74128a80812c9170bf9fbff30007b9faa9322b48))

## Changelog
