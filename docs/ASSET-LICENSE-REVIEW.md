# Third-party asset and license review

This document records what could and could not be verified from the source archive supplied for this release candidate.

## Fonts — notice included

`static/fonts/LICENSES.txt` contains the per-font copyright notices extracted from the WOFF2 metadata, upstream project links, and the full SIL Open Font License 1.1 text for Oswald, Source Serif 4, Space Grotesk, Inter, Fraunces, Lora, and JetBrains Mono. Before release, verify that each font file's exact family/version matches the notice. The license permits bundling font software with applications subject to its conditions; the full text is included alongside the font files.

## Audio — generated for this project

All nine WAV files in `static/sounds/` were synthesised by code. `tools/generate_sounds.py` rebuilds every one of them byte for byte from sine waves, filtered noise and simple drum models:

- `music-ambient.wav`, `music-brown.wav`, `music-lofi1.wav`, `music-lofi2.wav`, `music-rain.wav`
- `tick-metronome.wav`, `tick-pocket.wav`, `tick-wall.wav`, `tick-wood.wav`

No recordings, samples, loops or machine-learning audio models were used, and nothing was copied from another work, so there is no third-party audio to license. The generator was written with an AI coding assistant; the sounds themselves come from deterministic signal-processing code. They are released under the same MIT license as the rest of the project.

This is a statement of provenance, not legal advice. Copyright rules for software-generated works differ between countries. If you ever add recordings, note the author, source URL and license here.

## App icon

The `assets/Tickover.*` icon set (a stopwatch with a tick) was drawn for this project with code. It is original artwork released under the MIT license and can be replaced if the project owner selects a different concept.

The release workflow checks the repository Actions variable
`AUDIO_ASSETS_LICENSE_CONFIRMED`. It will still build test artifacts, but it
will not publish a GitHub Release until the maintainer explicitly sets this
variable to `true`. The audio section above documents where the sounds come from, so
set the variable once you are comfortable with it.
