// Vendors Bootstrap Icons into the packaged static tree.
//
// The platform pulls the same icon set from a CDN. The desktop client cannot:
// it must render correctly with no network at all, and the installer ships
// whatever sits under broccoli_desktop/static. So the font is copied in at
// build time from the pinned npm package, the same way output.css is produced.
//
// The destination is deliberately outside Tailwind's @source scan (see the
// `@source not` line in app.css) -- the icon stylesheet is a few thousand class
// names that would otherwise be fed to the candidate extractor for nothing.

import { cp, mkdir, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = join(here, "node_modules", "bootstrap-icons", "font");
const destination = join(here, "..", "broccoli_desktop", "static", "vendor", "bootstrap-icons");

await rm(destination, { recursive: true, force: true });
await mkdir(destination, { recursive: true });
await cp(join(source, "bootstrap-icons.css"), join(destination, "bootstrap-icons.css"));
await cp(join(source, "fonts"), join(destination, "fonts"), { recursive: true });

console.log(`Vendored Bootstrap Icons into ${destination}`);
