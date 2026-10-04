# Image controls

[Documentation](index.md) › Image controls

The Config page lists the controls the camera really has, read from the device: typically
exposure, white balance, brightness, contrast, saturation, sharpness, gamma, hue, gain,
backlight compensation and power line frequency. The names, ranges and defaults differ
per camera.

<!-- comparison images per control, made at different times of day, go here -->

## Modes

Each control has a mode:

| Mode | The control is |
|---|---|
| **Camera** | left to the camera: its automatic mode where it has one (exposure, white balance), otherwise its default value |
| **Manual** | the value you enter, within the camera's range |
| **Software** | adjusted continuously by the firmware from the picture (exposure, contrast, brightness and gamma) |

## Software mode

Every 1.5 seconds the firmware measures the picture (ignoring its brightest 5%: sky,
lamps, headlights) and nudges one of the software controls, in turn:

| Control | Aims for | Notes |
|---|---|---|
| Exposure | mean brightness 100 (of 255) | never longer than one frame; if the picture is still too dark at that limit, it hands over to the camera's own auto exposure (which also raises gain) and tries again 5 minutes later, once the picture is bright |
| Contrast | spread (standard deviation) of at least 45 | only raises it for flat pictures, never flattens a scene below the camera's default; eases back to the default in very dark or very bright pictures, where more contrast only makes it worse |
| Brightness | black level (darkest 5%) near 12 | lifts the shadows; learns how strongly the camera reacts |
| Gamma | mean equal to median (balanced tones) | brightens a dark picture when exposure can do no more; does not brighten a picture that is already bright |

The **offset** slider (-100 to +100) moves what a control aims for: +100 means a mean
brightness 50 higher for exposure, a spread 30 higher for contrast, a black level 20
higher for brightness, a tone balance 10 higher for gamma. The text next to it shows the
target.

The value the loop has set is shown next to the control ("now ..."). If a camera accepts
exposure changes but ignores them, the loop notices and stops trying; the page says so.

When the picture has both blown-out highlights and crushed shadows (backlight, low sun),
no global control can fix it; the loop then leaves contrast, brightness and gamma near
their defaults instead of chasing either side.

## Light and dark sets

Each camera keeps two sets of controls, on the **Light** and **Dark** tabs. Each set has
its own mode, value and offset per control, and its own **frame rate**. The Dark set starts
as a copy of the Light set.

A lower frame rate lets the camera expose each frame longer: at 10 fps up to 100 ms, at
30 fps only 33 ms. At night that is the biggest single improvement: a brighter, less noisy
picture. The frame rate row shows the rate the camera actually delivers; it can be lower
than set when the camera slows down in the dark.

Switching to a set with another frame rate reopens the camera: the picture freezes for
about a second, but recordings carry on.

## Day/night switching

**Day/night** chooses when the Dark set applies:

| Setting | The Dark set applies |
|---|---|
| **Off** | never: one set, day and night |
| **By light level** | when the picture's mean brightness (0-255) stays below *Dark below* (default 30) for 2 minutes; the Light set returns when it stays above *Light again above* (default 80) for 2 minutes |
| **By sunset and sunrise** | from sunset until sunrise, moved by *Minutes into the night* (-180 to 180): +30 starts the night 30 minutes after sunset and ends it 30 minutes before sunrise |

**By light level**: the camera cannot report how much light there is, so the picture is
the measure, and the Dark set itself makes the picture brighter. Keep the gap between the
two thresholds wider than that, or it switches back and forth. The page shows the current
light level to help choose them.

**By sunset and sunrise**: computed with [astral](https://astral.readthedocs.io) for the
location under Global (latitude and longitude); left empty, a city in the system time zone
is used. Where the sun does not set or rise that day (far north or south), the whole day
uses the Light or the Dark set. The page shows today's switching times.

The Config page shows which set is in use, marked "● now" on its tab. The software loop
keeps running across a switch; manual and camera modes are applied at once.
