# Third-Party Notices

This source distribution declares its dependency and asset inventory below.
The browser extension build bundles runtime package code into generated files
under `extension/dist/`. Generated distributions must retain the applicable
notices and license terms. This inventory records upstream declarations and
does not decide license compatibility or grant a license for Omarchy Clipper.

## Bundled browser runtime packages

### @mozilla/readability 0.6.0

- Source: <https://github.com/mozilla/readability>
- Package: `@mozilla/readability@0.6.0`
- License: Apache License 2.0 (`Apache-2.0`)
- Verified package license file: `LICENSE.md`, SHA-256
  `a5b1e8181751ce05b85b7bfaa832b785e87086250a5148e679d17ca9bdcfa958`
- Copyright notice: Copyright (c) 2010 Arc90 Inc

The verified package license notice points to the Apache License 2.0 text at
<https://www.apache.org/licenses/LICENSE-2.0> and applies its warranty and
liability disclaimer.

### DOMPurify 3.4.17

- Source: <https://github.com/cure53/DOMPurify>
- Package: `dompurify@3.4.17`
- License: Mozilla Public License 2.0 or Apache License 2.0
  (`MPL-2.0 OR Apache-2.0`)
- Verified Apache license file: `LICENSE`, SHA-256
  `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`
- Verified MPL license file: `LICENSE-MPL`, SHA-256
  `fab3dd6bdab226f1c08630b1dd917e11fcb4ec5e1e020e2c16f83a0a13863e85`
- Copyright notice: Copyright (c) Cure53 and other contributors

The complete verified license texts are included in the locked npm package.
The Apache License 2.0 is available at
<https://www.apache.org/licenses/LICENSE-2.0>; the Mozilla Public License 2.0
is available at <https://www.mozilla.org/MPL/2.0/>.

### ipaddr.js 2.5.0

- Source: <https://github.com/whitequark/ipaddr.js>
- Package: `ipaddr.js@2.5.0`
- License: MIT (`MIT`)
- Verified package license file: `LICENSE`, SHA-256
  `62568a2d1337b77171ecca9db10579163446de1ba6151678e81f06cdc199971b`
- Copyright notice: Copyright (C) 2011-2017 whitequark

The complete verified MIT license text is included in the locked npm package.

## Build-only npm packages

These packages are required to type-check or build the extension. Their code
is not intentionally included in `extension/dist/`:

- `esbuild@0.25.12`, MIT. Verified `LICENSE.md` SHA-256:
  `b40ec5baec7bb34fa5b1c09521fa3cd52d5fad7adafed74932a2010d3612a681`.
  The lock also selects the matching optional `@esbuild/*@0.25.12` platform
  package for AIX, Android, Darwin, FreeBSD, Linux, NetBSD, OpenBSD,
  OpenHarmony, SunOS, or Windows; each declares MIT in the lock.
- `typescript@5.9.3`, Apache-2.0. Verified `LICENSE.txt` SHA-256:
  `a7d00bfd54525bc694b6e32f64c7ebcf5e6b7ae3657be5cc12767bce74654a47`.
- `@types/chrome@0.0.322`, MIT. Its locked transitive type-only packages are
  `@types/filesystem@0.0.36`, `@types/filewriter@0.0.33`,
  `@types/har-format@1.2.16`, and `@types/trusted-types@2.0.7`. Their verified
  Microsoft MIT `LICENSE` files share SHA-256
  `c2cfccb812fe482101a8f04597dfc5a9991a6b2748266c47ac91b6a5aae15383`.

`package-lock.json` is the authoritative complete npm package and version
inventory for this candidate.

## Native and desktop system dependencies

The executables dynamically link system-provided Qt 6 libraries for
Core, Gui, Widgets, Sql, Qml, Quick, QuickControls2, and WebEngineQuick, plus
libxml2. The mixed detail helper uses Widgets without linking WebEngine.
Those libraries and their license materials are supplied by the user's system;
they are not copied into this source candidate. Omarchy, Quickshell,
`wl-clipboard`, a supported browser, Node.js, and npm are external runtime or
build tools and are likewise not bundled here.

## Project asset

`assets/clipper.svg` is the only project-local image asset in this candidate.
It was generated as SVG source by an AI coding agent during this project's
initial implementation on October 14, 2026. The creation record shows the
agent adding the path directly; no external asset download or icon-library
attribution was found in the reviewed development records. This documents
the observed creation process, not a guarantee of unique or exclusive authorship.

The project owner has approved including this asset in the distribution under
the project's Apache License, Version 2.0. See `LICENSE` for the terms.
