# drawcustom

With `drawcustom`, you can create an image in Home Assistant and send the rendered image to an OpenDisplay device.

> **Element reference lives in [odl-renderer](https://github.com/OpenDisplay/odl-renderer).**
> drawcustom renders the payload with the odl-renderer library, which owns every element
> type and shared field (`visible`, `rotation`, `mirror`, `pivot`, anchors, colors,
> coordinates). For the full, authoritative field reference see the
> [odl-renderer README](https://github.com/OpenDisplay/odl-renderer/blob/odl-renderer-v0.5.10/README.md).
> This page documents only what the Home Assistant integration adds on top: the service
> call, fonts, templating, and entity-backed plots.

## Contents
- [Basic Usage](#basic-usage)
- [Service Options](#service-options)
- [Color Support](#color-support)
- [Font support](#font-support)
- [Element types](#element-types)
- [Plot (entity-backed)](#plot-entity-backed)
- [Template Examples](#template-examples)

## Basic Usage

The payload is a list of drawing elements that define what to display. Each element must specify its type and required properties. The elements are drawn in order from first to last.

Example payload:
```yaml
- type: text
  value: Hello World!
  font: ppb.ttf
  x: 0
  y: 0
  size: 40
  color: red
- type: icon
  value: mdi:account-cowboy-hat
  x: 60
  y: 120
  size: 120
  color: red
```

### Service Options

The service targets one or more OpenDisplay devices via the standard Home Assistant
**target** selector (devices, areas, or labels). The following data fields are available:

| Option             | Description                              | Default   | Values                                                                                                    |
|--------------------|------------------------------------------|-----------|-----------------------------------------------------------------------------------------------------------|
| `payload`          | List of drawing elements (required)      | —         | See [Element types](#element-types)                                                                       |
| `background`       | Background color                         | `white`   | `white`, `black`, `accent`, `red`, `yellow`                                                               |
| `rotate`           | Rotation of the whole image, in degrees  | `0`       | `0`, `90`, `180`, `270`                                                                                   |
| `dither`           | Dithering algorithm                      | `ordered` | `none`, `burkes`, `ordered`, `floyd_steinberg`, `atkinson`, `stucki`, `sierra`, `sierra_lite`, `jarvis_judice_ninke`, `dizzy` |
| `refresh_type`     | E-paper refresh mode                     | `full`    | `full`, `fast`                                                                                            |
| `tone_compression` | Tone compression strength (%)            | automatic | `0`–`100`; omit for automatic tone mapping                                                                |
| `dry-run`          | Generate the image without sending it    | `false`   | `true`, `false`                                                                                           |

> Legacy numeric `dither` values (`0`/`1`/`2`, …) and other pre-3.0 keys (e.g. `ttl`)
> are still accepted for backward compatibility — unknown keys are ignored.


## Color Support

OpenDisplay devices predominantly come in two variants: red and yellow accent colors (devices with more also exist). You can specify colors in several ways:

- Using explicit colors: `"black"`, `"white"`, `"red"`, `"yellow"`
- Using gray levels (for multi-gray displays): `"dkgray"` (dark), `"gray"`/`"grey"` (mid), `"ltgray"` (light) — long forms `"darkgray"`/`"dark_gray"` and `"lightgray"`/`"light_gray"` also work
- Using halftone colors (requires a dithering mode, e.g. the default `dither: ordered`): `"half_black"` (mid gray, same as `"gray"`), `"half_white"` (light gray, same as `"ltgray"`), `"half_red"`, `"half_yellow"`
- Using single letter shortcuts: `"b"` (black), `"w"` (white), `"r"` (red), `"y"` (yellow)
- Using halftone shortcuts: `"hb"` (mid gray), `"hw"` (light gray), `"hr"` (50% red), `"hy"` (50% yellow)
- Using `"accent"`, `"a"`, `"half_accent"`, or `"ha"` to automatically use the display's accent color (red or yellow depending on the hardware)
- Using hex colors: `"#RGB"`, `"#RGBA"`, `"#RRGGBB"`, or `"#RRGGBBAA"` (e.g., `"#F00"` or `"#FF0000"` for red). The 4-/8-digit forms are accepted for convenience, but the alpha component is **not** blended during rendering and has no visible effect — colors render fully opaque. For multi-gray displays, equal-channel hex such as `"#808080"` selects a gray level.

> [!IMPORTANT]
> Always quote hex colors in YAML: write `fill: "#FF0000"`, not `fill: #FF0000`.
> In YAML an unquoted `#` starts a comment, so `fill: #FF0000` parses as an empty
> value (`null`) and the color is silently dropped. Named colors like `red` do not
> need quotes.

Example payload adapting to display color:
```yaml
- type: text
  value: Hello World!
  font: ppb.ttf
  x: 0
  y: 0
  size: 40
  color: accent  # Will be red or yellow depending on the display
```

### Color Support by Element Type

All elements that support colors (text, shapes, icons, etc.) accept the following color properties:

| Property     | Description                        | Values                                                                       |
|--------------|------------------------------------|------------------------------------------------------------------------------|
| `color`      | Primary color                      | `white`, `black`, `dkgray`, `gray`, `ltgray`, `accent`, `red`, `yellow`, `#RRGGBB` |
| `fill`       | Fill color                         | `white`, `black`, `dkgray`, `gray`, `ltgray`, `accent`, `red`, `yellow`, `#RRGGBB` |
| `outline`    | Outline/border color               | `white`, `black`, `dkgray`, `gray`, `ltgray`, `accent`, `red`, `yellow`, `#RRGGBB` |
| `background` | Background color (when applicable) | `white`, `black`, `dkgray`, `gray`, `ltgray`, `accent`, `red`, `yellow`, `#RRGGBB` |

Using `"accent"` is recommended for portable scripts that should work with both red and yellow devices.

## Font support

Custom fonts are supported for text elements. The integration provides several ways to specify fonts:

### Specifying fonts

```yaml
# Using the default font (ppb.ttf)
- type: text
  value: Default font
  font: ppb.ttf # Optional, you can also omit this line
  x: 10
  y: 10
  
# Using just the filename (searched in all font directories)
- type: text
  value: "Custom Font"
  font: "CustomFont.ttf"
  x: 10
  y: 50

# Using the absolute path (direct access)
- type: text
  value: "Custom Font with Path"
  font: "/media/GothamBold-Rnd.ttf"
  x: 10
  y: 90
```

### Font locations

A font referenced by filename (e.g. `font: "CustomFont.ttf"`) is searched for in these
directories, in order:

1. `/config/www/fonts/`
2. `/config/media/fonts/`
3. `/media/fonts/`

The built-in fonts `ppb.ttf` and `rbm.ttf` are bundled with odl-renderer and always
available, and absolute paths (e.g. `font: "/media/GothamBold-Rnd.ttf"`) are used
directly — neither needs any of the directories above.

> **Note:** none of these directories exist by default. Create the one you want to use.

#### Setting Up Font Directories

To create the standard font directories:

```bash
# Under your Home Assistant config directory
mkdir -p /config/www/fonts
mkdir -p /config/media/fonts

# Or the top-level media directory
mkdir -p /media/fonts
```

You can access these directories:
- Through the Home Assistant File Editor or the VSCode Addon by navigating to `/config/www/fonts/`
- Via SFTP/SSH if you have direct access to your Home Assistant server
- Through Samba shares if configured

### Default fonts

Two default fonts are bundled with odl-renderer and always available:
- `ppb.ttf`
- `rbm.ttf`

They are used as the fallback when a specified font cannot be found.

### Font not found

If a referenced font can't be found in any of the search directories, a warning is
logged and rendering falls back to the default `ppb.ttf`.

## Element types

The payload is a list of drawing elements rendered by
[odl-renderer](https://github.com/OpenDisplay/odl-renderer). The integration passes
your elements through unchanged and additionally expands Home Assistant templates in
field values, resolves entity-backed data for [plots](#plot-entity-backed), and maps
colors / applies dithering for the target display.

For the **complete field reference of every element type and the shared fields**, see
the **[odl-renderer README](https://github.com/OpenDisplay/odl-renderer/blob/odl-renderer-v0.5.10/README.md)**.
This integration pins the odl-renderer version in `manifest.json`; the link above points
at the matching tag.
<!-- Keep this version in sync with the odl-renderer pin in manifest.json. -->


| Type | Purpose |
|------|---------|
| `text` | Single-line text with wrapping, truncation, stroke, inline color markup |
| `multiline` | Fixed-line text split by a delimiter |
| `line` | Straight line |
| `rectangle` | Rectangle (filled / outlined) |
| `rectangle_pattern` | Repeated grid of rectangles |
| `polygon` | Arbitrary polygon |
| `circle` | Circle |
| `ellipse` | Ellipse |
| `arc` | Arc / pie slice |
| `icon` | Material Design Icon |
| `icon_sequence` | Row of icons |
| `dlimg` | Downloaded / embedded image — `url` is `http(s)://`, a `data:` URI, an absolute path, or `/local/<name>` for `<config>/www/<name>` (as everywhere in Home Assistant) |
| `qrcode` | QR code |
| `plot` | Line plot of Home Assistant history (see [Plot](#plot-entity-backed)) |
| `progress_bar` | Progress bar with optional percentage text |
| `debug_grid` | Coordinate grid overlay for layout debugging |

Shared fields available on **every** element include `visible` (a templated `"false"`
or empty string hides it), `rotation` (degrees, positive = clockwise) and `mirror`
(`h` / `v` / `hv`) with an optional `pivot`, plus
[anchors](https://github.com/OpenDisplay/odl-renderer/blob/odl-renderer-v0.5.10/README.md#anchors),
colors and percentage coordinates — all documented in the odl-renderer README.

> **Plots are different here.** odl-renderer's `plot` takes raw data points; this
> integration instead lets you reference Home Assistant entities and a time range and
> fetches the history for you. Those entity-backed options are documented below.

## Plot (entity-backed)
Renders historical data from Home Assistant entities as a line plot. This is the one
element whose configuration is integration-specific: you reference entities and a time
range, and the integration fetches the history and feeds raw points to odl-renderer.

```yaml
- type: plot
  x_start: 10
  y_start: 20
  x_end: 199
  y_end: 119
  duration: 36000 # 10 hours in seconds
  low: 10
  high: 20
  font: "ppb.ttf"
  data:
    - entity: sensor.temperature
      width: 3
    - entity: sensor.humidity
      color: red
  ```

| Parameter      | Description               | Required | Default       | Notes                                     |
|----------------|---------------------------|----------|---------------|-------------------------------------------|
| `data`         | List of entities to plot  | Yes      | -             | Array                                     |
| `ylegend`      | Y-axis legend options     | No       | -             | See [Y-Legend Options](#Y-Legend-Options) |
| `yaxis`        | Y-axis options            | No       | -             | See [Y-Axis Options](#Y-Axis-Options)     |
| `xlegend`      | X-axis legend options     | No       | -             | See [X-Legend Options](#X-Legend-Options) |
| `xaxis`        | X-axis options            | No       | -             | See [X-Axis Options](#X-Axis-Options)     |
| `x_start`      | Left position             | No       | `0`           | Pixels                                    |
| `y_start`      | Top position              | No       | `0`           | Pixels                                    |
| `x_end`        | Right position            | No       | Canvas width  | Pixels                                    |
| `y_end`        | Bottom position           | No       | Canvas height | Pixels                                    |
| `duration`     | Time range                | No       | `86400`       | Seconds                                   |
| `low`          | Minimum Y value           | No       | Auto          | Number                                    |
| `high`         | Maximum Y value           | No       | Auto          | Number                                    |
| `font`         | Font for Legend Text      | No       | `ppb.ttf`     | Font name                                 |
| `round_values` | Round min/max to integers | No       | `false`       | `true`, `false`                           |
| `size`         | Font size                 | No       | `10`          | Pixels                                    |
| `debug`        | Show debug borders        | No       | `false`       | `true`, `false`                           |
| `visible`      | Show/hide element         | No       | `true`        | `true`, `false`                           |

#### Line Options (per entity)
Each entry in the `data` array can have these options:
```yaml
- entity: sensor.temperature  
  color: red
  width: 2
  smooth: true
  show_points: true
  point_size: 3
  point_color: black
  value_scale: 1.0
```
| Parameter     | Description                                                        | Required | Default | Notes                       |
|---------------|--------------------------------------------------------------------|----------|---------|-----------------------------|
| `entity`      | Entity ID to plot                                                  | Yes      | -       | String                      |
| `color`       | Line color                                                         | No       | `black` | Any supported color         |
| `width`       | Line width                                                         | No       | `1`     | Pixels                      |
| `span_gaps`   | Connect lines across gaps                                          | No       | `false` | `true`, `false`, or seconds |
| `smooth`      | Curve smoothing                                                    | No       | `false` | `true`, `false`             |
| `line_style`  | `linear`: direct connections between points, `step`: stair pattern | No       | linear  | `linear` or `step`          |
| `show_points` | Show data points                                                   | No       | `false` | `true`, `false`             |
| `point_size`  | Data point size                                                    | No       | `3`     | Pixels                      |
| `point_color` | Data point color                                                   | No       | `black` | Any supported color         |
| `value_scale` | Scale data points by a factor                                      | No       | `1.0`   | Float                       |

#### Gap Handling

By default, the plot creates visual gaps when sensor data is unavailable or null. This matches Home Assistant's history graph behavior and prevents misleading visual connections across missing data periods.

**`span_gaps` Parameter Options:**

- `false` (default): Break lines at null/unavailable values - creates visual gaps
- `true`: Connect lines across all gaps
- `<number>`: Only span time gaps smaller than N seconds

**Examples:**

```yaml
# Default behavior - break at null values (recommended)
- type: plot
  data:
    - entity: sensor.temperature
      color: red
       # span_gaps: false (implicit default)

# Connect across all gaps
- type: plot
  data:
    - entity: sensor.temperature
      span_gaps: true

# Only break at gaps longer than 1 hour
- type: plot
  data:
    - entity: sensor.temperature
      span_gaps: 3600  # seconds
```

#### Y-Legend Options
```yaml
ylegend:
  width: -1
  color: black
  position: left
  size: 10
```
| Parameter  | Description     | Required | Default | Notes                         |
|------------|-----------------|----------|---------|-------------------------------|
| `width`    | Legend width    | No       | -1      | Pixels or `-1` for auto width |
| `color`    | Legend color    | No       | `black` | Any supported color           |
| `position` | Legend position | No       | `left`  | `left`, `right`               |
| `size`     | Font size       | No       | `10`    | Pixels                        |


#### Y-Axis Options
```yaml
yaxis:
  width: 1
  color: black
  tick_width: 2
  tick_every: 1.0
  grid: 5
  grid_color: black
  grid_style: dotted
```
| Parameter    | Description     | Required | Default   | Notes                                  |
|--------------|-----------------|----------|-----------|----------------------------------------|
| `width`      | Axis line width | No       | `1`       | Pixels                                 |
| `color`      | Axis color      | No       | `black`   | Any supported color                    |
| `tick_width` | Tick mark width | No       | `2`       | Pixels                                 |
| `tick_every` | Tick interval   | No       | `1.0`     | Float                                  |
| `grid`       | Enable Grid     | No       | `true`    | Boolean                                |
| `grid_color` | Grid color      | No       | `black`   | Any supported color                    |
| `grid_style` | Grid line style | No       | `dotted`  | `dotted`, `dashed`, or `lines` (solid) |

#### X-Legend Options
```yaml
xlegend:
  width: -1
  format: "%H:%M"
  interval: 3600
  snap_to_hours: true
  size: 10
  position: bottom
  color: black
```
| Parameter       | Description                | Required | Default  | Notes                                           |
|-----------------|----------------------------|----------|----------|-------------------------------------------------|
| `width`         | Legend width               | No       | -1       | Pixels or `-1` for auto width                   |
| `format`        | Time label format          | No       | `%H:%M`  | [Python strftime format](https://strftime.org/) |
| `interval`      | Time interval in seconds   | No       | `3600`   | Seconds                                         |
| `snap_to_hours` | Align time labels to hours | No       | `true`   | `true`, `false`                                 |
| `size`          | Font size for time labels  | No       | `10`     | Pixels                                          |
| `position`      | Position of time labels    | No       | `bottom` | `bottom` or `top`                               |
| `color`         | Color for time labels      | No       | `black`  | Any supported color                             |

#### X-Axis Options
```yaml
xaxis:
  width: 1
  color: black
  tick_width: 2
  tick_length: 4
  tick_every: 1.0
  grid: true
  grid_color: black
  grid_style: dotted
```
| Parameter     | Description      | Required | Default  | Notes                                  |
|---------------|------------------|----------|----------|----------------------------------------|
| `width`       | Axis line width  | No       | `1`      | Pixels                                 |
| `color`       | Axis color       | No       | `black`  | Any supported color                    |
| `tick_width`  | Tick mark width  | No       | `2`      | Pixels                                 |
| `tick_length` | Tick mark length | No       | `4`      | Pixels                                 |
| `tick_every`  | Tick interval    | No       | `1.0`    | Float                                  |
| `grid`        | Enable grid      | No       | `true`   | Boolean                                |
| `grid_color`  | Grid color       | No       | `black`  | Any supported color                    |
| `grid_style`  | Grid line style  | No       | `dotted` | `dotted`, `dashed`, or `lines` (solid) |

#### Example with Full Configuration
```yaml
- type: plot
  x_start: 10
  y_start: 20
  x_end: 290
  y_end: 120
  duration: 86400
  font: "ppb.ttf"
  round_values: true
  ylegend:
    color: black
    position: left
    size: 12
    width: -1
  yaxis:
    width: 1
    color: black
    grid: 5
    grid_color: gray
    grid_style: dotted
    tick_width: 2
    tick_every: 1.0
  xlegend:
    format: "%H:%M"
    interval: 3600
    snap_to_hours: true
    color: black
    position: bottom
    size: 12
    width: -1
  xaxis:
    width: 1
    color: black
    grid: 5
    grid_color: gray
    grid_style: dotted
    tick_width: 2
    tick_length: 4
    tick_every: 1.0
  data:
    - entity: sensor.temperature
      color: red
      width: 2
      smooth: true
      show_points: true
      point_size: 3
      point_color: black
      value_scale: 1.0
```

## Template Examples

Basic state display:
```yaml
- type: "text"
  value: "Temperature: {{ states('sensor.temperature') }}°C"
  x: 10
  y: 10
```

Conditional formatting:
```yaml
- type: "text"
  value: >
    Status:
    [{{ 'red' if is_state('binary_sensor.door', 'on') else 'black' }}]
    {{ states('binary_sensor.door') }}
    [/{{ 'red' if is_state('binary_sensor.door', 'on') else 'black' }}]
  parse_colors: true
  x: 10
  y: 10
```

Dynamic positioning:
```yaml
- type: "text"
  value: "Centered"
  x: "50%"
  y: "50%"
  anchor: "mm"
```

### Common Use Cases

Battery status with icon:
```yaml
- type: "icon"
  value: "mdi:battery"
  x: 10
  y: 10
  size: 24
  color: "{{ 'red' if states('sensor.battery')|float < 20 else 'black' }}"
- type: "text"
  value: "{{ states('sensor.battery') }}%"
  x: 40
  y: 10
```

Header with divider:
```yaml
- type: "text"
  value: "Status Overview"
  x: 10
  y: 10
  size: 24
- type: "line"
  x_start: 10
  x_end: 286
  y_start: 40
  width: 2
```

Multi-sensor display:
```yaml
- type: "text"
  value: "Living Room"
  x: 10
  y: 10
  size: 24
- type: "icon"
  value: "mdi:thermometer"
  x: 10
  y: 40
  size: 20
- type: "text"
  value: "{{ states('sensor.living_room_temperature') }}°C"
  x: 35
  y: 40
- type: "icon"
  value: "mdi:water-percent"
  x: 10
  y: 70
  size: 20
- type: "text"
  value: "{{ states('sensor.living_room_humidity') }}%"
  x: 35
  y: 70
```
