# Brand assets

1st Energy's logo, prepared at the sizes Home Assistant expects.

> **These are 1st Energy Pty Ltd's trademarks, not this project's.** This
> integration is independent and unofficial: not affiliated with, endorsed
> by, or sponsored by 1st Energy. The logo is here solely to identify the
> service the integration connects to. It is **not** covered by this
> project's MIT licence. Read [TRADEMARKS.md](../TRADEMARKS.md).

## Files

This directory is the workshop. The files Home Assistant actually serves are
the copies in `custom_components/first_energy/brand/`, which
`make_assets.py` installs.

| File | Size | Use |
|---|---|---|
| `1st_energy_logo.svg` | vector | **the source**: 1st Energy's official logo, unmodified |
| `icon.svg` | vector | the same artwork on a square canvas |
| `icon.png` | 256×256 | the square icon Home Assistant displays |
| `icon@2x.png` | 512×512 | high-DPI variant |
| `logo.svg` | vector | the full lockup, source canvas |
| `logo.png` | 523×256 | lockup raster |
| `logo@2x.png` | 1046×512 | high-DPI variant |

There are no `dark_*` variants. Making them would mean recolouring the
mark, which this project doesn't do. The navy "ENERGY" is less legible on
dark themes as a result; Home Assistant falls back to these files for dark
requests.

## Where the source came from

`1st_energy_logo.svg` is the logo in the header of `1stenergy.com.au`,
served from `/app/themes/slate/dist/images/1st-energy-logo.svg`.

## How the derivatives are made

`make_assets.py` produces everything from that one file by **changing the
viewBox**, never by editing the artwork:

- **icon**: a square viewBox with the lockup centred and 8% padding either
  side.
- **logo**: the source viewBox (`0 0 1280 626.3`), which is already tight to
  the artwork.

```bash
uv run --no-project --with cairosvg python brands/make_assets.py
```

It uses cairosvg rather than ImageMagick: ImageMagick's built-in SVG
renderer drops the gradient on the three circles and draws them black.

## How Home Assistant picks these up

Since **2026.3** (this integration's minimum) the frontend asks Home
Assistant's own
[brands proxy](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/)
for integration images, and it checks a custom integration's local `brand/`
directory before the brands CDN. So the icon ships with the integration: no
pull request to home-assistant/brands, and it works offline.

The directory must be named `brand` (singular); that is how Home Assistant
knows an integration has local branding.
